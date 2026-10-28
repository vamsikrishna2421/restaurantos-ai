"""Scenario control (CONTRACTS_API #35, CONTRACTS_SEED §11, ARCHITECTURE_OPS §11; P30b): the scenario a tenant is in
and the simulated world around the judge.

- ``load`` puts a tenant into a scenario through ``store.sandbox.reset(tenant_id, name)``: its state, events and ledger
  go, ``seq`` stays monotonic, the flags (rush, chaos, auto_cook) clear and ``sandbox.reset`` makes every surface
  resync. A sandbox comes back paused at the scenario's start; ``live`` and ``demo`` keep the scenario's own clock, and
  the public ``live`` board only ever runs ``quiet``. The route stops the tenant's rush first.
- ``rush_script``: the scenario's rush (``dinner_rush`` has one, ``quiet`` none), validated against the seed once per
  process (config files never change while the server runs); ``current_scenario`` names the tenant's scenario.
- ``place_sim_order``: one labelled simulated walk-in (channel ``sim``, data class ``synthetic``) through
  ``OrderService.place``, priced by the menu and idempotent by its key: no model is ever called, so it spends $0.
- ``cook_sim_tickets`` (``auto_cook``): the simulated kitchen bumps simulated tickets once their prep time is up and
  starts queued ones while their station has a free slot. A judge's own tickets (any other channel) are left alone.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

from restaurantos.contracts.api import ScenarioResult
from restaurantos.contracts.common import LIVE_TENANT, CallCtx, biz_from_ms, is_sandbox, ms_from_dt
from restaurantos.contracts.errors import BadRequest, Conflict, NotFound
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.orders import Cart, CartLine
from restaurantos.domain import pricing
from restaurantos.judge.chaos import TenantChaos
from restaurantos.seed.loader import load_seed
from restaurantos.seed.scenario import RushScript, ScriptLine, load_scenario
from restaurantos.services.container import require
from restaurantos.store import repo as store_repo
from restaurantos.store.sandbox import current_scenario as _current_scenario

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.events import ScenarioName
    from restaurantos.contracts.kitchen import Ticket
    from restaurantos.contracts.menu import MenuSnapshot
    from restaurantos.contracts.ports import RepoPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

LIVE_SCENARIOS: frozenset[str] = frozenset({"quiet"})  # the public board never shows a simulated rush
SIM_ACTOR = "system"
MIN_MS = 60_000
_scripts: dict[str, RushScript | None] = {}


def rush_script(name: str | None) -> RushScript | None:
    """The rush of scenario ``name`` (None for a scenario without one, or no scenario)."""
    if name is None:
        return None
    if name not in _scripts:
        _scripts[name] = load_scenario(name, seed=load_seed()).rush
    script = _scripts[name]
    return None if script is None else script.model_copy(deep=True)


async def current_scenario(conn: AsyncConnection, tenant_id: str) -> ScenarioName | None:
    """The scenario named by the tenant's latest ``scenario.loaded`` or ``sandbox.reset``."""
    return await _current_scenario(conn, tenant_id)


def repo_of(c: Container) -> RepoPort:
    return c.repo if c.repo is not None else store_repo  # type: ignore[return-value]


async def load(c: Container, tenant_id: str, name: ScenarioName) -> ScenarioResult:
    """REST #35: reset ``tenant_id`` into scenario ``name``; returns it with the tenant's business time afterwards.
    400 for a rush on the public ``live`` board; 404 for an unknown tenant."""
    if tenant_id == LIVE_TENANT and name not in LIVE_SCENARIOS:
        raise BadRequest("The public live board only runs the quiet scenario",
                         details={"scenario": name, "allowed": sorted(LIVE_SCENARIOS)})
    store = require(c, "store")
    await store.sandbox.reset(tenant_id, scenario=name)
    if isinstance(c.chaos, TenantChaos):  # the reset cleared the drill: never wait for the bus to say so
        await c.chaos.refresh(tenant_id)
    async with store.read() as conn:
        info = await repo_of(c).tenant(conn, tenant_id)
    now_biz = c.clock.for_tenant(info.clock).now_biz_ms()
    log.info("scenario %s loaded into %s", name, tenant_id)
    return ScenarioResult(loaded=name, biz_time=biz_from_ms(now_biz, c.settings.tenant_timezone))


