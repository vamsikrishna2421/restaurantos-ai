"""The GM tool registry (CONTRACTS §6, ARCHITECTURE §6.8): the six actions the GM can express, and nothing else.

Every tool works inside the caller's write transaction and never opens one of its own (the store's write lock is not
re-entrant); nothing here awaits Meta, TTS, the network or an ack:

- ``check(conn, ctx, step)``: what ``policy.viability_problems`` leaves to the tool: a skip (``already``, the dish is
  in the requested state) or a failed precondition (a dish, order or alternative gone, a PO made for this step);
- ``apply(tx, ctx, step)``: the EFFECT and its events; returns a float-free ``result`` for the EFFECT ledger row;
- ``undo(tx, ctx, step, result)``: restore, retract, cancel (409 once the supplier acked the PO), deactivate or
  clear; a no-op for state someone else changed since.

| action | EFFECT | undo |
|---|---|---|
| set_item_availability | menu row (source ``plan``), ``menu_version`` + 1, menu.availability_changed | previous state |
| suggest_alternative | ``menu_items.alternatives``, menu.alternatives_changed | previous lists |
| create_purchase_order | PO ``sent`` to the sandbox supplier inbox; a pickup supplier adds a FOH task | cancel |
| notify_staff | a KDS notice at the person's station | retract |
| adjust_quote_time | the incident's one active offset per station: offsets never stack | deactivate |
| flag_ticket_for_callback | a ``callback`` task: a human calls the guest, the AI never does | clear |

VERIFY read-backs and a decided draft live in ``governance.verify``, crash-resume probes in ``governance.resume``.
**Taint rule** (CONTRACTS §8.2): a model draft reaches the KDS only on a T1 step of an untainted report (``shown``)
or once a human approved its T2 step (``approved``); until then the notice carries the action line alone
(``pending_approval``), and a T1 step of a tainted (or unknown) report never shows its draft (``none``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import insert, select, update

from restaurantos.contracts import events as ev
from restaurantos.contracts.common import DEFAULT_TZ, Model, biz_from_ms, ms_from_dt, utc_from_ms
from restaurantos.contracts.errors import Conflict
from restaurantos.contracts.gm import (
    AdjustQuoteTimeParams,
    CreatePurchaseOrderParams,
    FlagTicketForCallbackParams,
    NotifyStaffParams,
    PlanStep,
    SetItemAvailabilityParams,
    SuggestAlternativeParams,
)
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.inventory import PurchaseOrder
from restaurantos.contracts.kitchen import QuoteOffset, StaffNotice, StaffTask
from restaurantos.domain.availability import DEFAULT_UNAVAILABLE_REASON
from restaurantos.gm import incoming as on_order
from restaurantos.gm.fmt import quantity, short_name
from restaurantos.governance.approvals import load_incident
from restaurantos.governance.digest import APPROVAL_TIERS
from restaurantos.governance.policy import viability_problems
from restaurantos.services.kitchen import notice_from_row, task_from_row
from restaurantos.store import repo
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import PolicyPort, TxPort

log = logging.getLogger(__name__)

EXECUTOR = "executor"
OPEN_TASK = ("open", "blocked")
Result = dict[str, Any]


class ToolError(Exception):
    """An effect that cannot happen as asked: its transaction rolls back and the executor fails the step closed."""


@dataclass(frozen=True, slots=True)
class ToolCtx:
    """The step's plan and incident, both clocks, and the facts the tools need beyond the params."""

    tenant_id: str
    plan_id: str
    incident_id: str
    idx: int
    now_biz_ms: int
    now_real_ms: int
    tz: str = DEFAULT_TZ
    ingredient_id: str | None = None
    tainted: bool = True  # unknown counts as tainted: a draft then waits for a human
    actor: str = EXECUTOR


@dataclass(frozen=True, slots=True)
class Check:
    skip: str | None = None  # nothing to do (``already``)
    fail: str | None = None  # a precondition that does not hold


def params_of[P: Model](step: PlanStep, kind: type[P]) -> P:
    params = step.params_model()
    if not isinstance(params, kind):
        raise ToolError(f"step {step.idx} is {step.action_type}, not a {kind.__name__}")
    return params


async def one(conn: AsyncConnection, query: Any) -> RowMapping | None:
    return (await conn.execute(query)).mappings().one_or_none()


