"""GM invariants I4–I6 (CONTRACTS_SEED §11) on the real store: a ``dinner_rush`` sandbox driven to I3.

P04's store and sandbox clone the materialized template and apply the scenario (P05); the rush's walk-ins (+1..+5)
and the canonical order (+6 or +9 business minutes) are then written the way ``OrderService.place`` writes them: the
order and its lines, one ticket per station (``kds.route``) and the reservations (``inventory.reserve``). The report
is built through ``gm.situation`` from ``repo`` reads and P09's forecast, at the canonical order's business time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import insert, update

from restaurantos.contracts import ids
from restaurantos.contracts.common import biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.gm import Candidate, Incident, PlanStep, SituationReport
from restaurantos.contracts.orders import Order, OrderLine
from restaurantos.domain import inventory, kds, pricing
from restaurantos.gm import baseline, simulate, situation
from restaurantos.gm.candidates import po_params
from restaurantos.seed import materialize
from restaurantos.seed.loader import load_seed
from restaurantos.seed.scenario import load_scenario
from restaurantos.services.container import Container
from restaurantos.store import repo
from restaurantos.store import tables as t
from tests.fakes.clock import DINNER_RUSH_ANCHOR_MS
from tests.fakes.container import make_test_container

SEED = load_seed()
TZ = SEED.tenant.timezone
MIN = 60_000
ANCHOR = DINNER_RUSH_ANCHOR_MS  # Sat 2026-11-07 18:42 ET
RUSH = load_scenario("dinner_rush", seed=SEED).rush
assert RUSH is not None
Line = tuple[str, int, list[str]]


async def place(c: Container, tid: str, at_ms: int, items: Sequence[Line]) -> Order:
    """One simulated walk-in placed at business time ``at_ms``, as ``OrderService.place`` writes it."""
    async with c.store.read() as conn:
        menu = await repo.menu_snapshot(conn, tid)
    price = {item.code: item.price_cents for item in menu.items}
    lines = [OrderLine(line_no=n, item_code=code, qty=qty, modifiers=mods, unit_price_cents=price[code],
                       line_total_cents=price[code] * qty) for n, (code, qty, mods) in enumerate(items, start=1)]
    subtotal, tax, total = pricing.totals(lines, menu.tax_bp)
    async with c.store.write() as tx:
        order = Order(id=ids.new_id("ord"), order_no=await repo.next_no(tx, tid, "order"), tenant_id=tid,
                      channel="sim", status="placed", language="en", session_id="sim:rush", cart_id=ids.new_id("crt"),
                      lines=lines, subtotal_cents=subtotal, tax_cents=tax, total_cents=total, ai_cost_micros=0,
                      created_at=utc_from_ms(c.clock.now_real_ms()), placed_biz=biz_from_ms(at_ms, TZ))
        await tx.conn.execute(insert(t.orders).values(
            tenant_id=tid, id=order.id, order_no=order.order_no, channel="sim", status="placed", language="en",
            session_id="sim:rush", cart_id=order.cart_id, subtotal_cents=subtotal, tax_cents=tax, total_cents=total,
            ai_cost_micros=0, flags=[], idempotency_key=f"rush:{order.id}", created_ms=c.clock.now_real_ms(),
            placed_biz_ms=at_ms))
        await tx.conn.execute(insert(t.order_lines), [{"tenant_id": tid, "order_id": order.id,
                                                       **line.model_dump(exclude={"line_total_cents"})}
                                                      for line in lines])
        for ticket in kds.route(order, menu, menu.stations):
            await tx.conn.execute(insert(t.tickets).values(
                tenant_id=tid, id=ticket.id, order_id=order.id, order_no=order.order_no, station_id=ticket.station_id,
                status="queued", lines=[line.model_dump(mode="json") for line in ticket.lines], channel="sim",
                created_biz_ms=at_ms, started_biz_ms=None, bumped_biz_ms=None, changed=False, undo_until_ms=None))
        for delta in inventory.reserve(lines, await repo.recipes(tx.conn, tid), ref_id=order.id):
            ing = t.ingredients
            await tx.conn.execute(update(ing).where(ing.c.tenant_id == tid, ing.c.id == delta.ingredient_id)
                                  .values(reserved=ing.c.reserved + delta.delta_reserved))
    return order


def _lines(order_lines: Sequence[object]) -> list[Line]:
    return [(line.item_code, line.qty, list(line.modifiers)) for line in order_lines]  # type: ignore[attr-defined]


@pytest.fixture(params=[6, 9], ids=["canonical+6", "canonical+9"])
async def i3(request: pytest.FixtureRequest) -> AsyncIterator[tuple[situation.Situation, SituationReport]]:
    """The sandbox right after the canonical order (I3: paneer ACT, 800 g free, out in 40 min), as the GM sees it."""
    assert RUSH is not None
    c = make_test_container(real_store=True)
    try:
        await materialize.startup(c)
        tid = await c.store.sandbox.create(pool="judge", caps_level="default")
        for walk_in in RUSH.pre_call.orders:
            await place(c, tid, ANCHOR + (walk_in.at_biz_min or 0) * MIN, _lines(walk_in.lines))
        now = ANCHOR + request.param * MIN
        await place(c, tid, now, _lines(RUSH.canonical_order.lines))
        async with c.store.read() as conn:
            incident = Incident(id="inc_paneer", kind="shortage", ingredient_id=RUSH.ingredient_id, band="act",
                                status="open", plan_epoch=1, opened_biz=biz_from_ms(now, TZ))
            s = await situation.load_situation(conn, tid, incident, now)
        report = situation.report(s)
        assert (report.incident.band, report.ingredient.free, report.ingredient.eta_min) == ("act", 800, 40)
        yield s, report
    finally:
        await c.store.aclose()


def _find(report: SituationReport, action: str, **params: object) -> Candidate:
    found = [c for c in report.candidates if c.action_type == action
             and all(c.params.get(key) == value for key, value in params.items())]
    assert len(found) == 1, (action, params, [c.params for c in report.candidates if c.action_type == action])
    return found[0]


def test_candidates_dinner_rush(i3: tuple[situation.Situation, SituationReport]) -> None:
    """I4: the candidates include every one CONTRACTS_SEED §11 lists, with its tier, within the cap of 16."""
    _, report = i3
    assert len(report.candidates) <= 16
    for dish in ("PNR_TIKKA", "PALAK_PNR", "PNR_BM"):
        assert _find(report, "set_item_availability", item_code=dish, available=False).tier == "T2"
    assert _find(report, "suggest_alternative", for_item_codes=["PNR_BM", "PALAK_PNR"],
                 alternative_codes=["VEG_KORMA", "CHANA_MASALA"]).tier == "T1"
    assert _find(report, "suggest_alternative", for_item_codes=["PNR_TIKKA"],
                 alternative_codes=["GOBI_MAN", "SAMOSA"]).tier == "T1"
    now = report.now_biz
    for packs, cost, tier in ((3, 3300, "T2"), (4, 4400, "T2")):
        patel = _find(report, "create_purchase_order", supplier_id="SUP_PATEL", packs=packs)
        assert (patel.params["qty"], patel.cost_cents, patel.tier) == (packs * 1000, cost, tier)
        assert ms_from_dt(po_params(patel).eta_biz) == ms_from_dt(now) + 40 * MIN
    dairy = _find(report, "create_purchase_order", supplier_id="SUP_FRESHDAIRY", packs=6)
    assert (dairy.params["qty"], dairy.cost_cents, dairy.tier) == (6000, 5700, "T3")  # over the $50 manager limit
    assert f"{po_params(dairy).eta_biz:%Y-%m-%d %H:%M}" == "2026-11-08 07:00"  # next day 07:00
    for staff, lang in (("STF_CARLOS", "es"), ("STF_ARJUN", "te"), ("STF_MARIA", "es")):  # Maria: the pickup
        notice = _find(report, "notify_staff", staff_id=staff)
        assert (notice.params["language"], notice.tier) == (lang, "T1")
    assert _find(report, "adjust_quote_time", station_id="STN_CURRY", minutes=5).tier == "T1"


def test_baseline_dinner_rush(i3: tuple[situation.Situation, SituationReport]) -> None:
    """I5: the playbook 86s all three paneer dishes, orders Patel 4 kg and notifies Carlos and Arjun."""
    _, report = i3
    by_id = {c.id: c for c in report.candidates}
    chosen = [by_id[action.candidate_id] for action in baseline.plan(report).actions]
    assert report.baseline_candidate_ids == [c.id for c in chosen]

    def targets(action: str) -> list[str]:
        return [c.target for c in chosen if c.action_type == action]

    assert sorted(targets("set_item_availability")) == ["PALAK_PNR", "PNR_BM", "PNR_TIKKA"]
    [po] = [c for c in chosen if c.action_type == "create_purchase_order"]
    assert (po.params["supplier_id"], po.params["qty"], po.cost_cents) == ("SUP_PATEL", 4000, 4400)
    assert targets("notify_staff") == ["STF_CARLOS", "STF_ARJUN"]
    assert len(targets("suggest_alternative")) == 2 and not targets("adjust_quote_time")


def test_sim_keep_pnr_bm(i3: tuple[situation.Situation, SituationReport]) -> None:
    """I6: "86 PNR_TIKKA + PALAK_PNR, keep PNR_BM, Patel 3 kg" shows no PNR_BM stockout before Patel arrives."""
    s, report = i3
    picked = [_find(report, "set_item_availability", item_code="PNR_TIKKA"),
              _find(report, "set_item_availability", item_code="PALAK_PNR"),
              _find(report, "create_purchase_order", supplier_id="SUP_PATEL", packs=3)]
    steps = [PlanStep(idx=i, candidate_id=c.id, action_type=c.action_type, target=c.target, params=c.params,
                      tier=c.tier, decision="pending") for i, c in enumerate(picked)]
    sim = simulate.dry_run(simulate.state_for(s), steps, tz=s.tz)
    lands = ms_from_dt(po_params(picked[2]).eta_biz)
    assert sim.stockout_biz_with is None or ms_from_dt(sim.stockout_biz_with) >= lands
    assert sim.stockout_biz_without is not None and ms_from_dt(sim.stockout_biz_without) <= lands  # doing nothing
    assert (sim.open_orders_protected, sim.po_cost_cents) == (sim.open_orders_total, 3300)
    assert sim.expected_lost_with_x10 < sim.expected_lost_without_x10 and sim.expected_redirected_x10 > 0
