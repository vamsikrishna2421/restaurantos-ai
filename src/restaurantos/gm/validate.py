"""The plan validator (CONTRACTS §8.2, ARCHITECTURE §6.5): what a ``PlanLLM`` may choose, and what it never writes.

``validate_plan(plan_llm, report, policy)`` turns the model's (or the playbook's) choice into ``PlanStep``s:

- unknown and duplicate candidate ids are dropped, so is any same-day PO after the first the policy lets through for
  its ingredient (``alternative_po``: the bridge, to-close and other suppliers' same-day POs each cover the same gap,
  so a second one over-orders on a single approval), then the plan is capped at ``max_steps_per_plan``;
- a step is its candidate: params, target, cost and the delivered notify ``message`` are code-computed. ``message``
  and ``message_en`` count only on ``notify_staff``, where they become ``ai_draft``/``ai_draft_en`` (also copied into
  the params, so the approval digest binds the exact draft) if the draft passes: at most ``staff_message_max_chars``,
  no URL, email or phone number, written in the recipient's language, with an English gloss for a non-English
  recipient. Anything else is dropped, never repaired;
- **taint rule**: on a tainted report a kept draft makes its step T2 (``Policy.classify`` says ``tainted_ai_draft``;
  enforced here too), so the draft is shown only after an approval while the action line still runs as T1 (P25);
- ``rationale`` is trimmed to 25 words, with contact details removed;
- the policy assigns tier and decision (a candidate's own tier is never trusted); a step it forbids moves to
  ``rejected``.

The third element of the result is the REVIEW ledger payloads (CONTRACTS_RUNTIME §9), in the model's order: one per
drop or fix (``check`` validator), one per step from the policy and one per tainted draft (``check`` taint). The
planner appends them with ``Ledger.append`` inside its own write transaction. Pure and synchronous.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from restaurantos.contracts.gm import (
    Candidate,
    CreatePurchaseOrderParams,
    PlanLLM,
    PlanLLMAction,
    PlanStep,
    PolicyContext,
    RejectedStep,
    SituationReport,
    TierDecision,
)

if TYPE_CHECKING:
    from restaurantos.contracts.ports import PolicyPort

RATIONALE_MAX_WORDS = 25
AUTHORIZER_POLICY = "allow:policy"
REMOVED = "[removed]"
_TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "FORBIDDEN": 4}
_JOINERS = frozenset("‌‍")  # ZWNJ and ZWJ shape Telugu and Tamil: the only format characters kept

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(
    r"(?i)(?:\b(?:https?|ftp)://|\bwww\.)\S+"
    r"|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|in|us|uk|info|biz|app|dev|ly|me|ai|"
    r"xyz|gg|tv|site|online|link|shop|store|top|click|live)\b(?:/\S*)?")
_PHONE = re.compile(r"(?<!\w)\+?\(?\d(?:[\s().\-]*\d){6,}(?!\w)")  # 7+ digits, any script, with separators
_CONTACTS = (("email", _EMAIL), ("url", _URL), ("phone", _PHONE))
_SCRIPTS = (("te", 0x0C00, 0x0C7F), ("ta", 0x0B80, 0x0BFF))
_SPANISH_MARKS = "áéíóúñü¿¡"
_ES_WORDS = frozenset(
    "el la los las de del que y en por para con se es son está están un una al lo le les su sus ya hay más muy pero "
    "como sin este esta estos estas solo sólo quedan queda ahora favor usar usa usen ofrece ofrecer ofrezcan platos "
    "plato pedidos pedido hoy mañana antes después hasta menos nos cocina gracias".split())
_EN_WORDS = frozenset(
    "the and of to is are was were for with now please only left we you your it this that be at from by have has "
    "stop use keep before after until more than next running low tonight today instead offer guests will can just "
    "not off our need what when which there their they them should would could about into out then here dish "
    "dishes thanks".split())


@dataclass
class _Built:
    """One kept action: its step before the policy, the fixes the validator made, and the policy's verdict."""

    step: PlanStep
    fixes: list[tuple[str, str]] = field(default_factory=list)  # (result, reason)
    verdict: TierDecision | None = None


