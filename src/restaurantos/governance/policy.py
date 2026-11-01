"""The GM policy (CONTRACTS §6, ARCHITECTURE §6.5): ``config/policy.yaml``, loaded once and hashed, and the tier of
every step.

- ``load_policy()`` reads the file once at startup (``RESTOS_POLICY_PATH`` overrides the location), validates it with
  pydantic (exactly the CONTRACTS §6 actions, every forbidden action listed, no duplicate YAML keys) and hashes the
  raw bytes: ``policy_hash = sha256(file bytes)``, which ``Ledger.append`` stamps into every row. Nothing re-reads
  the file at runtime; ``Policy`` is read-only and ``limits`` hands out a copy.
- ``Policy.classify(step, ctx)`` (sync) re-derives a step's tier from its action and params; the tier a candidate or
  the model claims is never trusted. T0/T1 are ``auto`` (``allow:policy``), T2 needs a manager and T3 an owner
  (``pending``), FORBIDDEN is ``blocked``:

  - ``set_item_availability``: T2, for an 86 and an un-86 alike;
  - ``suggest_alternative`` and ``flag_ticket_for_callback``: T1;
  - ``create_purchase_order``: T2; T3 above ``manager_po_limit_cents`` or for a supplier in ``ctx.new_supplier_ids``;
    FORBIDDEN when it would take the day's PO spend past ``po_daily_cap_cents``;
  - ``notify_staff``: T1 for the code-rendered action line; T2 when the report is tainted and the step carries a
    model draft. The executor still delivers the action line as T1 and shows the draft only once the step is
    approved (CONTRACTS §8.2 taint rule);
  - ``adjust_quote_time``: T1 up to ``quote_offset_auto_max_min``, T2 above it, FORBIDDEN above
    ``quote_offset_max_min``;
  - anything else (``refund``, ``change_price``, ...): FORBIDDEN.
- No seed data marks a supplier as new, so ``PolicyContext.new_supplier_ids`` stays empty unless a caller fills it;
  the T3 rule applies to whatever it lists. ``po_spent_today_cents`` gives the day's PO spend for ``PolicyContext``.
- Who decides (``check_approver``): T2 a manager, owner, judge or admin; T3 an owner, judge or admin; on ``live`` and
  ``demo`` only an admin (``human:<name>`` for a name in ``admin_tokens``). A session approver is
  ``human:<role>:<sid8>`` (``human:judge:<sid8>`` in a sandbox).

``reason`` in a ``TierDecision`` is a stable snake_case code (``over_manager_limit``, ``tainted_ai_draft``, ...).
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import model_validator
from sqlalchemy import func, select

from restaurantos.config import REPO_ROOT
from restaurantos.contracts.common import Model, TenantInfo, Tier, biz_from_ms, ms_from_dt
from restaurantos.contracts.errors import Forbidden
from restaurantos.contracts.gm import (
    ACTION_PARAMS,
    ACTION_TYPES,
    FORBIDDEN_ACTIONS,
    QUOTE_OFFSET_MAX_MIN,
    STAFF_MESSAGE_MAX_CHARS,
    AdjustQuoteTimeParams,
    Approver,
    CreatePurchaseOrderParams,
    ForbiddenAction,
    NotifyStaffParams,
    PlanStep,
    PolicyContext,
    PolicyLimits,
    SetItemAvailabilityParams,
    TierDecision,
)
from restaurantos.contracts.hashing import sha256_hex
from restaurantos.domain import detectors
from restaurantos.domain.load import effective_offset
from restaurantos.governance.digest import APPROVAL_TIERS
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.ports import PolicyPort, RepoPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

POLICY_ENV = "RESTOS_POLICY_PATH"
AUTHORIZER_POLICY = "allow:policy"
TIER_RANK: Mapping[str, int] = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "FORBIDDEN": 4}
AUTO_TIERS = frozenset({"T0", "T1"})
PO_SPEND_WINDOW_MS = 24 * 3_600_000
APPROVER_ROLES = frozenset({"manager", "owner", "judge", "admin"})  # T2 (CONTRACTS §6: manager or judge, and above)
OWNER_ROLES = frozenset({"owner", "judge", "admin"})  # T3
ADMIN_ONLY_KINDS = frozenset({"live", "demo"})
_ADMIN_ID = re.compile(r"human:([A-Za-z0-9_.-]{1,40})")
_SESSION_ID = re.compile(r"human:(manager|owner|judge):[A-Za-z0-9_-]{1,40}")
_PLAIN_REASONS: Mapping[str, str] = {"set_item_availability": "menu_availability",
                                     "suggest_alternative": "alternatives", "flag_ticket_for_callback": "callback_task"}


# ----- the file ---------------------------------------------------------------------------------------------------


class TierAuthorities(Model):
    """``tiers``, pinned to CONTRACTS §6: T1 is authorized by the policy itself, T2 by a manager, T3 by an owner."""

    T0: Literal["none"]
    T1: Literal["policy"]
    T2: Literal["manager"]
    T3: Literal["owner"]


class PurchaseOrderTiers(Model):
    default: Tier
    over_limit: Tier
    new_supplier: Tier


class NotifyStaffTiers(Model):
    default: Tier
    tainted_ai_draft: Tier


class QuoteTimeTiers(Model):
    default: Tier
    over_auto_max: Tier


class ActionTiers(Model):
    """Exactly the CONTRACTS §6 actions: an unknown key (``refund: T1``) is refused."""

    set_item_availability: Tier
    suggest_alternative: Tier
    create_purchase_order: PurchaseOrderTiers
    notify_staff: NotifyStaffTiers
    adjust_quote_time: QuoteTimeTiers
    flag_ticket_for_callback: Tier


if set(ActionTiers.model_fields) != set(ACTION_TYPES):  # a new action in the contracts needs a policy entry
    raise RuntimeError("governance.policy.ActionTiers is out of step with contracts.gm.ACTION_TYPES")


class PolicyFile(Model):
    policy_version: Literal[2]
    tiers: TierAuthorities
    limits: PolicyLimits
    actions: ActionTiers
    forbidden: list[ForbiddenAction]

    @model_validator(mode="after")
    def _sane(self) -> PolicyFile:
        missing = set(FORBIDDEN_ACTIONS) - set(self.forbidden)
        if missing:
            raise ValueError(f"forbidden must list every forbidden action; missing {sorted(missing)}")
        limits = self.limits.model_dump()
        if bad := sorted(k for k, v in limits.items() if isinstance(v, int) and v < 1):
            raise ValueError(f"policy limits must be positive: {bad}")
        if not math.isfinite(self.limits.gm_run_usd_cap) or self.limits.gm_run_usd_cap < 0:
            raise ValueError("gm_run_usd_cap must be a finite, non-negative dollar amount")
        if not self.limits.quote_offset_auto_max_min <= self.limits.quote_offset_max_min <= QUOTE_OFFSET_MAX_MIN:
            raise ValueError(f"quote offsets: auto max <= max <= {QUOTE_OFFSET_MAX_MIN} minutes")
        if self.limits.staff_message_max_chars > STAFF_MESSAGE_MAX_CHARS:
            raise ValueError(f"staff_message_max_chars cannot exceed {STAFF_MESSAGE_MAX_CHARS}")
        return self


class _StrictLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses duplicate keys: in a policy file the last one would silently win."""


