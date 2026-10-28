"""The E1 text runner (P31a; CONTRACTS_SEED §15, D19) and the CI replay gate (MASTER_PLAN §6).

``data_class`` comes from each case's ``source`` and never from a flag; budget stops and failed parses; the Meta
provider on ``FakeMetaClient``; and the replay gate on the real ``MetaClient`` and ``OrderAgent`` (replay mode, the
recorded fixtures in ``tests/fixtures/ai_cache/evals``).
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from restaurantos.agents.order_agent import OrderParseUnavailable
from restaurantos.config import AIMode, Purpose
from restaurantos.contracts.orders import CartOp, OrderTurnLLM
from restaurantos.evals import FIXTURE_ROOT, GATE_PATH, EvalCase, LineSpec, load_cases
from restaurantos.evals import run as runner
from restaurantos.evals.providers import meta as meta_provider
from restaurantos.evals.report import GateBaseline, exact_pct_x10, gate_passes
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import PolicyViolation
from restaurantos.meta.spend import CallRecord, SpendGuard
from restaurantos.seed.loader import load_seed
from tests.fakes.container import make_test_settings
from tests.fakes.meta import FakeMetaClient

CONTRIBUTOR = "muse-spark-1.3-contributor"


def make_case(i: int, source: str = "synthetic:test", **fields) -> EvalCase:
    data = {"id": f"t-{i:03d}", "language_style": "en", "channel": "kiosk",
            "gold_ops": [CartOp(op="add", item_code="SAMOSA", qty=1)],
            "gold_cart_after": [LineSpec(item_code="SAMOSA", qty=1)], "gold_intent": "order", "brief": "one samosa",
            "utterance": f"one samosa please {i}", "source": source} | fields
    return EvalCase.model_validate(data)


def samosa_turn() -> OrderTurnLLM:
    return OrderTurnLLM(language="en", intent="order", ops=[CartOp(op="add", item_code="SAMOSA", qty=1)],
                        closing=False, needs_clarification=False, utterance_en="one samosa",
                        injection_suspected=False)


class FakeProvider:
    """Answers from ``script`` by case id: a turn, or an exception to raise."""

    name = "fake"

    def __init__(self, script=None, model: str = "muse-spark-1.3") -> None:
        self.script = dict(script or {})
        self._model = model
        self.ctxs = []

    @property
    def model(self) -> str:
        return self._model

    async def parse(self, case, ctx):
        self.ctxs.append(ctx)
        out = self.script.get(case.id, samosa_turn())
        if isinstance(out, BaseException):
            raise out
        return out, CallRecord(purpose="order", model=self.model, endpoint="chat", status="ok", cost_usd=0.002,
                               latency_ms=1500, input_tokens=5400, cached_tokens=5300, output_tokens=300,
                               reasoning_tokens=150)


def test_data_class_from_source_never_flag():
    assert runner.data_class_for("synthetic:muse-spark-1.3-contributor") == "synthetic"
    expected = {"public_dataset:fleurs/te_in/1.wav": "public_dataset", "founder_voice:cns_1": "founder_voice",
                "consented:cns_2": "founder_voice", "judge:sbx_1": "judge", "staff:kds": "staff",
                "SYNTHETIC:x": "customer", " synthetic:x": "customer", "synthetic": "customer", "a call": "customer"}
    assert {source: runner.data_class_for(source) for source in expected} == expected
    assert runner.source_kind("synthetic:x") == "synthetic" and runner.source_kind("consented:cns_1") == "real"

    for flag in ({"synthetic": True}, {"data_class": "synthetic"}):  # a record cannot carry its own data class
        with pytest.raises(ValidationError):
            make_case(1, source="consented:cns_1", **flag)
    for fn in (runner.run_cases, runner.case_ctx, runner.check_policy, meta_provider.MetaOrderProvider.parse,
               meta_provider.build):
        assert not {"synthetic", "data_class"} & set(inspect.signature(fn).parameters), fn

    real, synthetic = make_case(1, source="consented:cns_1"), make_case(2)
    ctx = runner.case_ctx(real)
    assert (ctx.data_class, ctx.tenant_id, ctx.session_id, ctx.spend_scope) == ("founder_voice", None, None,
                                                                                "dev:P31a-run")
    assert runner.case_ctx(synthetic).data_class == "synthetic"


async def test_real_data_never_reaches_a_contributor_model():
    dev, test = make_test_settings(environment="dev"), make_test_settings()
    real, synthetic = make_case(1, source="founder_voice:cns_1"), make_case(2)
    provider = FakeProvider(model=CONTRIBUTOR)
    with pytest.raises(PolicyViolation):
        await runner.run_cases([synthetic, real], provider, dev)
    with pytest.raises(PolicyViolation):  # outside dev not even synthetic text goes to a contributor model
        await runner.run_cases([synthetic], provider, test)
    assert provider.ctxs == []  # refused before the first call
    outcome = await runner.run_cases([synthetic], provider, dev)
    assert [r.status for r in outcome.results] == ["ok"] and provider.ctxs[0].data_class == "synthetic"
    standard = FakeProvider()
    outcome = await runner.run_cases([real, synthetic], standard, test)  # Standard models take real data
    assert [c.data_class for c in standard.ctxs] == ["founder_voice", "synthetic"]


async def test_run_cases_results_failures_and_budget_stop():
    cases = [make_case(i) for i in range(6)]
    provider = FakeProvider({"t-001": OrderParseUnavailable("timeout", "muse-spark-1.3"),
                             "t-002": RuntimeError("bug"), "t-004": OrderParseUnavailable("budget", "muse-spark-1.3")})
    seen = []
    outcome = await runner.run_cases(cases, provider, make_test_settings(), scope_cap_usd=0.5, concurrency=1,
                                     on_result=seen.append)
    assert [(r.id, r.status, r.reason) for r in outcome.results] == [
        ("t-000", "ok", None), ("t-001", "unavailable", "timeout"), ("t-002", "error", "RuntimeError"),
        ("t-003", "ok", None)]
    assert outcome.budget_stop and outcome.not_run == ["t-004", "t-005"] and seen == outcome.results
    ok = outcome.results[0]
    assert (ok.cost_usd, ok.input_tokens, ok.cached_tokens, ok.api_latency_ms, ok.record_status) == (
        0.002, 5400, 5300, 1500, "ok")
    assert ok.turn == samosa_turn() and all(c.scope_cap_usd == 0.5 for c in provider.ctxs)


async def test_meta_provider_sends_the_case_cart(tmp_path):
    settings = make_test_settings()
    meta = FakeMetaClient({Purpose.ORDER: [samosa_turn()]}, guard=SpendGuard.from_settings(settings))
    case = make_case(1, cart_before=[LineSpec(item_code="MANGO_LASSI", qty=2)], last_question="anything_else",
                     channel="phone",
                     gold_cart_after=[LineSpec(item_code="MANGO_LASSI", qty=2), LineSpec(item_code="SAMOSA", qty=1)])
    path = tmp_path / "run.jsonl"
    outcome = await runner.run_cases([case], meta_provider.build(meta, settings), settings,
                                     on_result=lambda r: runner.append_result(path, r))
    assert [r.status for r in outcome.results] == ["ok"] and outcome.results[0].record_status == "mock"
    user = meta.calls_for(Purpose.ORDER)[0].kwargs["messages"][-1]["content"]
    assert "CHANNEL: phone" in user and "L1 MANGO_LASSI x2 []" in user and "LAST_AGENT_QUESTION: anything_else" in user
    assert runner.load_results(path) == {case.id: outcome.results[0]}


async def test_ci_replay_gate_2pts():
    assert gate_passes(900, 880) and gate_passes(900, 950) and not gate_passes(900, 879)
    assert gate_passes(905, 885, 2.0) and not gate_passes(905, 884, 2.0)
    if not GATE_PATH.is_file():
        pytest.fail(f"no replay gate baseline: publish one with scripts/eval.py report ({GATE_PATH})")
    baseline = GateBaseline.model_validate_json(GATE_PATH.read_text(encoding="utf-8"))
    ids = set(baseline.ids)
    subset = [c for c in load_cases() if c.id in ids]
    assert len(subset) == len(ids) == baseline.n == 60
    settings = make_test_settings(ai_mode=AIMode.REPLAY, ai_cache_dir=FIXTURE_ROOT)
    client = MetaClient(settings, SpendGuard.from_settings(settings))
    try:
        outcome = await runner.run_cases(subset, meta_provider.build(client, settings), settings, concurrency=8)
    finally:
        await client.aclose()
    missing = [r.id for r in outcome.results if r.reason == "replay_miss"]
    if missing:
        pytest.fail(f"{len(missing)} gate cases have no recording: the o1 prompt, the order schema, the seed menu or "
                    "the ORDER route changed. Re-run and re-publish E1 (scripts/eval.py run, then report).")
    assert all(r.record_status in ("replay", "ok") for r in outcome.results)  # ok only while scripts/rerecord.py runs
    pct, n = exact_pct_x10(subset, {r.id: r for r in outcome.results}, load_seed().menu)
    assert n == baseline.n
    assert gate_passes(baseline.exact_match_pct_x10, pct, baseline.max_drop_points), (
        f"exact match {pct / 10:.1f}% dropped more than {baseline.max_drop_points} points below the published "
        f"{baseline.exact_match_pct_x10 / 10:.1f}%")