def validate_plan(plan_llm: PlanLLM, report: SituationReport, policy: PolicyPort, *,
                  ctx: PolicyContext | None = None, plan_id: str | None = None,
                  ) -> tuple[list[PlanStep], list[RejectedStep], list[dict[str, Any]]]:
    """``(steps, rejected, review_payloads)`` for ``plan_llm`` against ``report`` (CONTRACTS_RUNTIME §10.8).

    ``ctx`` is the ``PolicyContext`` to classify with (``governance.policy.policy_context``); it defaults to the
    report's taint alone, and a caller can never untaint a tainted report. ``plan_id`` is stamped into every payload.
    """
    limits = policy.limits
    ctx = _context(ctx, report)
    by_id = {candidate.id: candidate for candidate in report.candidates}
    seen: set[str] = set()
    restocked: set[str] = set()  # ingredients with a same-day PO kept: any other same-day PO is an alternative to it
    items: list[_Built | tuple[str, str]] = []  # a kept action, or (candidate_id, why it was dropped)
    for action in plan_llm.actions:
        cid = action.candidate_id
        same_day = _same_day_po(by_id[cid], report) if cid in by_id else None
        why = ("unknown_candidate" if cid not in by_id else "duplicate_candidate" if cid in seen
               else "alternative_po" if same_day in restocked
               else "over_max_steps" if len(seen) >= limits.max_steps_per_plan else None)
        if why is not None:
            items.append((cid, why))
            continue
        seen.add(cid)
        built = _build(by_id[cid], action, limits.staff_message_max_chars)
        built.verdict = _tiered(policy.classify(built.step, ctx), built.step, ctx)
        if same_day is not None and built.verdict.decision != "blocked":
            restocked.add(same_day)
        items.append(built)

    steps: list[PlanStep] = []
    rejected: list[RejectedStep] = []
    reviews: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, tuple):
            rejected.append(RejectedStep(candidate_id=item[0], reason=item[1]))
            reviews.append(_row(plan_id, None, "validator", "dropped", item[1], candidate_id=item[0]))
            continue
        verdict = item.verdict
        assert verdict is not None
        if verdict.decision == "blocked":
            rejected.append(RejectedStep(candidate_id=item.step.candidate_id, reason=f"policy:{verdict.reason}"))
            reviews.append(_row(plan_id, None, "policy", "dropped", verdict.reason, tier=verdict.tier,
                                candidate_id=item.step.candidate_id))
            continue
        claimed = item.step.model_copy(update={"idx": len(steps)})
        reviews += [_row(plan_id, claimed.idx, "validator", result, reason) for result, reason in item.fixes]
        reviews.append(_policy_row(policy, plan_id, claimed, verdict))
        if ctx.tainted and claimed.ai_draft:
            reviews.append(_row(plan_id, claimed.idx, "taint", "tiered", "tainted_ai_draft", tier=verdict.tier))
        steps.append(claimed.model_copy(update={
            "tier": verdict.tier, "decision": verdict.decision,
            "authorizer": AUTHORIZER_POLICY if verdict.decision == "auto" else None}))
    return steps, rejected, reviews


def _same_day_po(candidate: Candidate, report: SituationReport) -> str | None:
    """The ingredient of a PO candidate landing on the report's business day; None for anything else."""
    if candidate.action_type != "create_purchase_order":
        return None
    params = candidate.params_model()
    assert isinstance(params, CreatePurchaseOrderParams)
    same = params.eta_biz.astimezone(report.now_biz.tzinfo).date() == report.now_biz.date()
    return params.ingredient_id if same else None


def _context(ctx: PolicyContext | None, report: SituationReport) -> PolicyContext:
    if ctx is None:
        return PolicyContext(tainted=report.tainted)
    return ctx if ctx.tainted or not report.tainted else ctx.model_copy(update={"tainted": True})


def _build(candidate: Candidate, action: PlanLLMAction, max_chars: int) -> _Built:
    params = dict(candidate.params)
    fixes: list[tuple[str, str]] = []
    message = message_en = draft = gloss = None
    wrote = bool(clean(action.message) or clean(action.message_en))  # "" or null both mean no draft
    if candidate.action_type == "notify_staff":
        message, message_en = params["message"], params["message_en"]
        if wrote:
            draft, gloss, problem = _draft(action, params["language"], max_chars)
            if problem is not None:
                fixes.append(("dropped", problem))
                draft = gloss = None
        params["ai_draft"], params["ai_draft_en"] = draft, gloss  # the approval digest covers params only
    elif wrote:
        fixes.append(("fixed", "message_not_allowed"))
    rationale, notes = _rationale(action.rationale)
    fixes += [("fixed", note) for note in notes]
    step = PlanStep(idx=0, candidate_id=candidate.id, action_type=candidate.action_type, target=candidate.target,
                    params=params, tier=candidate.tier, decision="pending", message=message, message_en=message_en,
                    ai_draft=draft, ai_draft_en=gloss, rationale=rationale)
    return _Built(step, fixes)


def _tiered(verdict: TierDecision, step: PlanStep, ctx: PolicyContext) -> TierDecision:
    """The taint rule, enforced whatever the policy port says: a tainted draft waits for a manager at least."""
    if ctx.tainted and step.ai_draft and verdict.decision != "blocked" and _TIER_RANK[verdict.tier] < 2:
        return TierDecision(tier="T2", decision="pending", reason="tainted_ai_draft", needs_role="manager")
    return verdict


def _row(plan_id: str | None, idx: int | None, check: str, result: str, reason: str, *, tier: str | None = None,
         candidate_id: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"plan_id": plan_id, "idx": idx, "check": check, "result": result, "reason": reason}
    if tier is not None:
        row["tier"] = tier
    if candidate_id is not None:
        row["candidate_id"] = candidate_id
    return row