# ----- the simulated world ----------------------------------------------------------------------------------------


async def menu_of(c: Container, tenant_id: str) -> MenuSnapshot:
    async with require(c, "store").read() as conn:
        return await repo_of(c).menu_snapshot(conn, tenant_id)


def sim_cart(key: str, session_id: str, lines: Iterable[ScriptLine], menu: MenuSnapshot) -> Cart | None:
    """A priced ``sim`` cart of the lines whose dish is on the menu (None when none is); ``place`` re-prices it."""
    items = {item.code: item for item in menu.items}
    out: list[CartLine] = []
    for line in lines:
        if (item := items.get(line.item_code)) is not None:
            out.append(CartLine(line_no=len(out) + 1, item_code=item.code, qty=line.qty, modifiers=list(line.modifiers),
                                unit_price_cents=item.price_cents,
                                line_total_cents=pricing.line_total_cents(line.qty, item.price_cents)))
    if not out:
        return None
    subtotal, tax, total = pricing.totals(out, menu.tax_bp)
    return Cart(session_id=session_id, cart_id=f"crt_{key}", lines=out, subtotal_cents=subtotal, tax_cents=tax,
                total_cents=total, language="en", channel="sim")


async def place_sim_order(c: Container, tenant_id: str, key: str, lines: Iterable[ScriptLine], *, session_id: str,
                          menu: MenuSnapshot | None = None) -> bool:
    """Place one simulated walk-in with idempotency key ``key`` (a replay of the key writes nothing). False when the
    menu refuses the cart now (an 86'd dish: 409 from ``place``, logged); other failures raise."""
    cart = sim_cart(key, session_id, lines, menu or await menu_of(c, tenant_id))
    if cart is None:
        return False
    ctx = CallCtx(tenant_id=tenant_id, session_id=session_id, trace_id=new_id("tr"), data_class="synthetic",
                  channel="sim", spend_scope=tenant_id if is_sandbox(tenant_id) else "pool:admin")
    try:
        await require(c, "orders").place(ctx, cart, key)
    except Conflict as exc:
        log.info("%s: simulated order %s not placed (%s)", tenant_id, key, exc.details.get("reason"))
        return False
    return True


def _prep_ms(ticket: Ticket, prep: dict[str, int]) -> int:
    return max((prep.get(line.item_code, 0) for line in ticket.lines if line.qty > 0), default=0) * MIN_MS


async def cook_sim_tickets(c: Container, tenant_id: str, now_biz_ms: int) -> int | None:
    """``auto_cook``: bump simulated tickets whose prep time is up, then start queued ones while their station has a
    free slot. Returns the business ms the next one is done, or None when nothing simulated is cooking."""
    async with require(c, "store").read() as conn:
        tickets = await repo_of(c).open_tickets(conn, tenant_id)
        menu = await repo_of(c).menu_snapshot(conn, tenant_id)
    prep = {item.code: item.prep_min for item in menu.items}
    slots = {station.id: station.parallel_slots for station in menu.stations}
    cooking = Counter(ticket.station_id for ticket in tickets if ticket.status == "cooking")
    sim = [ticket for ticket in tickets if ticket.channel == "sim"]
    dues: list[int] = []
    for ticket in sim:
        if ticket.status == "cooking" and ticket.started_biz is not None:
            done = ms_from_dt(ticket.started_biz) + _prep_ms(ticket, prep)
            if done > now_biz_ms:
                dues.append(done)
            elif await _ticket(c, ticket, "bump"):
                cooking[ticket.station_id] -= 1
    for ticket in sim:
        if ticket.status == "queued" and cooking[ticket.station_id] < slots.get(ticket.station_id, 1):
            if await _ticket(c, ticket, "start"):
                cooking[ticket.station_id] += 1
                dues.append(now_biz_ms + _prep_ms(ticket, prep))
    return min(dues, default=None)


async def _ticket(c: Container, ticket: Ticket, action: str) -> bool:
    try:
        await require(c, "kitchen").ticket_action(ticket.id, action, SIM_ACTOR)
    except (Conflict, NotFound) as exc:  # a cook got there first, or the order was cancelled
        log.info("simulated kitchen: could not %s ticket %s (%s)", action, ticket.id, type(exc).__name__)
        return False
    return True
