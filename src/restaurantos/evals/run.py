"""The E1 text runner (P31a; CONTRACTS_SEED §15, ARCHITECTURE_VOICE_MEDIA §8, DECISIONS D19).

**Data class comes from the record, never from a flag.** ``data_class_for(source)`` is ``synthetic`` exactly when the
case's ``source`` starts with ``synthetic:``; ``public_dataset:``, ``founder_voice:``/``consented:``, ``judge:`` and
``staff:`` name those classes, and anything else counts as customer data. Nothing here takes a synthetic or data-class
argument, and ``EvalCase`` refuses unknown fields. Real data never meets a contributor model: ``run_cases`` refuses the
whole run (``PolicyViolation``) before its first call when the provider's model is a ``-contributor`` model and any
case's context fails the F0 contributor guard (dev, no tenant, no session, synthetic).

``run_cases`` parses each case through a provider with at most ``concurrency`` calls in flight, on spend scope
``dev:P31a-run`` (no tenant, no session) capped at ``scope_cap_usd``: the SpendGuard refuses the call that would cross
it. Each case yields a ``CaseResult``: the turn, the wall-clock latency of the parse, the call's cost and tokens. A
parse that comes back unavailable (timeout, API error, unreadable output, replay miss) is a result with that reason,
so it is graded as a miss. A budget refusal stops the run: cases not started get no result and are listed in
``RunOutcome.not_run``. ``on_result`` sees every result as it lands, so ``scripts/eval.py`` appends it to the run's
JSONL and resumes after a crash without paying twice (``load_results``). Utterance text is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from restaurantos.agents.order_agent import OrderParseUnavailable
from restaurantos.config import Settings, is_contributor_model
from restaurantos.contracts.api import EvalSource
from restaurantos.contracts.common import CallCtx, DataClass
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.orders import OrderTurnLLM
from restaurantos.evals import SYNTHETIC_PREFIX, EvalCase
from restaurantos.evals.providers import EvalProvider
from restaurantos.meta import errors as me
from restaurantos.meta.spend import CallRecord

log = logging.getLogger(__name__)

RUN_SCOPE = "dev:P31a-run"
RUN_BUDGET_USD = 1.00  # the scope's lifetime cap: every run of this package's E1 spend together
TRACE_PREFIX = "p31a"
PRODUCTION_TIMEOUT_MS = 5_000  # agents.order_agent.PARSE_TIMEOUT_S: slower answers are counted in the report
_REAL_PREFIXES: tuple[tuple[str, DataClass], ...] = (
    ("public_dataset:", "public_dataset"), ("founder_voice:", "founder_voice"), ("consented:", "founder_voice"),
    ("judge:", "judge"), ("staff:", "staff"))
ResultStatus = Literal["ok", "unavailable", "error"]


def data_class_for(source: str) -> DataClass:
    """The data class of a case, from its ``source`` alone (see the module docstring)."""
    if source.startswith(SYNTHETIC_PREFIX):
        return "synthetic"
    return next((cls for prefix, cls in _REAL_PREFIXES if source.startswith(prefix)), "customer")


def source_kind(source: str) -> EvalSource:
    """``synthetic`` or ``real``: the report's ``EvalCell.source``."""
    return "synthetic" if data_class_for(source) == "synthetic" else "real"


def case_ctx(case: EvalCase, *, scope: str = RUN_SCOPE, scope_cap_usd: float | None = None) -> CallCtx:
    """The context of one parse: no tenant, no session, the data class of the case's source."""
    return CallCtx(trace_id=new_id(TRACE_PREFIX), data_class=data_class_for(case.source), channel=case.channel,
                   lang_hint=case.language_style, spend_scope=scope, scope_cap_usd=scope_cap_usd)


def check_policy(cases: Iterable[EvalCase], model: str, settings: Settings, *, scope: str = RUN_SCOPE) -> None:
    """``PolicyViolation`` when a contributor model would see any case its guard does not admit (real data)."""
    if not is_contributor_model(model):
        return
    for case in cases:
        if not me.contributor_allowed(settings, case_ctx(case, scope=scope)):
            raise me.PolicyViolation(f"{model} may not parse eval case {case.id}: contributor models only take "
                                     "synthetic data in dev (F0 delta 7)")


