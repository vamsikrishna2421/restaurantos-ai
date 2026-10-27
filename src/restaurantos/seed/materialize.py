"""Writes the Spice Route template and the ``live`` and ``demo`` tenants (CONTRACTS_SEED §1, CONTRACTS_RUNTIME §10.2).

``materialize_template(tx)`` runs in STARTUP_SYNC through ``startup(c)``, before handlers subscribe and before any
traffic. It is idempotent by ``seed_hash``: a tenant row already stamped with the current hash is left alone. A
missing tenant is created; one stamped with an older hash is rebuilt, which deletes its state (menu and stock rows,
orders, tickets, POs, notices, tasks, offsets, incidents, plans, approvals, corrections, ledger and events) but keeps
its row, so ``last_seq`` stays monotonic, and restarts its ledger at genesis. ``live`` and ``demo`` then get the
``quiet`` scenario and a ``scenario.loaded`` event, so every client resyncs. The template rows are never served;
sandboxes are cloned from them by ``store.sandbox``. A seed change therefore resets ``live`` and ``demo``: calls,
consents, feedback, turns, tokens and handler cursors are kept.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Row, Table, delete, insert, select, update

from restaurantos.contracts.common import (
    DEMO_TENANT,
    LIVE_TENANT,
    TEMPLATE_TENANT,
    SpendPool,
    TenantClockSpec,
    TenantFlags,
    TenantKind,
    biz_from_ms,
)
from restaurantos.contracts.errors import NotWired
from restaurantos.seed import scenario
from restaurantos.seed.loader import SeedData, load_seed
from restaurantos.seed.scenario import SeedTx, real_now_ms
from restaurantos.store import tables as t

log = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64  # ledger_head of an empty ledger (CONTRACTS_RUNTIME §9)
SEED_ACTOR = "system"
# (tenant id, kind, spend pool); live and demo start in the quiet scenario.
SEEDED_TENANTS: tuple[tuple[str, TenantKind, SpendPool], ...] = (
    (TEMPLATE_TENANT, "template", "admin"),
    (LIVE_TENANT, "live", "public"),
    (DEMO_TENANT, "demo", "admin"),
)
CONFIG_TABLES: tuple[Table, ...] = (
    t.stations, t.staff, t.modifiers, t.menu_items, t.ingredients, t.recipe_lines, t.suppliers,
)
STATE_TABLES: tuple[Table, ...] = (
    *CONFIG_TABLES, t.orders, t.order_lines, t.tickets, t.purchase_orders, t.staff_notices, t.staff_tasks,
    t.quote_offsets, t.incidents, t.plans, t.approvals, t.corrections, t.ledger, t.events,
)


class _Container(Protocol):
    store: Any  # StorePort: .write() -> AsyncContextManager[Tx]
    clock: Any  # ClockPort: .now_real_ms()


async def startup(c: _Container) -> None:
    """STARTUP_SYNC entry point (CONTRACTS_API §7): materialize inside one ``store.write()`` transaction."""
    if getattr(c, "store", None) is None:
        raise NotWired("store")
    async with c.store.write() as tx:
        wrote = await materialize_template(tx, now_real_ms=c.clock.now_real_ms())
    log.info("seed %s: %s", TEMPLATE_TENANT, "materialized" if wrote else "up to date")


async def materialize_template(tx: SeedTx, *, now_real_ms: int | None = None, root: Path | None = None) -> bool:
    """Write the template, ``live`` and ``demo`` tenants unless already at the current ``seed_hash``.

    Returns True when anything was written. ``now_real_ms`` stamps ``created_ms``/``last_active_ms`` and the order
    rows (default: the installed ``IdGen``'s clock); ``root`` points at another config directory (tests).
    """
    seed = load_seed(root)
    now = real_now_ms(now_real_ms)
    tenant_ids = [tenant_id for tenant_id, _, _ in SEEDED_TENANTS]
    query = (select(t.tenants.c.id, t.tenants.c.seed_hash, t.tenants.c.menu_version)
             .where(t.tenants.c.id.in_(tenant_ids)))
    existing = {row.id: row for row in (await tx.conn.execute(query)).all()}
    wrote = False
    for tenant_id, kind, pool in SEEDED_TENANTS:
        row = existing.get(tenant_id)
        if row is not None and row.seed_hash == seed.seed_hash:
            continue
        await _write_tenant(tx, seed, tenant_id, kind, pool, row, now)
        if kind != "template":
            await scenario.apply(tx, tenant_id, "quiet", now_real_ms=now, root=root)
            biz_time = biz_from_ms(now, seed.tenant.timezone).isoformat(timespec="milliseconds")
            await tx.append_event(tenant_id, "scenario.loaded",
                                  {"tenant_id": tenant_id, "scenario": "quiet", "biz_time": biz_time}, SEED_ACTOR)
        wrote = True
    return wrote


async def _write_tenant(tx: SeedTx, seed: SeedData, tenant_id: str, kind: TenantKind, pool: SpendPool,
                        row: Row[Any] | None, now: int) -> None:
    conn = tx.conn
    values = {
        "name": seed.tenant.name, "template": TEMPLATE_TENANT, "kind": kind, "spend_pool": pool,
        "last_active_ms": now, "expires_ms": None, "caps_level": "default", "dtmf_code": None,
        "dtmf_armed_until_ms": None, "clock": TenantClockSpec().model_dump(mode="json"), "tax_bp": seed.tenant.tax_bp,
        "ledger_seq": 0, "ledger_head": GENESIS_HASH, "seed_hash": seed.seed_hash,
        "flags": TenantFlags().model_dump(mode="json"),
    }
    if row is None:
        await conn.execute(insert(t.tenants).values(id=tenant_id, created_ms=now, menu_version=seed.menu.menu_version,
                                                    last_seq=0, **values))
    else:  # rebuild in place: the row (and so last_seq) survives; menu_version keeps rising
        for table in STATE_TABLES:
            await conn.execute(delete(table).where(table.c.tenant_id == tenant_id))
        await conn.execute(update(t.tenants).where(t.tenants.c.id == tenant_id)
                           .values(menu_version=row.menu_version + 1, **values))
    for table, rows in config_rows(seed, tenant_id):
        await conn.execute(insert(table), rows)


def config_rows(seed: SeedData, tenant_id: str) -> list[tuple[Table, list[dict[str, Any]]]]:
    """The template's configuration rows for ``tenant_id``: menu, modifiers, stations, staff, stock, recipes and
    suppliers. Stock starts at par with nothing reserved; supplier notes start empty."""
    menu = seed.menu
    return [
        (t.stations, [{"tenant_id": tenant_id, "id": s.id, "names": s.names.model_dump(), "parallel_slots":
                       s.parallel_slots, "default_language": s.default_language, "sort": i}
                      for i, s in enumerate(menu.stations)]),
        (t.staff, [{"tenant_id": tenant_id, **person.model_dump(mode="json")} for person in seed.staff]),
        (t.modifiers, [{"tenant_id": tenant_id, "code": m.code, "labels": m.labels.model_dump(),
                        "aliases": list(m.aliases)} for m in menu.modifiers]),
        (t.menu_items, [{"tenant_id": tenant_id, **item.model_dump(mode="json", exclude={"allergens"}), "sort": i,
                         "unavailable_source": None} for i, item in enumerate(menu.items)]),
        (t.ingredients, [{"tenant_id": tenant_id, **ing.model_dump(mode="json")} for ing in seed.ingredients]),
        (t.recipe_lines, [{"tenant_id": tenant_id, **line.model_dump()} for line in seed.recipes]),
        (t.suppliers, [{"tenant_id": tenant_id, **s.model_dump(mode="json", exclude={"notes"}), "notes": []}
                       for s in seed.suppliers]),
    ]