def _mapping_without_duplicates(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    out: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in out:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        out[key] = loader.construct_object(value_node, deep=True)
    return out


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates)


def policy_path() -> Path:
    return Path(os.environ.get(POLICY_ENV) or REPO_ROOT / "config" / "policy.yaml")


def parse_policy(data: bytes) -> PolicyFile:
    """Validate the bytes of a policy file; ValueError (pydantic's ValidationError included) when malformed."""
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"policy.yaml is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError("policy.yaml must be a mapping")
    return PolicyFile.model_validate(raw)


def load_policy(path: Path | None = None) -> Policy:
    """Read, validate and hash the policy file once. FileNotFoundError when absent, ValueError when malformed."""
    path = path or policy_path()
    data = path.read_bytes()
    return Policy(parse_policy(data), sha256_hex(data), path)


def stricter(a: Tier, b: Tier) -> Tier:
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


# ----- the policy -------------------------------------------------------------------------------------------------


class Policy:
    """``PolicyPort``: the loaded file and its hash, immutable at runtime."""

    __slots__ = ("_file", "_hash", "_path")

    def __init__(self, file: PolicyFile, policy_hash: str, path: Path | None = None) -> None:
        self._file = file
        self._hash = policy_hash
        self._path = path

    @property
    def limits(self) -> PolicyLimits:
        return self._file.limits.model_copy()  # a copy: callers can never change the running policy

    @property
    def policy_hash(self) -> str:
        return self._hash

    @property
    def version(self) -> int:
        return self._file.policy_version

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def forbidden(self) -> frozenset[str]:
        return frozenset(self._file.forbidden)

    def needs_role(self, tier: Tier) -> Literal["manager", "owner"] | None:
        """The role a tier waits for: T2 and T3 per ``tiers``; None for auto and forbidden tiers."""
        return getattr(self._file.tiers, tier) if tier in APPROVAL_TIERS else None

    def classify(self, step: PlanStep, ctx: PolicyContext) -> TierDecision:
        # The validator keeps a model draft on the step (CONTRACTS §8.2); the params may carry it too: either counts.
        return self.classify_action(step.action_type, step.params, ctx, draft=bool(step.ai_draft))

    def classify_action(self, action_type: str, params: Mapping[str, Any], ctx: PolicyContext | None = None, *,
                        draft: bool = False) -> TierDecision:
        """The tier and decision for one action; ``draft`` says a model draft comes with it (besides
        ``params.ai_draft``). ValueError when ``params`` do not fit the action."""
        ctx = ctx or PolicyContext()
        if action_type in self._file.forbidden or action_type not in ACTION_PARAMS:
            return self._decision("FORBIDDEN", "forbidden_action")
        typed = ACTION_PARAMS[action_type].model_validate(dict(params))
        actions, limits = self._file.actions, self._file.limits
        if isinstance(typed, CreatePurchaseOrderParams):
            if ctx.po_spent_today_cents + typed.cost_cents > limits.po_daily_cap_cents:
                return self._decision("FORBIDDEN", "po_daily_cap")
            po, tier, reasons = actions.create_purchase_order, actions.create_purchase_order.default, []
            if typed.supplier_id in ctx.new_supplier_ids:
                tier, reasons = stricter(tier, po.new_supplier), [*reasons, "new_supplier"]
            if typed.cost_cents > limits.manager_po_limit_cents:
                tier, reasons = stricter(tier, po.over_limit), [*reasons, "over_manager_limit"]
            return self._decision(tier, ",".join(reasons) or "within_manager_limit")
        if isinstance(typed, NotifyStaffParams):
            if ctx.tainted and (typed.ai_draft or draft):
                return self._decision(actions.notify_staff.tainted_ai_draft, "tainted_ai_draft")
            return self._decision(actions.notify_staff.default, "action_line")
        if isinstance(typed, AdjustQuoteTimeParams):
            if typed.minutes > limits.quote_offset_max_min:
                return self._decision("FORBIDDEN", "over_quote_offset_max")
            if typed.minutes > limits.quote_offset_auto_max_min:
                return self._decision(actions.adjust_quote_time.over_auto_max, "over_auto_max")
            return self._decision(actions.adjust_quote_time.default, "within_auto_max")
        return self._decision(getattr(actions, action_type), _PLAIN_REASONS[action_type])

    def apply(self, step: PlanStep, ctx: PolicyContext) -> PlanStep:
        """``step`` with the policy's tier and decision; T1 is authorized ``allow:policy`` at once."""
        d = self.classify(step, ctx)
        return step.model_copy(update={"tier": d.tier, "decision": d.decision,
                                       "authorizer": AUTHORIZER_POLICY if d.decision == "auto" else None})

    def review_payload(self, plan_id: str, step: PlanStep, decision: TierDecision) -> dict[str, Any]:
        """A REVIEW ledger payload for the policy check of one step (CONTRACTS_RUNTIME §9)."""
        return {"plan_id": plan_id, "idx": step.idx, "check": "policy",
                "result": "ok" if decision.tier == step.tier else "tiered", "reason": decision.reason,
                "tier": decision.tier}

    def _decision(self, tier: Tier, reason: str) -> TierDecision:
        if tier == "FORBIDDEN":
            return TierDecision(tier=tier, decision="blocked", reason=reason)
        if tier in AUTO_TIERS:
            return TierDecision(tier=tier, decision="auto", reason=reason)
        return TierDecision(tier=tier, decision="pending", reason=reason, needs_role=self.needs_role(tier))


