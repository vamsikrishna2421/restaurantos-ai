"""E1 generation (P31a; MASTER_PLAN §6, CONTRACTS_SEED §15, ARCHITECTURE_OPS §5, D19): gold specs first, the
contributor model only in dev on synthetic contexts, and no generation prompt holding anything found under ``data/``.

Generation runs on ``FakeMetaClient``; the committed dataset is checked against the deterministic specs.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from restaurantos.config import REPO_ROOT, ModelRoute, Purpose
from restaurantos.evals import TEXT_DATASET, DataLeak, DataStrings, load_cases, normalize
from restaurantos.evals.generate import (
    BATCH,
    GEN_BUDGET_USD,
    GEN_SCOPE,
    RULES,
    build_messages,
    build_specs,
    generate,
    require_contributor,
    usable,
)
from restaurantos.evals.run import data_class_for
from restaurantos.meta.errors import BudgetExceeded, PolicyViolation
from restaurantos.meta.spend import SpendGuard
from restaurantos.orders.lexicon import load_lexicons
from restaurantos.seed.loader import load_seed
from tests.fakes.container import make_test_settings
from tests.fakes.meta import FakeMetaClient

CONTRIBUTOR = "muse-spark-1.3-contributor"


@pytest.fixture(scope="module")
def menu():
    return load_seed().menu


@pytest.fixture(scope="module")
def lexicons():
    return load_lexicons()


@pytest.fixture(scope="module")
def specs(menu):
    return build_specs(menu)


def few(specs, per_style: int = 2):
    """The first ``per_style`` specs of every style, plus every homophone spec."""
    seen: Counter[str] = Counter()
    out = []
    for spec in specs:
        if seen[spec.language_style] < per_style or "homophone" in spec.tags:
            seen[spec.language_style] += 1
            out.append(spec)
    return out


class FakeWriter:
    """A scripted contributor: one unique utterance per task, in the task's script; ``drop`` leaves ids out once."""

    def __init__(self, drop: set[str] | None = None) -> None:
        self.drop = set(drop or ())
        self.n = 0

    def __call__(self, call) -> dict:
        tasks = json.loads(call.kwargs["messages"][1]["content"])["tasks"]
        out = []
        for task in tasks:
            if task["id"] in self.drop:
                self.drop.discard(task["id"])
                continue
            self.n += 1
            text = f"నాకు కావాలి {self.n}" if task["id"].startswith("e1-te-") else f"to biryani please {self.n}"
            out.append({"id": task["id"], "utterance": text, "utterance_en": f"I want it {self.n}"})
        return {"utterances": out}


def fake_meta(settings, *outputs) -> FakeMetaClient:
    return FakeMetaClient({Purpose.EVAL: list(outputs)}, guard=SpendGuard.from_settings(settings))


async def test_generation_synthetic_contributor_only(tmp_path, specs, menu, lexicons):
    batch = few(specs)
    for settings in (make_test_settings(),  # environment test: the EVAL route is muse-spark-1.3 there anyway
                     make_test_settings(environment="dev", routes={Purpose.EVAL: ModelRoute(model="muse-spark-1.3")})):
        meta = fake_meta(settings, FakeWriter())
        with pytest.raises(PolicyViolation):
            await generate(meta, settings, batch, data_dir=tmp_path, menu=menu, lexicons=lexicons)
        assert meta.calls == []  # refused before any call

    dev = make_test_settings(environment="dev")
    assert require_contributor(dev) == CONTRIBUTOR
    writer = FakeWriter(drop={batch[0].id})  # the first answer leaves one spec out: it is retried
    meta = fake_meta(dev, *[writer] * 20)
    seen_cases = []
    result = await generate(meta, dev, batch, data_dir=tmp_path, menu=menu, lexicons=lexicons,
                            on_case=seen_cases.append)
    assert [c.id for c in result.cases] == [s.id for s in batch] and result.failed == [] and not result.budget_stop
    assert seen_cases[-1].id == batch[0].id  # the dropped spec came back in a later batch
    calls = meta.calls_for(Purpose.EVAL)
    assert result.calls == len(calls) and len(calls) > len(batch) / BATCH
    for call in calls:
        ctx = call.ctx
        assert call.kwargs["model"] == CONTRIBUTOR and call.kwargs["strict"] is True
        assert (ctx.tenant_id, ctx.session_id, ctx.data_class) == (None, None, "synthetic")
        assert (ctx.spend_scope, ctx.scope_cap_usd) == (GEN_SCOPE, GEN_BUDGET_USD)
        assert call.record is not None and call.record.spend_scope == GEN_SCOPE
    for case, spec in zip(result.cases, batch, strict=True):
        assert case.source == f"synthetic:{CONTRIBUTOR}" and data_class_for(case.source) == "synthetic"
        assert case.model_dump(include=set(type(spec).model_fields)) == spec.model_dump()  # gold untouched

    stopped = await generate(fake_meta(dev, BudgetExceeded("scope", 0.25, 0.25, spend_scope=GEN_SCOPE)), dev, batch,
                             data_dir=tmp_path, menu=menu, lexicons=lexicons)
    assert stopped.budget_stop and stopped.cases == [] and stopped.failed == [s.id for s in batch]


