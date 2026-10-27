"""P21-G0 gate scripts (``scripts/measure/order_g0.py``, ``schema_variant.py``) on fakes only: the turn set, cart
scoring, the harness on FakeMetaClient with the real OrderAgent, summary, decision, projections and the full/lite
recommendation. Nothing here touches the network or spends."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from scripts.measure import order_g0 as g0
from scripts.measure import schema_variant as sv

from restaurantos.config import AIMode, Purpose
from restaurantos.meta.errors import MetaAPIError, MetaTimeout
from restaurantos.meta.spend import SpendGuard
from restaurantos.seed.loader import load_seed
from tests.fakes.container import make_test_settings
from tests.fakes.meta import FakeMetaClient


@pytest.fixture
def menu():
    return load_seed().menu


def out(ops: list[dict], closing: bool, language: str = "en", intent: str = "order") -> dict:
    return {"language": language, "intent": intent, "ops": ops, "closing": closing, "question": None,
            "needs_clarification": False, "clarify_candidates": [], "unrecognized": None, "reply_speech": "",
            "reply_en": "", "utterance_en": "x", "injection_suspected": False}


def add(code: str, qty: int = 1, *mods: str) -> dict:
    return {"op": "add", "line_ref": None, "item_code": code, "qty": qty, "modifiers": list(mods)}


def live_run(*outputs):
    settings = make_test_settings(ai_mode=AIMode.LIVE, live_tag="P21-G0-test")
    guard = SpendGuard.from_settings(settings)
    meta = FakeMetaClient({Purpose.ORDER: list(outputs)}, guard=guard)
    return g0.LiveG0(settings=settings, guard=guard, meta=meta), meta


def row(lang: str, wall_ms: int, cart_ok: bool = True, error: str | None = None, cost: float = 0.002) -> dict:
    return {"lang": lang, "wall_ms": wall_ms, "latency_ms": wall_ms - 5, "error": error, "input_tokens": 4300,
            "cached_tokens": 4200, "output_tokens": 180, "reasoning_tokens": 0, "cost_usd": cost,
            "cart_ok": cart_ok, "closing_ok": True}


def test_turns_cover_four_languages_interleaved_and_apply_cleanly(menu):
    assert len(g0.TURNS) == 20 and len({t.id for t in g0.TURNS}) == 20
    assert Counter(t.lang for t in g0.TURNS) == {"te-en": 5, "ta-en": 5, "es": 5, "en": 5}
    assert [t.lang for t in g0.TURNS[:4]] == ["te-en", "ta-en", "es", "en"]
    assert g0.TURNS[0].utterance.startswith("naaku rendu chicken biryani kavali") and g0.TURNS[0].expected == g0.CANON
    assert g0.check_turns(g0.TURNS, menu) == []


def test_cart_counts_ignore_line_split_order_and_modifier_order():
    assert g0.cart_counts([("CHK_BIRY", 2, ()), ("PNR_BM", 1, ())]) == g0.cart_counts(
        [("PNR_BM", 1, ()), ("CHK_BIRY", 1, ()), ("CHK_BIRY", 1, ())])
    mild_no_onion = [("CHK_BIRY", 1, ("MILD", "NO_ONION"))]
    assert g0.cart_counts([("CHK_BIRY", 1, ("NO_ONION", "MILD"))]) == g0.cart_counts(mild_no_onion)
    assert g0.cart_counts([("CHK_BIRY", 2, ())]) != g0.cart_counts(g0.CANON[:2])


def test_check_turns_flags_unknown_dish_and_disallowed_modifier(menu):
    bad = [g0.Turn("x1", "en", "a pizza", (("PIZZA", 1, ()),), False),
           g0.Turn("x2", "en", "mild samosa", (("SAMOSA", 1, ("MILD",)),), False)]
    problems = g0.check_turns(bad, menu)
    assert len(problems) == 2 and problems[0].startswith("x1 expected") and problems[1].startswith("x2 expected")


async def test_measure_scores_turns_on_fake_meta(menu):
    good = out([add("CHK_BIRY"), add("CHK_BIRY", 1, "NO_ONION"), add("PNR_BM")], True, "te-en")
    merged = out([add("CHK_BIRY", 2), add("PNR_BM")], True, "ta-en")  # the onion split is lost
    run, meta = live_run(out([], False, intent="smalltalk"), good, merged, MetaTimeout("slow"))
    lines: list[str] = []
    payload = await g0.measure(run, "full", g0.TURNS[:3], menu=menu, gap_s=0, echo=lines.append)
    await run.aclose()
    te, ta, es = payload["turns"]
    assert payload["warmup"]["error"] is None and payload["warmup"]["status"] == "mock"
    assert te["cart_ok"] and te["closing_ok"] and te["raw"] == good and te["status"] == "mock"
    assert not ta["cart_ok"] and ta["closing_ok"] and ta["got"] == "CHK_BIRYx2 + PNR_BMx1"
    assert es["error"].startswith("timeout: MetaTimeout") and not es["cart_ok"] and es["raw"] is None
    summary = payload["summary"]
    assert (summary["n"], summary["ok"], summary["cart_ok"], summary["errors"]) == (3, 2, 1, {"timeout": 1})
    assert summary["by_lang"]["te-en"]["cart_ok"] == 1 and summary["latency_wall_s"]["n"] == 2
    assert payload["spend_scope"] == "dev:P21-G0" and payload["data_class"] == "synthetic"
    assert payload["decision"]["recommend"] == "full" and payload["prompt"]["examples"] == 8
    assert payload["projections"] == {"note": "no billed turn: nothing to project"}  # fake calls cost $0
    assert all(c.ctx.data_class == "synthetic" and c.ctx.scope_cap_usd == run.scope_cap for c in meta.calls)
    assert all(c.kwargs["model"] == "muse-spark-1.3" and c.kwargs["max_retries"] == 0 for c in meta.calls)
    assert meta.closed and any("cart=WRONG" in line for line in lines)


async def test_measure_sends_no_turn_after_a_failed_warmup(menu):
    run, meta = live_run(MetaAPIError(400, "bad schema"), out([], False))
    payload = await g0.measure(run, "lite", g0.TURNS[:2], menu=menu, gap_s=0, echo=lambda _: None)
    assert payload["turns"] == [] and payload["warmup"]["error"].startswith("api_error")
    assert meta.remaining(Purpose.ORDER) == 1 and payload["prompt"]["examples"] == 4


async def test_measure_stops_after_three_failures_in_a_row(menu):
    run, meta = live_run(out([], False), *(MetaTimeout("slow") for _ in range(4)))
    payload = await g0.measure(run, "full", g0.TURNS[:4], menu=menu, gap_s=0, echo=lambda _: None)
    assert len(payload["turns"]) == 3 and meta.remaining(Purpose.ORDER) == 1


def test_live_run_refuses_mock_mode():
    with pytest.raises(SystemExit, match="RESTOS_AI_MODE=live"):
        g0.LiveG0(settings=make_test_settings())


def test_decide_on_the_warm_p50_gate():
    def summary(*walls):
        return g0.summarize_rows([row("en", w) for w in walls])

    assert g0.decide(summary(1700, 1790, 1900), "full")["recommend"] == "full"
    slow = g0.decide(summary(1700, 1810, 1900), "full")
    assert slow["recommend"] == "lite" and not slow["gate_met"] and "schema_variant" in slow["next"]
    assert "recommend" not in g0.decide(summary(1700), "lite") and g0.decide(summary(), "full")["p50_s"] is None


def test_summary_counts_errors_cold_turns_and_slow_turns():
    rows = [row("te-en", 1500), {**row("te-en", 6000), "cached_tokens": 0}, row("es", 900, False, "timeout: x")]
    s = g0.summarize_rows(rows)
    assert (s["ok"], s["cold_turns"], s["over_prod_timeout"], s["cart_ok"]) == (2, 1, 1, 2)
    assert s["latency_wall_s"]["p50"] == 1.5 and s["by_lang"]["es"]["p50_s"] is None and s["total_usd"] == 0.006


def test_projections_from_the_measured_turn_cost():
    p = g0.projections(0.002, 0.007, 0.0136, make_test_settings())
    assert p["kiosk_voice_order_usd"] == [0.0035, 0.007] and p["phone_order_usd"] == [0.012, 0.018]
    assert p["parses_within_call_cap"] == 35 and p["phone_orders_within_sandbox_cap"] == 22
    assert g0.projections(None, None, 0.0136, make_test_settings()) == {"note": "no billed turn: nothing to project"}


def test_dry_run_checks_turns_and_prints_both_variants():
    lines: list[str] = []
    assert g0.dry_run("full", 4, settings=make_test_settings(), echo=lines.append) == 0
    assert lines[0].startswith("full:") and lines[1].startswith("lite:") and "applies cleanly" in lines[-1]
    with pytest.raises(SystemExit):
        g0.main(["--dry-run", "--turns", "0"])


def summary(p50_ms: int, cart_ok: int, n: int = 20) -> dict:
    return g0.summarize_rows([row("en", p50_ms, i < cart_ok) for i in range(n)])


@pytest.mark.parametrize(("full", "lite", "variant"), [
    ((1700, 20), (1500, 20), "full"),  # full meets the gate
    ((2100, 20), (1600, 19), "lite"),  # full slow, lite faster, one cart within the slack
    ((2100, 20), (2200, 20), "full"),  # lite not faster
    ((2173, 20), (2116, 20), "full"),  # lite 57 ms faster: under the 0.1 s minimum gain (G0, Oct 27)
    ((2100, 20), (1600, 17), "full"),  # lite faster but more carts wrong
])
def test_recommend_lite_only_when_it_helps(full, lite, variant):
    rec = sv.recommend(summary(*full), summary(*lite))
    assert rec["variant"] == variant


def test_recommend_caveat_when_lite_still_misses_the_gate():
    rec = sv.recommend(summary(2600, 20), summary(2100, 20))
    assert rec["variant"] == "lite" and rec["caveats"] == ["lite p50 2.10 s still misses the 1.8 s gate"]


def test_latest_and_compare_use_the_newest_run_over_the_same_turns(tmp_path):
    def payload(ids, p50_ms):
        prompt = {"system_chars": 1, "schema_chars": 1, "examples": 8, "hold_usd": 0.01}
        return {"turns": [{"id": i} for i in ids], "summary": summary(p50_ms, 20), "prompt": prompt}

    folder = tmp_path / "measure"
    folder.mkdir()
    for stamp, ids, p50 in (("20261027T180000Z", ["te1", "ta1"], 2000), ("20261027T190000Z", ["te1"], 1000),
                            ("20261027T170000Z", ["te1", "ta1"], 3000)):
        (folder / f"g0_order_full_{stamp}.json").write_text(json.dumps(payload(ids, p50)), encoding="utf-8")
    (folder / "g0_order_full_20261027T200000Z.json").write_text("{not json", encoding="utf-8")
    path, full = sv.latest(tmp_path, "full", ["te1", "ta1"])
    assert path.name == "g0_order_full_20261027T180000Z.json" and sv.latest(tmp_path, "lite") is None
    comparison = sv.compare(full, payload(["te1", "ta1"], 1500))
    assert comparison["metrics"]["p50_s"] == {"full": 2.0, "lite": 1.5, "delta": -0.5}
    assert comparison["recommendation"]["variant"] == "lite"
    assert sv._write(tmp_path, {"gate": "G0"}, ["te1", "ta1"], (path, full), (None, payload(["te1"], 1)), 0.0) == 0
    assert len(list(folder.glob("g0_variant_*.json"))) == 1
