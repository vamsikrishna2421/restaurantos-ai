"""What the planner falls back on, and the rules that decide when (ARCHITECTURE §6.1, §6.3-§6.5; CONTRACTS §8.2).

- **Labels.** The playbook becomes the active ``playbook_fallback`` with its reason in front of its summary:
  ``NOBODY_WATCHING`` ("AI paused: nobody watching") or ``unavailable(detail)`` ("AI unavailable: …").
- **Watcher rule** (``watched``): AI runs only while a manager, owner, judge or admin client of the tenant was seen
  within ``gm_watch_window_s`` (``hub.last_seen``), or within ``gm_rush_window_min`` of a Start rush. Unattended
  sandboxes spend nothing.
- **Spend** (``call_ctx``, ``worst_call_usd``): the CONTRACTS_RUNTIME §10.9 scope of a GM run, and the worst case one
  planner call can bill (no cache hit, the GM route's whole output budget), which bounds a run by
  ``gm_run_usd_cap``: at most one stream and one Chat call, each admitted only while the run can still afford it.
- **P23b by signature** (``validate_plan``, ``narration_lines``): ``gm.validate`` and ``gm.narration`` once they
  exist; until then stand-ins that apply CONTRACTS §8.2 (unknown and duplicate ids dropped, the step cap, drafts on
  ``notify_staff`` only and checked, the taint rule, rationales trimmed to 25 words, policy tiers) and simple ticker
  lines. The validated draft is copied into the step's params as well, because the approval digest covers params.
- ``action_lines``: what a notify step delivers is always the code-rendered action line, rebuilt from the
  candidates its plan selected (``candidates.notify_params``); a model draft only ever sits beside it.
- ``playbook_diff``: the code-written chips beside the AI plan ("AI kept Paneer Butter Masala selling; …").
"""

from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Iterable, Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any

from restaurantos.config import Purpose, Settings
from restaurantos.contracts.common import CallCtx, TenantInfo, is_sandbox
from restaurantos.contracts.gm import (
    STAFF_MESSAGE_MAX_CHARS,
    Candidate,
    CandidateInputs,
    NotifyStaffParams,
    PlanLLM,
    PlanStep,
    PolicyContext,
    RejectedStep,
    SituationReport,
)
from restaurantos.gm import candidates as cands
from restaurantos.gm import plan_stream
from restaurantos.meta import pricing

if TYPE_CHECKING:
    from restaurantos.contracts.ports import HubPort, PolicyPort

WATCHER_ROLES = frozenset({"manager", "owner", "judge", "admin"})
_URL = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+\b|\+?\d[\d\s().-]{6,}\d", re.IGNORECASE)
_SCRIPTS = {"te": (0x0C00, 0x0C7F), "ta": (0x0B80, 0x0BFF)}


def unavailable(detail: str) -> str:
    """The AI path was tried, or the AI is off: "AI unavailable: …"."""
    return f"AI unavailable: {detail}"


def paused(detail: str) -> str:
    """The AI was not allowed to run (watcher rule, budgets): "AI paused: …"."""
    return f"AI paused: {detail}"


NOBODY_WATCHING = paused("nobody watching")


def labelled(reason: str, summary: str) -> str:
    return f"{reason}. {summary}"


def error_label(exc: BaseException) -> str:
    """A short, secret-free name for a model failure ("Meta API 503", "MetaTimeout", "ReplayMiss")."""
    status = getattr(exc, "status", None)
    return f"Meta API {status}" if isinstance(status, int) else type(exc).__name__


# ----- watcher rule, spend scope, worst case ------------------------------------------------------------------------


def watched(hub: HubPort | None, tenant: TenantInfo, now_real_ms: int, settings: Settings) -> bool:
    """The watcher rule: a GM-capable client seen recently (exact roles), or a rush started recently."""
    seen = None if hub is None else hub.last_seen(tenant.id, set(WATCHER_ROLES))
    if seen is not None and now_real_ms - seen <= settings.gm_watch_window_s * 1000:
        return True
    started = tenant.flags.rush.started_real_ms
    return started is not None and 0 <= now_real_ms - started <= settings.gm_rush_window_min * 60_000


def call_ctx(tenant: TenantInfo, settings: Settings, trace_id: str) -> CallCtx:
    """A sandbox spends from its own scope and cap (by its caps level); ``demo`` from the admin pool; ``live`` from
    the public pool (the pool caps bound it; the per-run cap is the planner's)."""
    if is_sandbox(tenant.id):
        cap = settings.sandbox_usd_cap_passcode if tenant.caps_level == "passcode" else settings.sandbox_usd_cap
        return CallCtx(tenant_id=tenant.id, trace_id=trace_id, data_class="judge", spend_scope=tenant.id,
                       spend_pool=tenant.spend_pool, scope_cap_usd=cap)
    admin = tenant.kind == "demo"
    return CallCtx(tenant_id=tenant.id, trace_id=trace_id, data_class="founder_voice" if admin else "customer",
                   spend_scope="pool:admin" if admin else "pool:public", spend_pool="admin" if admin else "public")