# ----- who decides ------------------------------------------------------------------------------------------------


def check_approver(approver: Approver, tenant: TenantInfo, admin_names: Iterable[str]) -> None:
    """Forbidden (403) unless ``approver`` may decide plans of ``tenant``. A T3 step additionally needs a role in
    ``OWNER_ROLES``; the caller checks that once it knows the selected steps."""
    if tenant.kind == "template":
        raise Forbidden("The template tenant is never served", code="wrong_tenant")
    if approver.role not in APPROVER_ROLES:
        raise Forbidden(f"A {approver.role} cannot decide GM plans", details={"needs": "manager"})
    if approver.role == "admin":
        match = _ADMIN_ID.fullmatch(approver.approver_id)
        if match is None or match.group(1) not in set(admin_names):
            raise Forbidden("Unknown admin", details={"admin_required": True})
        return
    if tenant.kind in ADMIN_ONLY_KINDS:
        raise Forbidden("On live and demo only an admin decides", details={"admin_required": True})
    match = _SESSION_ID.fullmatch(approver.approver_id)
    if match is None or match.group(1) != approver.role:
        raise Forbidden("The approver id must be human:<role>:<sid8>", details={"reason": "approver_id"})


# ----- context ----------------------------------------------------------------------------------------------------


async def po_spent_today_cents(conn: AsyncConnection, tenant_id: str, now_real_ms: int) -> int:
    """Cents of the tenant's POs that are not cancelled and were created in the last 24 real hours: the spend that
    ``po_daily_cap_cents`` bounds. A PO row records its creation in real time only, hence a rolling window; sandboxes
    live under a day and ``live`` resets nightly, so in practice it is the business day's PO spend."""
    po = t.purchase_orders
    total = (await conn.execute(select(func.coalesce(func.sum(po.c.cost_cents), 0)).where(
        po.c.tenant_id == tenant_id, po.c.status != "cancelled",
        po.c.created_ms > now_real_ms - PO_SPEND_WINDOW_MS))).scalar_one()
    return int(total)


