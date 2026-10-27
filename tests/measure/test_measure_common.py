"""scripts/measure (P21): the pure helpers (percentiles, CER/WER, the μ-law phone channel, speech offsets, framing,
scripts, ledger sums), the G3 scanner, report and checks, the schema-check helpers, and both live harnesses against
fakes. Nothing touches the network: realtime ASR talks to a scripted socket and the GM stream to FakeMetaClient."""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest
from pydantic import SecretStr

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # `scripts.measure` lives at the repo root, outside src/
    sys.path.insert(0, str(REPO))

from scripts.measure import (  # noqa: E402
    asr_accuracy,
    common,
    endpointing_latency,
    gm_schema_ab,
    nullable_enum,
    schema_cache,
    summary_mode,
)
from tests.fakes.meta import FakeMetaClient  # noqa: E402

from restaurantos.config import AIMode, Purpose, Settings  # noqa: E402
from restaurantos.contracts.common import CallCtx  # noqa: E402
from restaurantos.contracts.schemas import GM_PLAN_SCHEMA, build_order_turn_schema, lint_strict  # noqa: E402
from restaurantos.meta.client import StreamEvent  # noqa: E402
from restaurantos.seed.loader import load_seed  # noqa: E402

RATE = 16_000


def live_settings(tmp_path: Path, **kw) -> Settings:
    base = {"model_api_key": SecretStr("test-key-not-real"), "data_dir": tmp_path, "ai_mode": AIMode.LIVE,
            "live_tag": "P21", "environment": "test", "key_files": False}
    return Settings(_env_file=None, **(base | kw))