async def by_id(conn: AsyncConnection, table: Any, ctx: ToolCtx, row_id: str | None) -> RowMapping | None:
    """The tenant's row ``row_id`` of ``table`` (a notice, task, offset or PO); None without an id."""
    return await one(conn, select(table).where(table.c.tenant_id == ctx.tenant_id, table.c.id == row_id)) \
        if row_id else None


def draft_of(step: PlanStep) -> tuple[str | None, str | None]:
    """A notify step's model draft: the params' copy (bound by the approval digest), else the step's own."""
    return step.params.get("ai_draft") or step.ai_draft, step.params.get("ai_draft_en") or step.ai_draft_en


def draft_status(step: PlanStep, *, tainted: bool) -> str:
    """The notice's ``ai_draft_status`` for ``step`` now (the taint rule in the module docstring)."""
    if not draft_of(step)[0]:
        return "none"
    if step.tier in APPROVAL_TIERS:
        return "approved" if step.decision == "approved" else "pending_approval"
    return "none" if tainted else "shown"


class Tool:
    action_type: ClassVar[str]

    async def check(self, conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Check:
        return Check()

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        raise NotImplementedError

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        raise NotImplementedError


# ----- the menu ---------------------------------------------------------------------------------------------------


async def menu_rows(conn: AsyncConnection, ctx: ToolCtx, codes: list[str]) -> dict[str, RowMapping]:
    mi = t.menu_items
    rows = await conn.execute(select(mi.c.code, mi.c.available, mi.c.alternatives, mi.c.unavailable_reason,
                                     mi.c.unavailable_source).where(mi.c.tenant_id == ctx.tenant_id,
                                                                    mi.c.code.in_(codes)))
    return {row["code"]: row for row in rows.mappings()}


async def menu_row(conn: AsyncConnection, ctx: ToolCtx, code: str) -> RowMapping | None:
    return (await menu_rows(conn, ctx, [code])).get(code)


async def _set_available(tx: TxPort, ctx: ToolCtx, code: str, available: bool, reason: str | None = None,
                         source: str | None = None) -> int:
    """The menu row, ``menu_version`` + 1 and ``menu.availability_changed`` (source ``plan``); returns the version."""
    tn, mi = t.tenants, t.menu_items
    version = (await tx.conn.execute(select(tn.c.menu_version).where(tn.c.id == ctx.tenant_id))).scalar_one() + 1
    reason = None if available else reason or DEFAULT_UNAVAILABLE_REASON
    await tx.conn.execute(update(mi).where(mi.c.tenant_id == ctx.tenant_id, mi.c.code == code).values(
        available=available, unavailable_reason=reason, unavailable_source=None if available else source or "plan"))
    await tx.conn.execute(update(tn).where(tn.c.id == ctx.tenant_id).values(menu_version=version))
    await tx.append_event(ctx.tenant_id, "menu.availability_changed", ev.MenuAvailabilityChangedPayload(
        item_code=code, available=available, reason=reason or "", source="plan", plan_id=ctx.plan_id,
        menu_version=version), ctx.actor, correlation_id=ctx.plan_id)
    return version


class SetItemAvailability(Tool):
    action_type = "set_item_availability"

    async def check(self, conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Check:
        p = params_of(step, SetItemAvailabilityParams)
        row = await menu_row(conn, ctx, p.item_code)
        if row is None:
            return Check(fail="dish gone")
        return Check(skip="already") if bool(row["available"]) == p.available else Check()

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        p = params_of(step, SetItemAvailabilityParams)
        row = await menu_row(tx.conn, ctx, p.item_code)
        if row is None:
            raise ToolError(f"no dish {p.item_code} on the menu")
        previous = {"available": bool(row["available"]), "reason": row["unavailable_reason"],
                    "source": row["unavailable_source"]}
        version = await _set_available(tx, ctx, p.item_code, p.available)
        return {"item_code": p.item_code, "available": p.available, "menu_version": version, "previous": previous}

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        p = params_of(step, SetItemAvailabilityParams)
        row = await menu_row(tx.conn, ctx, p.item_code)
        if row is None or bool(row["available"]) != p.available:  # gone, or changed back by someone since
            return {"item_code": p.item_code, "already": True}
        previous = result.get("previous") or {}
        back = bool(previous.get("available", not p.available))
        version = await _set_available(tx, ctx, p.item_code, back, previous.get("reason"), previous.get("source"))
        return {"item_code": p.item_code, "available": back, "menu_version": version}


class SuggestAlternative(Tool):
    action_type = "suggest_alternative"

    async def check(self, conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Check:
        p = params_of(step, SuggestAlternativeParams)
        rows = await menu_rows(conn, ctx, [*p.for_item_codes, *p.alternative_codes])
        if any(code not in rows for code in p.for_item_codes):
            return Check(fail="dish gone")
        if any(code not in rows or not rows[code]["available"] for code in p.alternative_codes):
            return Check(fail="alternative unavailable")
        return Check()

    @staticmethod
    async def _set(tx: TxPort, ctx: ToolCtx, codes: list[str], alternatives: list[str]) -> None:
        mi = t.menu_items
        await tx.conn.execute(update(mi).where(mi.c.tenant_id == ctx.tenant_id, mi.c.code.in_(codes))
                              .values(alternatives=list(alternatives)))
        await tx.append_event(ctx.tenant_id, "menu.alternatives_changed", ev.MenuAlternativesChangedPayload(
            for_item_codes=codes, alternative_codes=list(alternatives), plan_id=ctx.plan_id), ctx.actor,
            correlation_id=ctx.plan_id)

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        p = params_of(step, SuggestAlternativeParams)
        rows = await menu_rows(tx.conn, ctx, p.for_item_codes)
        if missing := [code for code in p.for_item_codes if code not in rows]:
            raise ToolError(f"no dish {', '.join(missing)} on the menu")
        await self._set(tx, ctx, p.for_item_codes, p.alternative_codes)
        return {"previous": {code: list(rows[code]["alternatives"] or []) for code in p.for_item_codes}}

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        p = params_of(step, SuggestAlternativeParams)
        previous = result.get("previous") or {}
        rows = await menu_rows(tx.conn, ctx, p.for_item_codes)
        groups: dict[tuple[str, ...], list[str]] = {}
        for code in p.for_item_codes:  # only lists still as the step set them: a later change stands
            if code in rows and list(rows[code]["alternatives"] or []) == p.alternative_codes:
                groups.setdefault(tuple(previous.get(code) or ()), []).append(code)
        for back, codes in groups.items():
            await self._set(tx, ctx, codes, list(back))
        return {"restored": {code: list(back) for back, codes in groups.items() for code in codes}}


# ----- purchasing -------------------------------------------------------------------------------------------------


async def po_row(conn: AsyncConnection, ctx: ToolCtx, po_id: str | None = None) -> RowMapping | None:
    """The PO ``po_id``, or the one this step made (UNIQUE tenant, plan, step: its idempotency key)."""
    if po_id:
        return await by_id(conn, t.purchase_orders, ctx, po_id)
    po = t.purchase_orders
    return await one(conn, select(po).where(po.c.tenant_id == ctx.tenant_id, po.c.plan_id == ctx.plan_id,
                                            po.c.step_idx == ctx.idx))


async def _task(tx: TxPort, ctx: ToolCtx, kind: str, note: str, *, order_id: str | None = None,
                po_id: str | None = None) -> str:
    task = StaffTask(id=new_id("tsk"), kind=kind, order_id=order_id, po_id=po_id, note=note, status="open",  # type: ignore[arg-type]
                     plan_id=ctx.plan_id, created_at=utc_from_ms(ctx.now_real_ms))
    await tx.conn.execute(insert(t.staff_tasks).values(
        tenant_id=ctx.tenant_id, **task.model_dump(mode="json", exclude={"created_at"}), created_ms=ctx.now_real_ms))
    await tx.append_event(ctx.tenant_id, "task.updated", ev.TaskUpdatedPayload(task=task), ctx.actor,
                          correlation_id=ctx.plan_id)
    return task.id


async def _clear_task(tx: TxPort, ctx: ToolCtx, row: RowMapping | None) -> str | None:
    """Clear an open or blocked task; its status afterwards (None when there is none)."""
    if row is None or row["status"] not in OPEN_TASK:
        return None if row is None else str(row["status"])
    tk = t.staff_tasks
    await tx.conn.execute(update(tk).where(tk.c.tenant_id == ctx.tenant_id, tk.c.id == row["id"])
                          .values(status="cleared"))
    await tx.append_event(ctx.tenant_id, "task.updated", ev.TaskUpdatedPayload(
        task=task_from_row({**row, "status": "cleared"})), ctx.actor, correlation_id=ctx.plan_id)
    return "cleared"


class CreatePurchaseOrder(Tool):
    action_type = "create_purchase_order"

    async def check(self, conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Check:
        return Check(fail="a PO exists for this step") if await po_row(conn, ctx) is not None else Check()

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        p = params_of(step, CreatePurchaseOrderParams)
        if await po_row(tx.conn, ctx) is not None:
            raise ToolError("a PO exists for this step already")  # never a second one
        supplier = next((s for s in await repo.suppliers(tx.conn, ctx.tenant_id) if s.id == p.supplier_id), None)
        if supplier is None:
            raise ToolError(f"unknown supplier {p.supplier_id}")
        po = PurchaseOrder(id=new_id("po"), po_no=await repo.next_no(tx, ctx.tenant_id, "po"),
                           supplier_id=p.supplier_id, ingredient_id=p.ingredient_id, packs=p.packs, qty=p.qty,
                           cost_cents=p.cost_cents, status="sent", plan_id=ctx.plan_id, step_idx=ctx.idx,
                           created_at=utc_from_ms(ctx.now_real_ms), eta_biz=p.eta_biz)
        await tx.conn.execute(insert(t.purchase_orders).values(
            tenant_id=ctx.tenant_id, **po.model_dump(mode="json", exclude={"created_at", "eta_biz"}),
            created_ms=ctx.now_real_ms, eta_biz_ms=ms_from_dt(p.eta_biz)))
        await tx.append_event(ctx.tenant_id, "po.updated", ev.PoUpdatedPayload(po=po), ctx.actor,
                              correlation_id=ctx.plan_id)
        task_id = None
        if supplier.kind == "pickup":
            what = next((i for i in await repo.ingredients(tx.conn, ctx.tenant_id) if i.id == p.ingredient_id), None)
            amount = f"{quantity(p.qty, what.unit)} {what.names['en']}" if what else f"{p.qty} {p.ingredient_id}"
            eta = biz_from_ms(ms_from_dt(p.eta_biz), ctx.tz)
            task_id = await _task(tx, ctx, "pickup", f"Pick up {amount} at {short_name(supplier)}: PO #{po.po_no}, "
                                                     f"ready ≈{eta:%H:%M}", po_id=po.id)
        return {"po_id": po.id, "po_no": po.po_no, "task_id": task_id}

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        row = await po_row(tx.conn, ctx, result.get("po_id"))
        if row is None or row["status"] == "cancelled":
            return {"po_id": result.get("po_id"), "already": True}
        if row["status"] != "sent":
            raise Conflict("The supplier already has this purchase order: it can no longer be cancelled",
                           details={"reason": "po_acked", "po_id": row["id"], "status": row["status"]})
        po = t.purchase_orders
        await tx.conn.execute(update(po).where(po.c.tenant_id == ctx.tenant_id, po.c.id == row["id"])
                              .values(status="cancelled"))
        data = {key: row[key] for key in ("id", "po_no", "supplier_id", "ingredient_id", "packs", "qty",
                                          "cost_cents", "plan_id", "step_idx")}
        cancelled = PurchaseOrder(**data, status="cancelled", created_at=utc_from_ms(row["created_ms"]),
                                  eta_biz=biz_from_ms(row["eta_biz_ms"], ctx.tz))
        await tx.append_event(ctx.tenant_id, "po.updated", ev.PoUpdatedPayload(po=cancelled), ctx.actor,
                              correlation_id=ctx.plan_id)
        task = await _clear_task(tx, ctx, await by_id(tx.conn, t.staff_tasks, ctx, result.get("task_id")))
        return {"po_id": row["id"], "status": "cancelled", "task_status": task}


# ----- the kitchen ------------------------------------------------------------------------------------------------


async def save_notice(tx: TxPort, ctx: ToolCtx, row: Mapping[str, Any], **values: Any) -> StaffNotice:
    """Update a notice row and emit ``notice.updated``."""
    n = t.staff_notices
    await tx.conn.execute(update(n).where(n.c.tenant_id == ctx.tenant_id, n.c.id == row["id"]).values(values))
    notice = notice_from_row({**row, **values})
    await tx.append_event(ctx.tenant_id, "notice.updated", ev.NoticeUpdatedPayload(notice=notice), ctx.actor,
                          correlation_id=ctx.plan_id)
    return notice


async def notice_station(conn: AsyncConnection, ctx: ToolCtx, station_ids: list[str]) -> str | None:
    """Where a person's notice shows: the first of their stations that the incident's ingredient reaches, else their
    first station; None (every station) for someone without one."""
    if not station_ids or not ctx.ingredient_id:
        return station_ids[0] if station_ids else None
    mi, rl = t.menu_items, t.recipe_lines
    reached = set((await conn.execute(select(mi.c.station_id).join(
        rl, (rl.c.tenant_id == mi.c.tenant_id) & (rl.c.item_code == mi.c.code)).where(
        mi.c.tenant_id == ctx.tenant_id, rl.c.ingredient_id == ctx.ingredient_id))).scalars())
    return next((station for station in station_ids if station in reached), station_ids[0])


class NotifyStaff(Tool):
    action_type = "notify_staff"

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        p = params_of(step, NotifyStaffParams)
        person = next((s for s in await repo.staff(tx.conn, ctx.tenant_id, on_shift=False) if s.id == p.staff_id),
                      None)
        if person is None:
            raise ToolError(f"unknown staff {p.staff_id}")
        status = draft_status(step, tainted=ctx.tainted)
        if status == "none" and draft_of(step)[0]:
            log.warning("notice %s:%s: the report is tainted, so the model draft is withheld", ctx.plan_id, ctx.idx)
        draft, draft_en = draft_of(step) if status in ("shown", "approved") else (None, None)
        notice = StaffNotice(id=new_id("ntc"), staff_id=p.staff_id,
                             station_id=await notice_station(tx.conn, ctx, person.station_ids), message=p.message,
                             message_en=p.message_en, language=p.language, ai_draft=draft, ai_draft_en=draft_en,
                             ai_draft_status=status, plan_id=ctx.plan_id, step_idx=ctx.idx,  # type: ignore[arg-type]
                             status="sent", created_at=utc_from_ms(ctx.now_real_ms))
        await tx.conn.execute(insert(t.staff_notices).values(
            tenant_id=ctx.tenant_id, **notice.model_dump(mode="json", exclude={"created_at", "delivered_at",
                                                                               "seen_at"}),
            created_ms=ctx.now_real_ms, delivered_ms=None, seen_ms=None))
        await tx.append_event(ctx.tenant_id, "notice.updated", ev.NoticeUpdatedPayload(notice=notice), ctx.actor,
                              correlation_id=ctx.plan_id)
        return {"notice_id": notice.id, "station_id": notice.station_id, "ai_draft_status": status}

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        row = await by_id(tx.conn, t.staff_notices, ctx, result.get("notice_id"))
        if row is None or row["status"] == "retracted":
            return {"notice_id": result.get("notice_id"), "already": True}
        await save_notice(tx, ctx, row, status="retracted")
        return {"notice_id": row["id"], "status": "retracted"}


def same_station(column: Any, station_id: str | None) -> Any:
    return column.is_(None) if station_id is None else column == station_id


class AdjustQuoteTime(Tool):
    action_type = "adjust_quote_time"

    @staticmethod
    async def _deactivate(tx: TxPort, ctx: ToolCtx, row: Mapping[str, Any]) -> None:
        q = t.quote_offsets
        await tx.conn.execute(update(q).where(q.c.tenant_id == ctx.tenant_id, q.c.id == row["id"]).values(active=False))
        offset = QuoteOffset(id=row["id"], incident_id=row["incident_id"], station_id=row["station_id"],
                             minutes=row["minutes"], expires_biz=biz_from_ms(row["expires_biz_ms"], ctx.tz),
                             plan_id=row["plan_id"], active=False)
        await tx.append_event(ctx.tenant_id, "quote.offset_changed", ev.QuoteOffsetChangedPayload(offset=offset),
                              ctx.actor, correlation_id=ctx.plan_id)

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        p = params_of(step, AdjustQuoteTimeParams)
        q = t.quote_offsets
        replaced = []
        for row in (await tx.conn.execute(select(q).where(  # upsert the incident's one active offset there
                q.c.tenant_id == ctx.tenant_id, q.c.incident_id == ctx.incident_id,
                same_station(q.c.station_id, p.station_id), q.c.active.is_(True)))).mappings().all():
            await self._deactivate(tx, ctx, row)
            replaced.append(row["id"])
        expires = ctx.now_biz_ms + p.duration_min * 60_000
        offset = QuoteOffset(id=new_id("qo"), incident_id=ctx.incident_id, station_id=p.station_id,
                             minutes=p.minutes, expires_biz=biz_from_ms(expires, ctx.tz), plan_id=ctx.plan_id,
                             active=True)
        await tx.conn.execute(insert(q).values(tenant_id=ctx.tenant_id, expires_biz_ms=expires,
                                               **offset.model_dump(mode="json", exclude={"expires_biz"})))
        await tx.append_event(ctx.tenant_id, "quote.offset_changed", ev.QuoteOffsetChangedPayload(offset=offset),
                              ctx.actor, correlation_id=ctx.plan_id)
        return {"offset_id": offset.id, "replaced": replaced}

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        row = await by_id(tx.conn, t.quote_offsets, ctx, result.get("offset_id"))
        if row is None or not row["active"]:
            return {"offset_id": result.get("offset_id"), "already": True}
        await self._deactivate(tx, ctx, row)
        return {"offset_id": row["id"], "active": False}


class FlagTicketForCallback(Tool):
    action_type = "flag_ticket_for_callback"

    async def check(self, conn: AsyncConnection, ctx: ToolCtx, step: PlanStep) -> Check:
        p = params_of(step, FlagTicketForCallbackParams)
        o = t.orders
        status = (await conn.execute(select(o.c.status).where(o.c.tenant_id == ctx.tenant_id, o.c.id == p.order_id))
                  ).scalar_one_or_none()
        if status is None:
            return Check(fail="order gone")
        return Check(fail="order picked up") if status == "picked_up" else Check()

    async def apply(self, tx: TxPort, ctx: ToolCtx, step: PlanStep) -> Result:
        p = params_of(step, FlagTicketForCallbackParams)
        return {"task_id": await _task(tx, ctx, "callback", p.reason, order_id=p.order_id)}

    async def undo(self, tx: TxPort, ctx: ToolCtx, step: PlanStep, result: Mapping[str, Any]) -> Result:
        status = await _clear_task(tx, ctx, await by_id(tx.conn, t.staff_tasks, ctx, result.get("task_id")))
        return {"task_id": result.get("task_id"), "status": status}


# ----- the registry -----------------------------------------------------------------------------------------------

TOOLS: Mapping[str, Tool] = {tool.action_type: tool for tool in (
    SetItemAvailability(), SuggestAlternative(), CreatePurchaseOrder(), NotifyStaff(), AdjustQuoteTime(),
    FlagTicketForCallback())}


def tool_for(action_type: str) -> Tool:
    """The tool of ``action_type``. A forbidden or unknown action has none and cannot be expressed (ToolError)."""
    tool = TOOLS.get(action_type)
    if tool is None:
        raise ToolError(f"no tool for {action_type!r}: it cannot be expressed")
    return tool


async def precheck(conn: AsyncConnection, policy: PolicyPort, ctx: ToolCtx, step: PlanStep) -> Check:
    """CONTRACTS_RUNTIME §9 preconditions right before the EFFECT: the tool's own check, then the shared viability
    checks (supplier cutoff, the day's PO cap and a stricter tier, staff on shift, un-86 stock, the quote-offset cap),
    then for a same-day PO that a gap is still open once stock already on order is counted (``gm.incoming``): a PO is
    never placed for stock on its way. A model draft is not a precondition: the taint rule only keeps it off the KDS."""
    own = await tool_for(step.action_type).check(conn, ctx, step)
    if own.skip or own.fail:
        return own
    problems = await viability_problems(conn, policy, repo, [step], tenant_id=ctx.tenant_id,  # type: ignore[arg-type]
                                        incident_id=ctx.incident_id, ingredient_id=ctx.ingredient_id or "",
                                        tainted=False, now_biz_ms=ctx.now_biz_ms, now_real_ms=ctx.now_real_ms,
                                        tz=ctx.tz)
    if not problems and step.action_type == CreatePurchaseOrder.action_type and not await on_order.gap_open(
            conn, ctx.tenant_id, (await load_incident(conn, ctx.tenant_id, ctx.incident_id, ctx.tz))[0],
            params_of(step, CreatePurchaseOrderParams), now_biz_ms=ctx.now_biz_ms, limits=policy.limits, tz=ctx.tz):
        problems = ["po: stock already on order covers it"]
    return Check(fail=problems[0].split(": ", 1)[-1]) if problems else Check()