def test_generation_prompt_has_no_data_dir_strings(tmp_path, specs, menu, lexicons):
    planted = tmp_path / "data"
    (planted / "eval_audio" / "fleurs").mkdir(parents=True)
    (planted / "samples").mkdir()
    sentence = ("most of the small islands are independent nations or tied to france and known as luxury beach "
                "resorts")
    (planted / "eval_audio" / "fleurs" / "manifest.jsonl").write_text(
        json.dumps({"file": "te_in/1.wav", "raw_transcription": sentence, "transcription": sentence}) + "\n",
        encoding="utf-8")
    (planted / "samples" / "index.json").write_text(json.dumps([{"id": "smp_1", "transcript": "naaku rendu "
                                                    "chicken biryani kavali please", "transcript_en": "x"}]))
    (planted / "notes.txt").write_text("rendu masala dosa inka oka filter coffee\n", encoding="utf-8")
    (planted / "ai_calls.jsonl").write_text(json.dumps({"text": RULES[0]}) + "\n")  # the ledger is never scanned
    guards = [DataStrings.scan(planted), DataStrings.scan(REPO_ROOT / "data")]  # the real data/ where it exists
    assert len(guards[0].texts) == 2 and guards[0].shingles

    prompts = []
    for style in dict.fromkeys(s.language_style for s in specs):
        rows = [s for s in specs if s.language_style == style]
        prompts += ["\n".join(m["content"] for m in build_messages(rows[i:i + BATCH], style, menu, lexicons))
                    for i in range(0, len(rows), BATCH)]
    assert len(prompts) == 72
    for prompt in prompts:
        for guard in guards:
            guard.check(prompt)

    # positive controls: a planted transcript in any case and punctuation, an 8-word run of a long one, a short one
    for leaked in (prompts[0] + "\n" + sentence.upper() + "!", "x " + " ".join(sentence.split()[3:11]) + " y",
                   "Naaku rendu chicken-biryani kavali, please."):
        with pytest.raises(DataLeak):
            guards[0].check(leaked)


async def test_generate_checks_every_prompt_before_sending(tmp_path, specs, menu, lexicons):
    data = tmp_path / "data" / "measure"
    data.mkdir(parents=True)
    (data / "probe.json").write_text(json.dumps({"reference": RULES[0]}), encoding="utf-8")  # as if transcribed
    dev = make_test_settings(environment="dev")
    meta = fake_meta(dev, FakeWriter())
    with pytest.raises(DataLeak):
        await generate(meta, dev, few(specs), data_dir=tmp_path / "data", menu=menu, lexicons=lexicons)
    assert meta.calls == []


def test_build_specs_deterministic_and_balanced(specs, menu):
    assert [s.model_dump() for s in build_specs(menu)] == [s.model_dump() for s in specs]
    assert len(specs) == 360 and len({s.id for s in specs}) == 360
    for style, rows in ((st, [s for s in specs if s.language_style == st]) for st in dict.fromkeys(
            s.language_style for s in specs)):
        assert len(rows) == 60 and Counter(s.channel for s in rows) == {"kiosk": 30, "phone": 30}
        hard = [s for s in rows if {"self_correction", "split_modifier", "homophone", "absurd_qty", "off_menu",
                                    "allergen_q", "injection"} & set(s.tags)]
        assert len(hard) == 15, style
        assert any("homophone" in s.tags for s in rows) == (style not in ("te", "es"))
        assert all(("native_script" in s.tags) == (style == "te") for s in rows)
    assert all(s.last_question == ("anything_else" if s.cart_before else None) for s in specs)
    assert {s.gold_intent for s in specs if s.gold_injection} == {"order", "off_topic"}
    assert all(s.gold_question and s.gold_question.topic == "allergen" for s in specs if "allergen_q" in s.tags)


def test_usable_checks_script_and_homophone(specs):
    te = next(s for s in specs if s.language_style == "te")
    roman = next(s for s in specs if s.language_style == "te-en" and "homophone" not in s.tags)
    homophone = next(s for s in specs if "homophone" in s.tags)
    assert usable(te, "నాకు రెండు చికెన్ బిర్యానీ కావాలి", "I want two chicken biryanis")
    assert not usable(te, "naaku rendu chicken biryani kavali", "I want two chicken biryanis")  # not in script
    assert usable(roman, "naaku rendu chicken biryani kavali", "I want two chicken biryanis")
    assert not usable(roman, "నాకు రెండు chicken biryani kavali", "I want two chicken biryanis")  # mixed scripts
    assert not usable(roman, "naaku rendu chicken biryani kavali", "")  # no gloss
    assert usable(homophone, "to paneer tikka please", "to paneer tikka please")
    assert not usable(homophone, "two paneer tikka please", "two paneer tikka please")  # 'to' never written


@pytest.mark.skipif(not TEXT_DATASET.is_file(), reason="the E1 dataset is not generated yet")
def test_committed_dataset_is_gold_first(specs):
    cases = load_cases()
    by_id = {s.id: s for s in specs}
    assert len(cases) >= 300  # failed specs are listed by scripts/eval.py generate, never hidden
    texts = set()
    for case in cases:
        spec = by_id[case.id]
        assert case.model_dump(include=set(type(spec).model_fields)) == spec.model_dump(), case.id  # gold untouched
        assert case.source == f"synthetic:{CONTRIBUTOR}" and data_class_for(case.source) == "synthetic"
        assert usable(spec, case.utterance, case.utterance_en), case.id
        texts.add(normalize(case.utterance))
    assert len(texts) == len(cases)
