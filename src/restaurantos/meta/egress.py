"""Pseudonymized model egress (pass 17): back-office model calls see codes, never names.

A restaurant's ingredient and dish names are its trade secret — pass 16 keeps the system from
silently assuming the lists; this pass keeps the names themselves from leaving the building. When
the AI reasons about a kitchen (the GM planner's situation reports, a win-back draft), every
ingredient and dish travels under an opaque per-kitchen code — ``ING_0147``, ``ITEM_0231`` — and
the model's answer is translated back locally before anything is displayed or stored. Numbers and
quantities still travel: the AI needs them to reason about stock, so this hides WHAT, never HOW
MUCH — the privacy page says exactly that.

The mapping (``contracts.inventory.EgressCode`` rows in the ``egress_codes`` table) lives ONLY in
the kitchen's own database. A code's number is drawn from a hash of (tenant, kind, ref), so the
same name carries a different code in every kitchen and no global dictionary exists; rows are
assigned once, lazily, at the first covered call, and never renumbered.

Covered call sites (payloads built here, answers de-tokenized here):

- the GM planner (``gm.planner``): the situation report is tokenized on the way out — names,
  name-revealing internal ids (``ING_PANEER``, ``CHK_BIRY``) and any name inside free text a
  candidate's effect line carries — and the plan, the streamed reasoning and the progressive
  cards are de-tokenized on the way back. Two things in the report are NOT tokenized, on purpose:
  supplier and staff names (counterparties and people the manager acts on, not the kitchen's
  secret — the planner's instructions treat them as identities, not ingredients), and the
  supplier notes themselves: they are the suppliers' own words, carried verbatim inside the
  untrusted-data fence the security model depends on — a supplier naming what they supply is
  their disclosure, not the kitchen's, and rewriting adversarial text would break the fence.
- the win-back draft (``services.winback``): the guest's usual dish goes out as its code; the
  model's draft is de-tokenized before it is stored, so the guest reads the dish's real name.
  The guest's first name and the restaurant's name stay — they are the message's recipient and
  sender, not menu data.

Deliberately NOT covered, with the reason recorded in BUJJI_SESSION_HANDOFF.md (pass 17):
customer-facing ordering (the guest speaks dish names themselves — their own words, not the
kitchen's secret), shelf-photo counting and bill scan (their payloads are the manager's own photo
or bill plus generic instructions; no catalog names are sent), ASR (the guest's own speech), and
the offline eval harness (synthetic data only).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from restaurantos.contracts.gm import PlanLLM, SituationReport
from restaurantos.contracts.inventory import EgressCode, EgressKind, Ingredient
from restaurantos.contracts.menu import MenuItem

CODE_RE = re.compile(r"\b(?:ING|ITEM)_\d{4}\b")
_PREFIX: dict[str, str] = {"ingredient": "ING", "item": "ITEM"}
_NUMBERS = 9000  # codes run 1000..9999 within a kind
_ID_EDGE = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"
_WORD_BOUNDARY = r"(?<!\w){}(?!\w)"


def _draw(tenant_id: str, kind: str, ref: str) -> int:
    """The first number tried for one ref: a hash of (tenant, kind, ref), so the same name draws a
    different number in every kitchen and nothing about the name can be read back from the draw."""
    digest = hashlib.sha256(f"ros-egress:{tenant_id}:{kind}:{ref}".encode()).hexdigest()
    return 1000 + int(digest[:12], 16) % _NUMBERS


def new_assignments(tenant_id: str, existing: list[EgressCode],
                    refs: dict[str, list[str]]) -> list[EgressCode]:
    """Codes for the refs that have none yet, in the caller's order; existing rows never renumber.

    A draw that lands on a number another ref of the same tenant already holds probes forward
    (wrapping at 9999) until a free number turns up, so assignment is deterministic for a given
    catalog history."""
    have = {(row.kind, row.ref) for row in existing}
    taken = {row.code for row in existing}
    out: list[EgressCode] = []
    for kind in ("ingredient", "item"):
        for ref in refs.get(kind, []):
            if (kind, ref) in have:
                continue
            number = _draw(tenant_id, kind, ref)
            for _ in range(_NUMBERS):
                code = f"{_PREFIX[kind]}_{number:04d}"
                if code not in taken:
                    break
                number = 1000 + (number - 1000 + 1) % _NUMBERS
            else:  # pragma: no cover - a kitchen with 9000 ingredients of one kind
                raise ValueError(f"no egress code left for {kind} {ref!r} in {tenant_id}")
            taken.add(code)
            have.add((kind, ref))
            out.append(EgressCode(kind=kind, ref=ref, code=code))  # type: ignore[arg-type]
    return out


class EgressMap:
    """One kitchen's code↔name view: tokenize on the way out, de-tokenize on the way back.

    Built from the persisted rows plus the kitchen's catalog (for the names). Rows whose ref has
    left the catalog keep no name and stop decoding — a stale code in an old text stays visible
    rather than guessing at a name."""

    def __init__(self, rows: list[EgressCode], ingredients: list[Ingredient],
                 items: list[MenuItem]) -> None:
        self._by_ref: dict[tuple[str, str], str] = {(r.kind, r.ref): r.code for r in rows}
        display: dict[str, str] = {}  # code -> the English name shown to people
        pairs: list[tuple[str, str, bool]] = []  # (text, code, is_ref) in registration order
        for ing in ingredients:
            code = self._by_ref.get(("ingredient", ing.id))
            if code is None:
                continue
            display[code] = ing.names["en"]
            pairs.append((ing.id, code, True))
            pairs += [(name, code, False) for name in ing.names.values() if name]
        for item in items:
            code = self._by_ref.get(("item", item.code))
            if code is None:
                continue
            display[code] = item.names.en
            pairs.append((item.code, code, True))
            pairs += [(name, code, False) for name in item.names.model_dump().values() if name]
        self._display = display
        self._refs_re, self._ref_codes = self._compile(
            [(text, code) for text, code, is_ref in pairs if is_ref], case_sensitive=True)
        self._names_re, self._name_codes = self._compile(
            [(text, code) for text, code, is_ref in pairs if not is_ref], case_sensitive=False)

    @staticmethod
    def _compile(pairs: list[tuple[str, str]], *, case_sensitive: bool,
                 ) -> tuple[re.Pattern[str] | None, dict[str, str]]:
        """One alternation per class, longest text first, so a dish's full name wins over an
        ingredient's shorter one at the same position."""
        unique: dict[str, str] = {}
        for text, code in pairs:
            unique.setdefault(text if case_sensitive else text.lower(), code)
        if not unique:
            return None, {}
        boundary = _ID_EDGE if case_sensitive else _WORD_BOUNDARY
        body = "|".join(re.escape(text) for text in sorted(unique, key=len, reverse=True))
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.compile(boundary.format(f"(?:{body})"), flags), unique

    # ----- the map itself ---------------------------------------------------------------------------------------

    def code_for(self, kind: EgressKind, ref: str) -> str | None:
        return self._by_ref.get((kind, ref))

    # ----- out --------------------------------------------------------------------------------------------------

    def tokenize(self, text: str) -> str:
        """``text`` with every internal id and every catalog name replaced by its code."""
        if not text:
            return text
        if self._refs_re is not None:
            text = self._refs_re.sub(lambda m: self._ref_codes[m.group(0)], text)
        if self._names_re is not None:
            text = self._names_re.sub(lambda m: self._name_codes[m.group(0).lower()], text)
        return text

    def tokenize_report(self, report: SituationReport) -> SituationReport:
        """A copy of the planner's report with names tokenized in every string it carries —
        structured fields, candidate effect lines and candidate params alike. The notes are the
        one exception: they pass through verbatim inside their fence (see the module docstring).
        The original stays untouched: it is what the incident stores and digests."""
        data = report.model_dump(mode="json")
        notes = data.pop("notes")
        walked = _walk(data, self.tokenize)
        walked["notes"] = notes
        return SituationReport.model_validate(walked)

    # ----- back -------------------------------------------------------------------------------------------------

    def detokenize(self, text: str) -> str:
        """Model text with codes turned back into names. A code this kitchen never issued (the
        model invented it) is left exactly as written — visible, harmless, and never a crash."""
        if not text:
            return text
        return CODE_RE.sub(lambda m: self._display.get(m.group(0), m.group(0)), text)

    def detokenize_plan(self, plan: PlanLLM) -> PlanLLM:
        """A model plan whose free text names dishes again: summary, questions, and each action's
        rationale and staff-message drafts."""
        return plan.model_copy(update={
            "summary": self.detokenize(plan.summary),
            "questions_for_manager": [self.detokenize(q) for q in plan.questions_for_manager],
            "actions": [action.model_copy(update={
                "rationale": self.detokenize(action.rationale),
                "message": None if action.message is None else self.detokenize(action.message),
                "message_en": None if action.message_en is None else self.detokenize(action.message_en),
            }) for action in plan.actions]})


def _walk(value: Any, fn: Any) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, fn) for k, v in value.items()}
    return value