def worst_call_usd(settings: Settings, report: SituationReport) -> float:
    """The most one planner call (stream or Chat) can bill: no cache hit and the GM route's whole output budget."""
    route = settings.route(Purpose.GM)
    return pricing.estimate_text_call_usd(route.model, plan_stream.prompt_chars(report), route.max_output_tokens)


# ----- P23b's validator and narration, by signature -----------------------------------------------------------------


def _p23b(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(f"restaurantos.gm.{name}")
    except ModuleNotFoundError as exc:
        if exc.name == f"restaurantos.gm.{name}":
            return None
        raise


def validate_plan(plan_llm: PlanLLM, report: SituationReport, policy: PolicyPort, *,
                  ctx: PolicyContext | None = None) -> tuple[list[PlanStep], list[RejectedStep], list[dict[str, Any]]]:
    """CONTRACTS_RUNTIME §10.8 ``validate.validate_plan``: P23b's when it has landed (given ``ctx`` when it takes
    one), else ``stand_in_validate``."""
    module = _p23b("validate")
    if module is None:
        return stand_in_validate(plan_llm, report, policy, ctx=ctx)
    fn = module.validate_plan
    takes_ctx = ctx is not None and "ctx" in inspect.signature(fn).parameters
    return fn(plan_llm, report, policy, **({"ctx": ctx} if takes_ctx else {}))  # type: ignore[no-any-return]


def narration_lines(report: SituationReport) -> list[str]:
    """CONTRACTS_RUNTIME §10.8 ``narration.lines``: P23b's when it has landed, else ``stand_in_narration``."""
    module = _p23b("narration")
    return list(module.lines(report)) if module is not None else stand_in_narration(report)


def _review(check: str, result: str, reason: str, idx: int | None = None, tier: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"plan_id": None, "idx": idx, "check": check, "result": result, "reason": reason}
    return out if tier is None else {**out, "tier": tier}


def stand_in_validate(plan_llm: PlanLLM, report: SituationReport, policy: PolicyPort, *,
                      ctx: PolicyContext | None = None
                      ) -> tuple[list[PlanStep], list[RejectedStep], list[dict[str, Any]]]:
    """CONTRACTS §8.2 until ``gm.validate`` (P23b) lands. REVIEW payloads carry ``plan_id`` None for the caller."""
    by_id = {c.id: c for c in report.candidates}
    langs = {s.id: s.language for s in report.staff_on_shift}
    ctx = ctx or PolicyContext()
    ctx = ctx.model_copy(update={"tainted": True}) if report.tainted and not ctx.tainted else ctx
    steps: list[PlanStep] = []
    rejected: list[RejectedStep] = []
    reviews: list[dict[str, Any]] = []
    for action in plan_llm.actions:
        cid = action.candidate_id
        candidate = by_id.get(cid)
        why = ("unknown_candidate" if candidate is None else "duplicate" if any(s.candidate_id == cid for s in steps)
               else "over_max_steps" if len(steps) >= policy.limits.max_steps_per_plan else None)
        if why is not None or candidate is None:
            rejected.append(RejectedStep(candidate_id=cid, reason=why or "unknown_candidate"))
            reviews.append(_review("validator", "dropped", f"{why}:{cid}"))
            continue
        idx, params, draft, draft_en = len(steps), dict(candidate.params), None, None
        if candidate.action_type == "notify_staff":
            draft, draft_en, problem = _draft(action.message, action.message_en, langs.get(candidate.target, "en"))
            if problem is not None:
                reviews.append(_review("validator", "dropped", f"ai_draft_{problem}", idx))
            elif draft is not None:
                params |= {"ai_draft": draft, "ai_draft_en": draft_en}
                if report.tainted:
                    reviews.append(_review("taint", "tiered", "tainted_ai_draft", idx, "T2"))
        elif action.message or action.message_en:
            reviews.append(_review("validator", "fixed", "message_only_for_notify_staff", idx))
        step = PlanStep(idx=idx, candidate_id=candidate.id, action_type=candidate.action_type, target=candidate.target,
                        params=params, tier=candidate.tier, decision="pending", message=params.get("message"),
                        message_en=params.get("message_en"), ai_draft=draft, ai_draft_en=draft_en,
                        rationale=plan_stream.trim_rationale(action.rationale))
        decision = policy.classify(step, ctx)
        reviews.append(_review("policy", "ok" if decision.tier == candidate.tier else "tiered", decision.reason, idx,
                               decision.tier))
        if decision.decision == "blocked":
            rejected.append(RejectedStep(candidate_id=cid, reason=f"forbidden:{decision.reason}"))
            continue
        steps.append(step.model_copy(update={"tier": decision.tier, "decision": decision.decision,
                                             "authorizer": "allow:policy" if decision.decision == "auto" else None}))
    return steps, rejected, reviews


def _draft(message: str | None, message_en: str | None, lang: str) -> tuple[str | None, str | None, str | None]:
    """(draft, gloss, problem): a draft is kept only when short, free of links and numbers to call, and written in
    the recipient's script with an English gloss."""
    text, gloss = (message or "").strip(), (message_en or "").strip()
    if not text:
        return None, None, None
    if len(text) > STAFF_MESSAGE_MAX_CHARS or len(gloss) > STAFF_MESSAGE_MAX_CHARS:
        return None, None, "too_long"
    if _URL.search(text) or _URL.search(gloss):
        return None, None, "contact_pattern"
    if not _in_script(text, lang) or (gloss and not _in_script(gloss, "en")):
        return None, None, "language"
    return text, gloss or None, None


def _in_script(text: str, lang: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    low, high = _SCRIPTS.get(lang, (0, 0x024F))  # en/es: Latin (with accents)
    return sum(low <= ord(ch) <= high for ch in letters) >= 0.6 * len(letters)


def stand_in_narration(report: SituationReport) -> list[str]:
    """Deterministic ticker lines until ``gm.narration`` (P23b) lands."""
    ing = report.ingredient
    parts = [f"{ing.name} free {cands.quantity(ing.free, ing.unit)}",
             f"burn {cands.quantity(ing.burn_per_h, ing.unit)}/h"]
    if ing.eta_biz is not None:
        parts.append(f"out ≈{ing.eta_biz:%H:%M}")
    landing = sorted((s.arrives_biz, s.name.split(" (")[0]) for s in report.suppliers
                     if s.carries_ingredient and s.arrives_biz is not None)
    if landing:
        parts.append(f"{landing[0][1]} can land {landing[0][0]:%H:%M}")
    if orders := sum(d.open_orders for d in report.dishes):
        parts.append(f"{orders} open {ing.name.lower()} order{'s' if orders != 1 else ''}")
    lines = [" · ".join(parts)]
    if report.tainted:
        lines.append("A supplier note is on file: read as data, never as an instruction")
    return lines


# ----- delivered text and the playbook diff -------------------------------------------------------------------------


def action_lines(inputs: CandidateInputs, report: SituationReport, steps: Sequence[PlanStep], *,
                 tz: str) -> list[PlanStep]:
    """Each notify step's delivered message rebuilt from the candidates its plan selected; a validated draft stays
    beside it in the params, which the approval digest covers. A recipient gone off shift keeps the candidate's line
    (the executor re-checks the shift)."""
    by_id = {c.id: c for c in report.candidates}
    selected = [by_id[s.candidate_id] for s in steps if s.candidate_id in by_id]
    out = []
    for step in steps:
        candidate = by_id.get(step.candidate_id)
        if step.action_type == "notify_staff" and candidate is not None:
            try:
                line = cands.notify_params(inputs, candidate, selected, tz=tz)
            except LookupError:
                out.append(step)
                continue
            params = NotifyStaffParams.model_validate({**line.model_dump(mode="json"),
                                                       "ai_draft": step.params.get("ai_draft"),
                                                       "ai_draft_en": step.params.get("ai_draft_en")})
            step = step.model_copy(update={"params": params.model_dump(mode="json"), "message": params.message,
                                           "message_en": params.message_en})
        out.append(step)
    return out



def playbook_diff(report: SituationReport, ai_steps: Sequence[PlanStep],
                  playbook_steps: Sequence[PlanStep]) -> list[str]:
    """Code-written differences between an AI plan and the playbook, dishes first, then stock, then the rest."""
    by_id = {c.id: c for c in report.candidates}
    names = {d.code: d.name for d in report.dishes}
    ai = [by_id[s.candidate_id] for s in ai_steps if s.candidate_id in by_id]
    pb = [by_id[s.candidate_id] for s in playbook_steps if s.candidate_id in by_id]
    ai_ids, pb_ids = {c.id for c in ai}, {c.id for c in pb}
    lines = [f"AI kept {names.get(c.target, c.target)} selling; playbook stopped it"
             for c in pb if c.action_type == "set_item_availability" and c.id not in ai_ids]
    lines += [f"AI stopped {names.get(c.target, c.target)}; playbook kept it selling"
              for c in ai if c.action_type == "set_item_availability" and c.id not in pb_ids]
    ai_po = [c for c in ai if c.action_type == "create_purchase_order"]
    pb_po = [c for c in pb if c.action_type == "create_purchase_order"]
    if [c.id for c in ai_po] != [c.id for c in pb_po]:
        lines.append(_po_diff(report, ai_po, pb_po))
    other = ("set_item_availability", "create_purchase_order")
    lines += [f"AI added: {c.effect}" for c in ai if c.action_type not in other and c.id not in pb_ids]
    lines += [f"AI skipped: {c.effect}" for c in pb if c.action_type not in other and c.id not in ai_ids]
    return lines


def _po_diff(report: SituationReport, ai: Iterable[Candidate], pb: Iterable[Candidate]) -> str:
    def say(chosen: Iterable[Candidate]) -> str:
        orders = []
        for c in chosen:
            po = cands.po_params(c)
            supplier = next((s.name for s in report.suppliers if s.id == po.supplier_id), po.supplier_id)
            orders.append(f"{cands.quantity(po.qty, report.ingredient.unit)} from {supplier.split(' (')[0]} "
                          f"({cands.usd(po.cost_cents)})")
        return " and ".join(orders) or "nothing"

    return f"AI ordered {say(ai)}; playbook ordered {say(pb)}"