def _policy_row(policy: PolicyPort, plan_id: str | None, step: PlanStep, verdict: TierDecision) -> dict[str, Any]:
    """P24's ``review_payload`` when the policy has one (``result`` ok, or tiered when it overrode the candidate)."""
    review = getattr(policy, "review_payload", None)
    if callable(review):
        return dict(review(plan_id, step, verdict))
    result = "ok" if verdict.tier == step.tier else "tiered"
    return _row(plan_id, step.idx, "policy", result, verdict.reason, tier=verdict.tier)


# ----- drafts -----------------------------------------------------------------------------------------------------


def clean(text: str | None) -> str:
    """NFC, control and format characters gone (bidi overrides, zero-width spaces; ZWNJ/ZWJ stay), whitespace
    collapsed to single spaces."""
    if not text:
        return ""
    out = []
    for char in unicodedata.normalize("NFC", text):
        category = unicodedata.category(char)
        if category == "Cc" or category in ("Zl", "Zp"):
            out.append(" ")
        elif not category.startswith("C") or char in _JOINERS:
            out.append(char)
    return " ".join("".join(out).split())


def contact(text: str) -> str | None:
    """``email``, ``url`` or ``phone`` when ``text`` carries one, else None."""
    return next((name for name, pattern in _CONTACTS if pattern.search(text)), None)


def sanitize(text: str | None, *, max_words: int | None = None) -> str:
    """Model text for a manager's screen (summary, questions, rationale): ``clean``, contact details replaced by
    ``[removed]``, optionally cut to ``max_words``."""
    value = clean(text)
    for _, pattern in _CONTACTS:
        value = pattern.sub(REMOVED, value)
    words = value.split()
    return " ".join(words[:max_words]) if max_words is not None else " ".join(words)


def _letters(text: str) -> Counter[str]:
    """Letters by script: ``latin``, ``te``, ``ta`` or ``other`` (Devanagari, Kannada, Malayalam, ...)."""
    out: Counter[str] = Counter()
    for char in text:
        if not char.isalpha():
            continue
        code = ord(char)
        script = next((name for name, low, high in _SCRIPTS if low <= code <= high), None)
        out[script or ("latin" if code < 0x0250 or 0x1E00 <= code <= 0x1EFF else "other")] += 1
    return out


def _votes(text: str) -> tuple[int, int]:
    """(Spanish, English) evidence: function words of each, plus Spanish accents and inverted marks."""
    lower = text.lower()
    words = re.findall(r"[a-záéíóúñü']+", lower)
    spanish = sum(word in _ES_WORDS for word in words) + sum(lower.count(mark) for mark in _SPANISH_MARKS)
    return spanish, sum(word in _EN_WORDS for word in words)


def language_ok(text: str, lang: str) -> bool:
    """Whether ``text`` reads as ``lang``: Telugu or Tamil script for te/ta (a third of the letters at least, English
    words allowed, no other Indic script); Latin script for en/es, refused only when the words clearly say the other
    one. Short neutral text ("PNR_BM: 86") passes for en and es."""
    letters = _letters(text)
    if letters["other"]:
        return False
    if lang in ("te", "ta"):
        own, foreign = letters[lang], letters["ta" if lang == "te" else "te"]
        return own > 0 and not foreign and own * 3 >= sum(letters.values())
    if letters["te"] or letters["ta"]:
        return False
    spanish, english = _votes(text)
    return english <= spanish if lang == "es" else spanish <= english


def draft_problem(text: str, lang: str, max_chars: int) -> str | None:
    """Why a cleaned draft cannot reach a screen (``empty``, ``too_long``, ``email``, ``url``, ``phone``,
    ``language``), else None."""
    if not text:
        return "empty"
    if len(text) > max_chars:
        return "too_long"
    return contact(text) or (None if language_ok(text, lang) else "language")


def _draft(action: PlanLLMAction, lang: str, max_chars: int) -> tuple[str | None, str | None, str | None]:
    """(draft, English gloss, None), or the reason the pair is dropped. An English recipient's draft is its own
    gloss; anyone else's needs a valid English gloss, so a manager approving it can read it."""
    draft = clean(action.message)
    if (problem := draft_problem(draft, lang, max_chars)) is not None:
        return None, None, f"draft_{problem}"
    if lang == "en":
        return draft, draft, None
    gloss = clean(action.message_en)
    if (problem := draft_problem(gloss, "en", max_chars)) is not None:
        return None, None, f"gloss_{problem}"
    return draft, gloss, None


def _rationale(text: str) -> tuple[str, list[str]]:
    """The rationale cut to 25 words with contact details removed, and the fixes that took."""
    cleaned = clean(text)
    scrubbed = sanitize(cleaned)
    notes = ["rationale_contact_removed"] if scrubbed != cleaned else []
    words = scrubbed.split()
    if len(words) > RATIONALE_MAX_WORDS:
        notes.append("rationale_trimmed")
    return " ".join(words[:RATIONALE_MAX_WORDS]), notes