async def policy_context(conn: AsyncConnection, tenant_id: str, *, now_real_ms: int, tainted: bool = False,
                         new_supplier_ids: Iterable[str] = ()) -> PolicyContext:
    """The ``PolicyContext`` for classifying a tenant's steps now (``conn`` from ``store.read()`` or ``tx.conn``)."""
    return PolicyContext(tainted=tainted, po_spent_today_cents=await po_spent_today_cents(conn, tenant_id, now_real_ms),
                         new_supplier_ids=sorted(set(new_supplier_ids)))


# ----- viability --------------------------------------------------------------------------------------------------


async def viability_problems(conn: AsyncConnection, policy: PolicyPort, repo: RepoPort, steps: Sequence[PlanStep], *,
                             tenant_id: str, incident_id: str, ingredient_id: str, tainted: bool, now_biz_ms: int,
                             now_real_ms: int, tz: str) -> list[str]:
    """Why ``steps`` (in order) could not run as approved now, empty when they can: a stricter tier than stored (the
    day's PO cap included, counting the earlier steps' POs), a supplier that no longer carries the ingredient or
    whose same-day cutoff (business time) has passed, staff off shift, a missing dish, too little free stock for an
    un-86 (< 2 × the largest serving of ``ingredient_id``), or a station whose resulting quote offset would exceed
    ``quote_offset_max_min``: the largest of other incidents' active offsets there and this step's, since offsets never
    stack (CONTRACTS_RUNTIME §9, ``domain.load.effective_offset``). The drift check of ``Approvals.decide`` runs it;
    so can executor preconditions."""
    spent = await po_spent_today_cents(conn, tenant_id, now_real_ms)
    suppliers = {s.id: s for s in await repo.suppliers(conn, tenant_id)}
    on_shift = {s.id for s in await repo.staff(conn, tenant_id, on_shift=True)}
    today = biz_from_ms(now_biz_ms, tz).date()
    problems: list[str] = []
    for step in steps:
        fresh = policy.classify(step, PolicyContext(tainted=tainted, po_spent_today_cents=spent))
        if TIER_RANK[fresh.tier] > TIER_RANK[step.tier]:
            problems.append(f"step {step.idx}: now {fresh.tier} ({fresh.reason})")
        p, why = step.params_model(), None
        if isinstance(p, CreatePurchaseOrderParams):
            spent += p.cost_cents
            supplier = suppliers.get(p.supplier_id)
            if supplier is None or not any(item.ingredient_id == p.ingredient_id for item in supplier.catalog):
                why = "supplier no longer carries it"
            elif biz_from_ms(ms_from_dt(p.eta_biz), tz).date() == today and \
                    detectors.viable(supplier, p.ingredient_id, now_biz_ms, tz=tz) is None:
                why = "supplier cutoff passed"
        elif isinstance(p, NotifyStaffParams) and p.staff_id not in on_shift:
            why = "staff off shift"
        elif isinstance(p, SetItemAvailabilityParams):
            why = await _dish_problem(conn, repo, tenant_id, ingredient_id, p)
        elif isinstance(p, AdjustQuoteTimeParams):
            others = [o for o in await repo.active_offsets(conn, tenant_id, now_biz_ms) if o.incident_id != incident_id]
            # this step's station, or every station some other offset reaches when it is aimed at all of them (None)
            stations = {p.station_id} if p.station_id is not None else {o.station_id or "" for o in others}
            peak = max((effective_offset(others, station, now_biz_ms) for station in stations), default=0)
            if max(peak, p.minutes) > policy.limits.quote_offset_max_min:
                why = "quote offsets over the cap"
        if why is not None:
            problems.append(f"step {step.idx}: {why}")
    return problems


async def _dish_problem(conn: AsyncConnection, repo: RepoPort, tenant_id: str, ingredient_id: str,
                        p: SetItemAvailabilityParams) -> str | None:
    mi = t.menu_items
    if (await conn.execute(select(mi.c.code).where(mi.c.tenant_id == tenant_id, mi.c.code == p.item_code))).first() \
            is None:
        return "dish gone"
    if not p.available:
        return None
    stock = next((i for i in await repo.ingredients(conn, tenant_id) if i.id == ingredient_id), None)
    largest = detectors.largest_serving(ingredient_id, await repo.approved_recipes(conn, tenant_id))
    if stock is None or stock.on_hand - stock.reserved < detectors.DEFAULT_POLICY.min_servings * largest:
        return "not enough free stock to un-86"
    return None


def provide(c: Container) -> None:
    """PROVIDERS entry (CONTRACTS_API §7): sets ``c.policy`` only."""
    policy = load_policy()
    c.policy = policy
    log.info("policy v%d loaded from %s (policy_hash %s)", policy.version, policy.path, policy.policy_hash)