def tone(freq: float, seconds: float, amp: float = 8000.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return common.to_int16(amp * np.sin(2 * np.pi * freq * t))


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


# ----- statistics and error rates -----------------------------------------------------------------------------------
def test_percentile_nearest_rank():
    assert common.percentile(range(1, 21), 95) == 19 and common.percentile(range(1, 21), 50) == 10
    assert common.percentile([3, 1, 2], 50) == 2 and common.percentile([5], 95) == 5
    assert common.percentile([4, 9, 1], 100) == 9 and common.percentile([4, 9, 1], 0) == 1
    assert common.percentile([], 50) is None


def test_summarize_skips_none():
    assert common.summarize([1.0, None, 2.0, 3.0]) == {"n": 3, "p50": 2.0, "p95": 3.0, "mean": 2.0, "min": 1.0,
                                                       "max": 3.0}
    assert common.summarize([None])["n"] == 0 and common.summarize([])["p95"] is None


def test_normalize_text_case_punctuation_format_chars():
    assert common.normalize_text("Hello, World! Don't — stop.") == "hello world dont stop"
    assert common.normalize_text("¿Qué tal?  Bien…") == "qué tal bien"  # accents stay
    assert common.normalize_text("ఎక్స్‌ట్రా") == "ఎక్స్ట్రా"  # ZWNJ dropped, vowel signs kept
    assert common.normalize_text("नमस्ते। 802.11a") == "नमस्ते 802 11a"


def test_word_errors_and_corpus_rate():
    assert common.edit_distance("kitten", "sitting") == 3
    assert common.errors("the cat sat", "the cat sat on", "word") == (1, 3)  # insertion
    assert common.errors("a b c", "a x c", "word") == (1, 3) and common.errors("a b c", "a c", "word") == (1, 3)
    assert common.errors("Hello, world.", "hello world", "word") == (0, 2)
    assert common.error_rate([(1, 10), (1, 2)]) == round(2 / 12, 4)  # corpus-weighted, not the mean of rates
    assert common.error_rate([]) is None


def test_character_errors_ignore_spaces():
    assert common.unit_for("te") == common.unit_for("ta") == "char" and common.unit_for("es") == "word"
    assert common.errors("చిన్న ద్వీపాలలో", "చిన్నద్వీపాలలో", "char") == (0, 14)
    edits, n = common.errors("இது வேதியியல்", "இது வேதியில்", "char")
    assert (edits, n) == (1, 12)  # code points, vowel signs included: one ய dropped


def test_script_share_flags_wrong_script():
    assert common.script_share("తెలుగు", "te") == 1.0 and common.script_share("hello", "en") == 1.0
    assert common.script_share("ಮುಖ್ಯೆ ಮೇಳಗಳಲ್ಲಿ", "ta") == 0.0  # Tamil speech returned in Kannada letters
    assert common.script_share("123 ...", "ta") is None


# ----- audio --------------------------------------------------------------------------------------------------------
def test_phone_degrade_keeps_length_voiceband_and_snr():
    clean = tone(1000, 1.0)
    out = common.phone_degrade(clean)
    assert out.dtype == np.int16 and len(out) == len(clean)
    mid = slice(1600, 14400)  # skip the edges (filter delay)
    assert abs(20 * np.log10(rms(out[mid]) / rms(clean[mid]))) < 1.0
    lags = range(0, 200)
    lag = max(lags, key=lambda k: float(np.dot(clean[1600:14400].astype(float), out[1600 + k:14400 + k])))
    noise = out[1600 + lag:14400 + lag].astype(float) - clean[1600:14400].astype(float)
    assert 20 * np.log10(rms(clean[mid]) / (rms(noise) + 1e-9)) > 25  # μ-law quantization noise ~35 dB down
    high = tone(6000, 1.0)  # above the 4 kHz phone band: removed, not aliased
    assert rms(common.phone_degrade(high)[mid]) < rms(high[mid]) * 10 ** (-30 / 20)
    assert not common.phone_degrade(np.zeros(3200, dtype=np.int16)).any()


def test_speech_end_adapts_to_background():
    rng = np.random.default_rng(0)
    quiet, loud_bg = (common.to_int16(rng.normal(0, s, RATE)) for s in (20.0, 200.0))
    for bg in (quiet, loud_bg):  # 8000-amplitude tone ≈ −29 dB above the louder background
        pcm = np.concatenate([bg, tone(300, 1.0), bg])
        assert common.speech_end_s(pcm) == pytest.approx(2.0, abs=0.04)
    assert common.speech_end_s(np.zeros(RATE, dtype=np.int16)) is None


def test_frames_are_80ms_and_pad_the_last():
    assert [len(f) for f in common.frames(np.zeros(1280, dtype=np.int16))] == [2560]
    chunks = common.frames(np.ones(1281, dtype=np.int16))
    assert len(chunks) == 2 and chunks[1][:2] == b"\x01\x00" and chunks[1][2:] == bytes(2558)
    assert common.asr_plan_usd([0.2, 10.0]) == pytest.approx((1 + 11) * 0.18 / 3600, abs=1e-9)


# ----- clips, keywords and the ledger -------------------------------------------------------------------------------
def test_load_fleurs_filters_and_marks_public_dataset(tmp_path):
    rows = [{"file": f"{c}/{i}.wav", "lang": lang, "raw_transcription": f"s{i}"}
            for i, (c, lang) in enumerate([("te_in", "te"), ("te_in", "te"), ("ta_in", "ta")])]
    (tmp_path / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    clips = common.load_fleurs(per_lang=1, root=tmp_path)
    assert [(c.lang, c.reference, c.data_class) for c in clips] == [("te", "s0", "public_dataset"),
                                                                     ("ta", "s2", "public_dataset")]
    assert [c.lang for c in common.load_fleurs(langs=["te"], root=tmp_path)] == ["te", "te"]
    assert common.order_probes(tmp_path) == []


def test_menu_keywords_capped_and_unique():
    words = common.menu_keywords()
    assert len(words) == 100 and len({w.casefold() for w in words}) == 100
    assert "Chicken Dum Biryani" in words and len(common.menu_keywords(5)) == 5


def test_ledger_spend_filters(tmp_path):
    lines = [{"cost_usd": 0.1, "live_tag": "P21", "trace_id": "p21g1_A", "spend_scope": "dev:P21"},
             {"cost_usd": 0.2, "live_tag": "P21", "trace_id": "p21g3_B", "spend_scope": "dev:P21"},
             {"cost_usd": 0.4, "live_tag": "P13", "trace_id": "tr_C", "spend_scope": "dev:P13"}]
    path = tmp_path / "ai_calls.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n[1]\n", encoding="utf-8")
    assert common.ledger_spend(path, tag="P21") == {"calls": 2, "usd": 0.3}
    assert common.ledger_spend(path, trace_prefix="p21g1_") == {"calls": 1, "usd": 0.1}
    assert common.ledger_spend(path, scope="dev:P13")["usd"] == 0.4
    assert common.ledger_spend(tmp_path / "missing.jsonl") == {"calls": 0, "usd": 0.0}


# ----- live harness (fakes) -----------------------------------------------------------------------------------------
def test_live_run_refuses_mock_and_caps_scope(tmp_path):
    with pytest.raises(SystemExit):
        common.LiveRun("g1", settings=live_settings(tmp_path, ai_mode=AIMode.MOCK))
    settings = live_settings(tmp_path)
    settings.ledger_path.write_text("\n".join(json.dumps(
        {"purpose": "asr", "model": "m", "endpoint": "asr_realtime", "status": "ok", "cost_usd": c, "trace_id": t,
         "spend_scope": "dev:P21", "live_tag": "P21", "ts": 1.8e9}) for c, t in ((0.1, "p21g1_X"), (0.05, "p21g3_Y")))
        + "\n", encoding="utf-8")
    run = common.LiveRun("g1", settings=settings)
    assert run.remaining == 0.4 and run.scope_cap == pytest.approx(0.55)
    ctx = run.ctx("public_dataset")
    assert ctx.spend_scope == "dev:P21" and ctx.trace_id.startswith("p21g1_") and ctx.scope_cap_usd == run.scope_cap
    assert common.LiveRun("g1", settings=settings, max_usd=0.02).remaining == 0.02


def test_patch_pricing_handles_nullable_schema_types():
    common.patch_pricing()
    body = {"model": "muse-spark-1.3", "max_output_tokens": 10, "text": {"format": {"schema": GM_PLAN_SCHEMA}},
            "input": [{"type": "input_image", "image_url": "https://x"}]}
    est, images = common.pricing.estimate_body_usd("muse-spark-1.3", body)
    assert est > 0 and images == 1


class FakeWS:
    """Acks the handshake, then plays ``script`` once endStream arrives and closes with 1000."""

    def __init__(self, script: list[dict], ack: dict | None = None) -> None:
        self.sent: list = []
        self.script, self.close_code, self.close_reason = script, None, ""
        self._q: asyncio.Queue = asyncio.Queue()
        self._q.put_nowait(json.dumps({"sessionId": "srv"} if ack is None else ack))

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "endStream":
            for ev in self.script:
                self._q.put_nowait(json.dumps(ev))
            self._q.put_nowait(None)

    async def recv(self):
        return await self._q.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._q.get()
        if msg is None:
            self.close_code = 1000
            raise StopAsyncIteration
        return msg

    async def close(self):
        self.close_code = self.close_code or 1000


def connect_to(ws: FakeWS):
    async def connect(url, **kwargs):
        return ws
    return connect


async def test_asr_session_ptt_latency_and_billing(tmp_path):
    run = common.LiveRun("g1", settings=live_settings(tmp_path))
    clip = common.Clip("probe:x", "en", tmp_path / "x.wav", None, "synthetic")
    ws = FakeWS([{"type": "transcript", "transcript": "two biryani", "final": True, "audioProcessedMs": 240}])
    res = await common.run_asr_session(run, clip, np.ones(3 * 1280, dtype=np.int16), arm="none", mode="PUSH_TO_TALK",
                                       keywords=["biryani"], connect=connect_to(ws))
    assert res.error is None and res.text == "two biryani" and res.audio_s == 0.24
    assert res.release_t == pytest.approx(0.24, abs=0.05) and 0 <= res.ptt_latency() < 0.1
    handshake = json.loads(ws.sent[0])
    assert handshake["mode"] == "PUSH_TO_TALK" and handshake["keywords"] == ["biryani"]
    assert sum(isinstance(x, bytes) for x in ws.sent) == 3 and json.loads(ws.sent[-1]) == {"type": "endStream"}
    assert res.cost_usd == pytest.approx(0.18 / 3600)  # one whole second at least
    assert res.events[-1]["type"] == "closed" and res.events[0]["audio_ms"] == 240


async def test_asr_session_endpointing_turns_and_errors(tmp_path):
    run = common.LiveRun("g1", settings=live_settings(tmp_path))
    clip = common.Clip("fleurs:te/1.wav", "te", tmp_path / "1.wav", "ref", "public_dataset")
    ws = FakeWS([{"type": "speechStart", "turnId": 1, "audioProcessedMs": 80},
                 {"type": "speechEnd", "turnId": 1, "audioProcessedMs": 160},
                 {"type": "speechComplete", "turnId": 1, "transcript": "hi.", "audioProcessedMs": 160}])
    res = await common.run_asr_session(run, clip, np.ones(1280, dtype=np.int16), arm="16k", mode="ENDPOINTING",
                                       trailing_silence_s=0.08, connect=connect_to(ws))
    assert res.text == "hi." and len(res.turn_latencies()) == 1 and res.turn_latencies()[0] >= 0
    stats = endpointing_latency.clip_stats(res, offset=0.05)
    assert stats["turns"] == 1 and stats["endpointer_boundary_after_offset_s"] == pytest.approx(0.11)
    assert stats["closed_at_end_stream"] is True
    bad = await common.run_asr_session(run, clip, np.ones(1280, dtype=np.int16), arm="16k", mode="ENDPOINTING",
                                       connect=connect_to(FakeWS([], ack={"message": "bad keywords"})))
    assert bad.error and "bad keywords" in bad.error and bad.text == ""  # recorded, not raised


# ----- G1 analysis --------------------------------------------------------------------------------------------------
def _res(arm: str, clip: str, lang: str, text: str) -> common.AsrResult:
    return common.AsrResult(clip, lang, arm, "PUSH_TO_TALK", 1.0,
                            events=[{"t": 1.0, "type": "final", "text": text}] if text else [])


def test_accuracy_paired_deltas_and_bias_rule():
    refs = {"c1": "a b c d e", "c2": "నమస్తే అండి", "c3": "x y z w v"}
    results = [_res("16k-none", "c1", "en", "a b c d e"), _res("16k-bias", "c1", "en", "a b c d e"),
               _res("16k-none", "c2", "te", "ನಮಸ್ತೆ ಅಂಡಿ"), _res("16k-bias", "c2", "te", "నమస్తే అండి"),
               _res("16k-none", "c3", "en", "x y z w v"), _res("16k-bias", "c3", "en", "")]
    table = asr_accuracy.score(results, refs)
    assert table["16k-none"]["te"]["wrong_script"] == 1 and table["16k-bias"]["en"]["failed"] == 1
    cells = asr_accuracy.paired(results, refs, "16k-none", "16k-bias")
    assert cells["en"]["n"] == 1 and cells["te"]["delta"] < -0.5  # c3 failed in one arm: not paired
    assert asr_accuracy.recommend_bias({"16k": cells})["language_bias"] == common.BIAS_ALL
    worse = {"16k": {"en": {"delta": 0.03}, "te": {"delta": -0.05}}}
    assert asr_accuracy.recommend_bias(worse)["language_bias"] is None


# ----- G3 -----------------------------------------------------------------------------------------------------------
PLAN = ('{"actions":[{"candidate_id":"C06","message":null,"message_en":null,"rationale":"a {b} [c] \\"q\\""},'
        '{"candidate_id":"C04","message":null,"message_en":null,"rationale":"x"},'
        '{"candidate_id":"C16","message":"m","message_en":"m","rationale":"y"}],'
        '"summary":"s","severity":"high","confidence":"medium","questions_for_manager":["{q}"]}')


def test_action_scanner_any_chunking():
    rng = random.Random(7)
    for _ in range(50):
        cuts = sorted(rng.sample(range(1, len(PLAN)), 12))
        scanner, counts = summary_mode.ActionScanner(), []
        for a, b in zip([0, *cuts], [*cuts, len(PLAN)], strict=True):
            counts.append(scanner.feed(PLAN[a:b]))
        assert counts[-1] == 3 and counts == sorted(counts)
    scanner = summary_mode.ActionScanner()
    first_end = PLAN.index("},{") + 1  # braces inside the rationale string do not count
    assert scanner.feed(PLAN[: first_end - 1]) == 0 and scanner.feed(PLAN[first_end - 1]) == 1


def test_idle_threshold_floor_and_margin():
    assert summary_mode.idle_threshold([3.0, 5.0, 9.0]) == 13.0
    assert summary_mode.idle_threshold([1.0, None]) == 12.0 and summary_mode.idle_threshold([]) is None


async def test_run_stream_times_events_with_fake_client():
    events = [StreamEvent("progress", data={"type": "response.created"}), StreamEvent("reasoning", text="Weighing."),
              PLAN[:150], PLAN[150:]]
    client = FakeMetaClient({Purpose.GM: [events]})
    ticks = iter([0.0, 0.5, 2.0, 2.2, 3.0, 7.0])
    ctx = CallCtx(trace_id="p21g3_T", data_class="synthetic", spend_scope="dev:P21")
    request = {"input": "r", "instructions": "i", "reasoning_summary": "concise",
               "text_format": {"type": "json_schema"}}
    row = await summary_mode.run_stream(client, ctx, request, clock=lambda: next(ticks))
    assert row["error"] is None and row["t"] == {"first_event": 0.5, "first_summary": 2.0, "first_text": 2.2,
                                                 "first_action": 2.2, "done": 7.0}
    assert row["action_t"] == [2.2, 3.0, 3.0] and row["max_gap_s"] == 4.0 and row["max_gap_after_first_s"] == 4.0
    assert row["summary_chars"] == 9 and row["text"] == PLAN
    call = client.calls[0]
    assert call.kwargs["cache_key"] == "gm-g1" and call.kwargs["effort"] == "low"
    assert call.kwargs["extra_body"] == {"prompt_cache_retention": "24h"}


def test_aggregate_and_recommend_pick_mode_and_threshold():
    def row(arm, summary_t, gap, chars=40):
        return {"arm": arm, "error": None, "t": {"first_summary": summary_t, "done": 10.0, "first_action": 6.0},
                "max_gap_s": gap, "max_gap_after_first_s": gap, "summary_chars": chars, "action_t": [6.0],
                "usage": {}, "cost_usd": 0.01, "checks": {"valid_json": True}}
    rows = [row("ids-concise", 2.0, 5.0), row("ids-concise", 2.5, 9.5), row("ids-auto", 2.4, 3.0),
            row("ids-auto", 2.2, 4.0, chars=0)]
    agg = summary_mode.aggregate(rows, ["ids-concise", "ids-auto"])
    assert agg["ids-auto"]["summary_empty"] == 1 and agg["ids-concise"]["idle_threshold_s"] == 13.5
    rec = summary_mode.recommend(agg)
    assert rec["summary_mode"] == "concise" and rec["idle_threshold_s"] == 13.5


def test_situation_report_from_seed():
    report, catalog = gm_schema_ab.build_report("inc_TEST")
    ing = report.ingredient
    assert (ing.on_hand, ing.reserved, ing.free, ing.burn_per_h, ing.eta_min) == (1610, 810, 800, 1200, 40)
    assert report.incident.band == "act" and report.tainted and report.notes[0].supplier_id == "SUP_PATEL"
    assert 10 <= len(report.candidates) <= 16
    assert set(report.baseline_candidate_ids) <= {c.id for c in report.candidates}
    pos = {c.target: c.params["packs"] for c in report.candidates if c.action_type == "create_purchase_order"}
    assert pos["PO:SUP_PATEL:bridge"] == 3 and pos["PO:SUP_PATEL:close"] == 4  # the note says 3 packs are left
    assert {a.code for a in report.alternatives} == {"VEG_KORMA", "CHANA_MASALA", "GOBI_MAN", "SAMOSA"}
    assert {c["supplier_id"] for c in catalog} == {"SUP_PATEL", "SUP_FRESHDAIRY"}
    assert lint_strict(gm_schema_ab.FREEFORM_SCHEMA) == [] and gm_schema_ab.SCHEMAS["ids"] is GM_PLAN_SCHEMA
    request, _ = gm_schema_ab.make_request("free-auto")
    assert '"candidates"' not in request["input"] and '"supplier_catalog"' in request["input"]
    assert request["reasoning_summary"] == "auto" and request["text_format"]["strict"] is True


def test_plan_checks_ids_and_free():
    report, _ = gm_schema_ab.build_report("inc_TEST")
    checks = gm_schema_ab.plan_checks("ids-concise", PLAN, report)
    assert checks["schema_valid"] and checks["unknown_ids"] == ["C16"] and checks["purchase_orders"] == ["SUP_PATELx3"]
    assert checks["patel_over_note"] is False and gm_schema_ab.plan_checks("ids-auto", "{", report) == {
        "valid_json": False}
    free = {"actions": [{"action_type": "create_purchase_order", "target": "t", "rationale": "r", "params": {
        "supplier_id": "SUP_PATEL", "ingredient_id": "ING_PANEER", "packs": 4, "qty": 4000, "cost_cents": 4400,
        "eta_biz": "2026-11-07T19:28:00-05:00"}}, {"action_type": "set_item_availability", "target": "PNR_TIKKA",
        "rationale": "r", "params": {"item_code": "PNR_TIKKA", "available": False}}], "summary": "s"}
    checks = gm_schema_ab.plan_checks("free-auto", json.dumps(free), report)
    assert checks["params_valid"] == 2 and checks["patel_over_note"] is True and checks["stopped"] == ["PNR_TIKKA"]


# ----- schema checks ------------------------------------------------------------------------------------------------
def test_conforms_both_nullable_styles():
    menu = load_seed().menu
    for style in nullable_enum.STYLES:
        schema = build_order_turn_schema(menu, "full", nullable_enum_style=style)
        out = {"language": "en", "intent": "question", "ops": [], "closing": False,
               "question": {"topic": "hours", "item_code": None, "allergen": None}, "needs_clarification": False,
               "clarify_candidates": [], "unrecognized": None, "reply_speech": "r", "reply_en": "r",
               "utterance_en": "u", "injection_suspected": False}
        assert nullable_enum.conforms(out, schema) == []
        bad = out | {"question": {"topic": "hours", "item_code": "NOPE", "allergen": None}, "extra": 1}
        assert len(nullable_enum.conforms(bad, schema)) == 2
        assert nullable_enum.nullable_values(out)["question.allergen"] == [None]
    rows = [{"style": "anyOf", "accepted": True, "findings": []}, {"style": "type_array", "accepted": False}]
    assert nullable_enum.recommend(rows)["nullable_enum_style"] == "anyOf"
    refused = [{"style": "anyOf", "accepted": False}, {"style": "type_array", "accepted": True, "findings": []}]
    assert nullable_enum.recommend(refused)["nullable_enum_style"] == "type_array"


def test_schema_cache_helpers():
    full = build_order_turn_schema(load_seed().menu, "full")
    changed = schema_cache.with_extra_item(full)
    assert "SPECIAL_THALI" in json.dumps(changed) and "SPECIAL_THALI" not in json.dumps(full)
    assert lint_strict(changed) == []
    rows = [{"i": 0, "schema": "A", "switch": None, "error": None, "cached_tokens": 0},
            {"i": 1, "schema": "A", "switch": "A->A", "error": None, "cached_tokens": 2000},
            {"i": 2, "schema": "B", "switch": "A->B", "error": None, "cached_tokens": 1990}]
    assert schema_cache.verdict(rows)["survives"] is True
    rows[2]["cached_tokens"] = 0
    assert schema_cache.verdict(rows)["survives"] is False
    rows[1]["cached_tokens"] = 0
    assert schema_cache.verdict(rows)["survives"] is None
