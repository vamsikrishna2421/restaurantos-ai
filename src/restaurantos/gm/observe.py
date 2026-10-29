"""Observe re-plans (P22, P1-2; ARCHITECTURE §6.1-§6.2, D28): what a re-plan adds for the observation behind it.

``gm.trigger`` (P23b) re-opens an incident and re-plans it with an ``ObserveContext``; the report carries it as
``observe`` and the candidates adapt to it (``gm.candidates``):

- ``verify_failed``, a notice nobody saw within ``notice_seen_timeout_s``: **re-route** (``notify_staff``, T1). Each
  notice of the observed plan still unseen goes to the first other on-shift person with its recipient's role at the
  station it showed on (another cook at the curry station), else the first on-shift manager, else an owner. Two
  misses at once re-plan once, so every unseen notice of the plan counts, not only the observed step; one seen after
  the deadline (its step stays ``verify_failed``) does not. The notice moves: whoever missed it is offered none, so
  the new plan retracts it (CONTRACTS §6 replacement). With nobody to take it, it stays put: its recipient is offered
  it again, so the replacement carries it over.
- Every later re-plan of the incident (any trigger, TTL and ACT re-entry too) keeps those moves: a notice nobody saw
  that a replacement retracted is routed again by the same rule, so whoever took it is still offered it (carried
  over, not sent again) and whoever missed it is never notified again (§6.5).
- ``task_blocked``, FOH "Can't go" on a pickup: **supplier switch**. The PO candidates are recomputed without the
  blocked supplier (whose stock no longer counts as on order either: ``gm.incoming``). Tiers stay by cost, so when
  only a next-day or an over-limit order is left (``dinner_rush`` after 19:30: Fresh Dairy 6 kg, $57) the switch
  waits for the owner (T3).
- ``po_received``, "Received" in the supplier inbox: **un-86** (``set_item_availability(dish, true)``, T2) for each
  dish this incident 86'd that a plan still keeps off, once free ≥ 2 × the largest serving and every ingredient of the
  dish allows 2 servings (another shortage may need it off).

``read`` takes the facts a ``CandidateInputs`` lacks from the run's snapshot (the notices already moved, the unseen
notices, the dishes the incident's plans stopped); ``reroutes``, ``excluded`` and ``un86`` are pure. A notify carrying
a notice nobody saw opens its ``effect`` with ``REROUTES`` or ``KEEPS``: the playbook keeps those whatever the
recipient's role (``carries``), since dropping one retracts that notice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from restaurantos.domain import detectors
from restaurantos.domain.i18n_kitchen import station_name
from restaurantos.governance.ledger import verify_key
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.gm import Candidate, CandidateInputs, ObserveContext, ReportDish
    from restaurantos.contracts.kitchen import Staff

EFFECTED = frozenset({"effect_ok", "verified", "verify_pending", "verify_failed"})  # as governance.verify.EFFECTIVE
UNSEEN = frozenset({"sent", "delivered"})  # a notice row still on its screen that nobody has seen
REPLACED = "replaced_by_plan"  # governance.executor's reason for undoing a T1 effect the new plan did not repeat
ROUTE_UP = ("manager", "owner")  # who gets an unseen notice when no peer at its station can
REROUTES = "Re-routes the "  # a notify's effect when it carries notices other people missed
KEEPS = "Keeps the "  # ... when it carries its own recipient's unseen notice, which nobody else can take


@dataclass(frozen=True)
class Unseen:
    """A notice nobody saw in time: its recipient and the station it showed on (None: every station)."""

    staff_id: str
    station_id: str | None = None


@dataclass(frozen=True)
class Observed:
    """What the incident's observations add to a snapshot beyond a re-plan's ``ObserveContext``."""

    unseen: tuple[Unseen, ...] = ()  # verify_failed: the observed plan's notices still unseen, in step order
    stopped: tuple[str, ...] = ()  # po_received: dishes the incident 86'd that a plan still keeps off, oldest first
    moved: tuple[Unseen, ...] = ()  # any run: notices nobody saw that a replacement retracted, oldest first


NOTHING = Observed()
Plans = Sequence[tuple[str, Mapping[str, Any]]]  # (plan id, body) of the incident's plans, oldest first


async def read(conn: AsyncConnection, tenant_id: str, incident_id: str, observe: ObserveContext | None) -> Observed:
    """The facts a run for ``incident_id`` needs, read on ``conn`` (the run's ``store.read()`` or ``tx.conn``): the
    notices already moved for every run, and what ``observe`` itself needs; ``NOTHING`` when there are none."""
    p = t.plans
    plans: Plans = [(str(plan_id), body or {}) for plan_id, body in (await conn.execute(
        select(p.c.id, p.c.body).where(p.c.tenant_id == tenant_id, p.c.incident_id == incident_id)
        .order_by(p.c.created_ms, p.c.plan_no))).all()]
    unseen: tuple[Unseen, ...] = ()
    if observe is not None and observe.trigger == "verify_failed":
        unseen = await _unseen(conn, tenant_id, observe.plan_id, dict(plans).get(observe.plan_id, {}))
    stopped = await _stopped(conn, tenant_id, plans) if observe is not None and observe.trigger == "po_received" else ()
    return Observed(unseen=unseen, stopped=stopped, moved=await _moved(conn, tenant_id, plans))


def _notices(body: Mapping[str, Any]) -> list[tuple[int, str, str | None, str | None]]:
    """(idx, recipient, status, detail) of a plan body's notify steps, in step order."""
    steps = [s for s in body.get("steps", []) if s.get("action_type") == "notify_staff"]
    return sorted(((int(s["idx"]), str((s.get("params") or {}).get("staff_id") or s.get("target")), s.get("status"),
                    s.get("detail")) for s in steps), key=lambda step: step[0])


async def _shown(conn: AsyncConnection, tenant_id: str,
                 plan_ids: Iterable[str]) -> dict[tuple[str, int], tuple[str | None, str, int | None]]:
    """(plan id, step idx) → (station it showed on, status, seen_ms) of the plans' notice rows."""
    n = t.staff_notices
    rows = await conn.execute(select(n.c.plan_id, n.c.step_idx, n.c.station_id, n.c.status, n.c.seen_ms).where(
        n.c.tenant_id == tenant_id, n.c.plan_id.in_(list(plan_ids))))
    return {(plan_id, idx): (station, status, seen) for plan_id, idx, station, status, seen in rows.all()}


async def _unseen(conn: AsyncConnection, tenant_id: str, plan_id: str,
                  body: Mapping[str, Any]) -> tuple[Unseen, ...]:
    """The plan's notify steps the executor marked ``verify_failed`` whose notice is still unseen (sent or delivered),
    with the station it showed on. A Seen after the deadline leaves the step ``verify_failed``; its recipient has it."""
    steps = [(idx, staff) for idx, staff, status, _ in _notices(body) if status == "verify_failed"]
    shown = await _shown(conn, tenant_id, [plan_id]) if steps else {}
    return tuple(Unseen(staff, row[0]) for idx, staff in steps
                 if (row := shown.get((plan_id, idx))) is not None and row[1] in UNSEEN)


async def _moved(conn: AsyncConnection, tenant_id: str, plans: Plans) -> tuple[Unseen, ...]:
    """Notices nobody saw that a replacement retracted: notify steps ``undone`` for ``replaced_by_plan`` whose VERIFY
    ``seen`` failed and whose notice was never seen, even late. One retracted within its 60 s, or undone by hand, did
    not move."""
    undone = [(plan_id, idx, staff) for plan_id, body in plans for idx, staff, status, detail in _notices(body)
              if status == "undone" and detail == REPLACED]
    if not undone:
        return ()
    lg = t.ledger
    keys = {verify_key(plan_id, idx, "seen"): (plan_id, idx) for plan_id, idx, _ in undone}
    rows = await conn.execute(select(lg.c.idempotency_key, lg.c.payload).where(
        lg.c.tenant_id == tenant_id, lg.c.kind == "VERIFY", lg.c.idempotency_key.in_(list(keys))))
    failed = {keys[key] for key, payload in rows.all() if (payload or {}).get("status") == "failed"}
    shown = await _shown(conn, tenant_id, {plan_id for plan_id, _, _ in undone})
    return tuple(Unseen(staff, row[0]) for plan_id, idx, staff in undone
                 if (plan_id, idx) in failed and (row := shown.get((plan_id, idx))) is not None and row[2] is None)


async def _stopped(conn: AsyncConnection, tenant_id: str, plans: Plans) -> tuple[str, ...]:
    """Dishes the incident's plans 86'd, effect standing (not undone, not skipped as ``already``: a cook's own 86
    stays the kitchen's), less those a later step of them put back on sale, and those no plan keeps off any more
    (``menu_items.unavailable_source``: a manager put one back on the KDS, maybe the kitchen 86'd it again since)."""
    stopped: dict[str, None] = {}
    for _, body in plans:
        for step in body.get("steps", []):
            params = step.get("params") or {}
            if step.get("action_type") == "set_item_availability" and step.get("status") in EFFECTED:
                stopped.pop(str(params.get("item_code")), None)
                if params.get("available") is False:
                    stopped[str(params.get("item_code"))] = None
    if not stopped:
        return ()
    mi = t.menu_items
    off = set((await conn.execute(select(mi.c.code).where(
        mi.c.tenant_id == tenant_id, mi.c.code.in_(list(stopped)), mi.c.available.is_(False),
        mi.c.unavailable_source == "plan"))).scalars())
    return tuple(code for code in stopped if code in off)


def reroutes(observe: ObserveContext | None, observed: Observed,
             staff_on_shift: Sequence[Staff]) -> tuple[dict[str, tuple[Unseen, ...]], frozenset[str]]:
    """``(routes, away)`` for the notices nobody saw: those a replacement moved already (any run) and, in a
    ``verify_failed`` re-plan, the observed plan's unseen ones. ``routes``: recipient id → the notices they are
    offered, in staff order; nobody who missed one gets another, and an unseen notice nobody else can take stays with
    its own recipient. ``away``: whoever missed a notice that moved, offered none so nobody notifies them again."""
    live = observed.unseen if observe is not None and observe.trigger == "verify_failed" else ()
    notices = [(u, False) for u in observed.moved] + [(u, True) for u in live]
    on = [person for person in staff_on_shift if person.on_shift]
    missed = {u.staff_id for u, _ in notices}
    role = {person.id: person.role for person in on}  # someone gone off shift has no peers: up it goes
    routes: dict[str, list[Unseen]] = {}
    for u, on_screen in notices:
        peers = [p for p in on if p.id not in missed and p.role == role.get(u.staff_id)
                 and (u.station_id is None or u.station_id in p.station_ids)]
        up = [p for rank in ROUTE_UP for p in on if p.role == rank and p.id not in missed]
        if (to := next(iter(peers or up), None)) is not None:
            routes.setdefault(to.id, []).append(u)
        elif on_screen and u.staff_id in role:  # nobody else can take it: it stays put
            routes.setdefault(u.staff_id, []).append(u)
    stays = {person_id for person_id, kept in routes.items() if any(u.staff_id == person_id for u in kept)}
    return {p.id: tuple(routes[p.id]) for p in on if p.id in routes}, frozenset(missed - stays)


def describe(missed: Sequence[Unseen], recipient: str, inputs: CandidateInputs) -> str:
    """Why a notify carries notices nobody saw, for its ``effect``: "Re-routes the notice Carlos (Curry) has not seen
    in 60 s", or for the recipient's own, which nobody else can take, "Keeps the notice Lakshmi (every station) has
    not seen in 60 s where it is, as nobody else on shift can take it"."""
    menu, names = inputs.menu, {person.id: person.name for person in inputs.staff_on_shift}
    who = [f"{names.get(u.staff_id, u.staff_id)} "
           f"({'every station' if u.station_id is None else station_name(menu, u.station_id, 'en')})" for u in missed]
    listed = who[0] if len(who) == 1 else f"{', '.join(who[:-1])} and {who[-1]}"
    noun, verb, it, is_ = ("notices", "have", "them", "they are") if len(who) > 1 else ("notice", "has", "it", "it is")
    said = f"{noun} {listed} {verb} not seen in {inputs.policy.notice_seen_timeout_s} s"
    if all(u.staff_id == recipient for u in missed):
        return f"{KEEPS}{said} where {is_}, as nobody else on shift can take {it}"
    return f"{REROUTES}{said}"


def carries(candidate: Candidate) -> bool:
    """Whether a notify carries a notice nobody saw (re-routed, or kept where nobody else can take it): the playbook
    keeps it whatever the recipient's role, since dropping it would retract that notice."""
    return candidate.action_type == "notify_staff" and candidate.effect.startswith((REROUTES, KEEPS))


def excluded(observe: ObserveContext | None) -> frozenset[str]:
    """The suppliers PO candidates leave out: the one whose pickup FOH could not make (``task_blocked``)."""
    if observe is None or observe.trigger != "task_blocked" or observe.blocked_supplier_id is None:
        return frozenset()
    return frozenset({observe.blocked_supplier_id})


def un86(observe: ObserveContext | None, observed: Observed, dishes: Sequence[ReportDish], free: int,
         servings_free: Mapping[str, int]) -> list[str]:
    """Dishes to put back on sale (``po_received`` only): those this incident 86'd that are still off, once ``free``
    is at least 2 × the largest serving of a dish using the ingredient and ``servings_free`` (the stock of every
    ingredient of the dish) allows 2 servings of it."""
    if observe is None or observe.trigger != "po_received":
        return []
    least = detectors.DEFAULT_POLICY.min_servings
    largest = max((dish.qty_per_serving for dish in dishes), default=0)
    if not largest or free < least * largest:
        return []
    off = {dish.code for dish in dishes if not dish.available}
    return [code for code in observed.stopped if code in off and servings_free.get(code, 0) >= least]