class CaseResult(BaseModel):
    """One parse of one case, as stored in ``config/evals/published/runs/<run_id>.jsonl``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: ResultStatus
    reason: str | None = None  # why it is not ok: an OrderParseUnavailable reason or an exception name
    model: str | None = None
    turn: OrderTurnLLM | None = None
    latency_ms: int = Field(default=0, ge=0)  # wall clock around the parse
    api_latency_ms: int = Field(default=0, ge=0)  # the CallRecord's
    cost_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    input_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    record_status: str | None = None  # ok while recording, replay when replayed, mock with fakes


def result_from(case: EvalCase, turn: OrderTurnLLM, record: CallRecord, latency_ms: int) -> CaseResult:
    return CaseResult(id=case.id, status="ok", model=record.model, turn=turn, latency_ms=latency_ms,
                      api_latency_ms=max(0, record.latency_ms), cost_usd=record.cost_usd,
                      input_tokens=record.input_tokens, cached_tokens=record.cached_tokens,
                      output_tokens=record.output_tokens, reasoning_tokens=record.reasoning_tokens,
                      record_status=record.status)


@dataclass
class RunOutcome:
    results: list[CaseResult] = field(default_factory=list)  # in case order
    not_run: list[str] = field(default_factory=list)  # ids never started (a budget stop)
    budget_stop: bool = False


async def run_cases(cases: Sequence[EvalCase], provider: EvalProvider, settings: Settings, *,
                    scope: str = RUN_SCOPE, scope_cap_usd: float | None = None, concurrency: int = 2,
                    on_result: Callable[[CaseResult], None] | None = None) -> RunOutcome:
    """Parse every case (see the module docstring); raises ``PolicyViolation`` before any call, or from a call."""
    check_policy(cases, provider.model, settings, scope=scope)
    gate = asyncio.Semaphore(max(1, concurrency))
    stop = asyncio.Event()
    results: dict[str, CaseResult] = {}

    async def one(case: EvalCase) -> None:
        async with gate:
            if stop.is_set():
                return
            t0 = time.monotonic()
            try:
                turn, record = await provider.parse(case, case_ctx(case, scope=scope, scope_cap_usd=scope_cap_usd))
            except me.BudgetExceeded:
                stop.set()
                return
            except OrderParseUnavailable as exc:
                if exc.reason == "budget":
                    stop.set()
                    return
                result = CaseResult(id=case.id, status="unavailable", reason=exc.reason, model=exc.model,
                                    latency_ms=_ms(t0))
            except me.PolicyViolation:
                raise
            except Exception as exc:  # one broken case must not end the run; the report counts it as a miss
                log.warning("eval case %s failed: %s", case.id, type(exc).__name__)
                result = CaseResult(id=case.id, status="error", reason=type(exc).__name__, latency_ms=_ms(t0))
            else:
                result = result_from(case, turn, record, _ms(t0))
        results[case.id] = result
        if on_result is not None:
            on_result(result)

    try:
        async with asyncio.TaskGroup() as tasks:  # a PolicyViolation cancels the calls still waiting
            for case in cases:
                tasks.create_task(one(case))
    except* me.PolicyViolation as group:
        raise group.exceptions[0] from None
    return RunOutcome([results[c.id] for c in cases if c.id in results],
                      [c.id for c in cases if c.id not in results], stop.is_set())


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ----- results files -------------------------------------------------------------------------------------------------


def load_results(path: Path) -> dict[str, CaseResult]:
    """The results of a run by case id (the last line for an id wins: a retried case replaces its miss)."""
    if not path.is_file():
        return {}
    out: dict[str, CaseResult] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            result = CaseResult.model_validate_json(line)
            out[result.id] = result
    return out


def append_result(path: Path, result: CaseResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")) + "\n")
