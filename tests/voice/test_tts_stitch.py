"""Stitched read-back (P15b): replies spoken from pre-rendered segments when live TTS cannot speak them, the segment
cover in every speak language, and the TTS 429 alert on the adapter's live path."""

from __future__ import annotations

import logging
from contextlib import aclosing

import pytest

from restaurantos.contracts.common import SPEAK_LANGS
from restaurantos.contracts.orders import ApplyResult, RejectedOp, Reply
from restaurantos.contracts.ports import StitchPort
from restaurantos.orders.reply_policy import ReplyView, choose
from restaurantos.orders.templates import (
    AllergenNames,
    Count,
    Dish,
    Dishes,
    Items,
    Money,
    OrderNo,
    Spice,
    Templates,
    load_phrase_files,
)
from restaurantos.voice import tts as voice_tts
from restaurantos.voice.tts import TTSAdapter, TTSUnavailable, prerender, stitch
from restaurantos.voice.tts.base import VoiceProfile, frames, silence
from restaurantos.voice.tts.cache import AudioCache, PhraseCache
from restaurantos.voice.tts.stitch import PAUSES, Piece, SegmentIndex, Stitcher, cover, normalize
from restaurantos.voice.tts.vendor_meter import VendorMeter
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container
from tests.fakes.order_agent import add_op, make_cart, seed_menu
from tests.fakes.tts import FakeTTSEngine

URL = "restaurantos.fly.dev"
PROFILES = {lang: VoiceProfile("fake", f"model_{lang}", "voice_1") for lang in SPEAK_LANGS}
CANONICAL = (("CHK_BIRY", 1, ()), ("CHK_BIRY", 1, ("NO_ONION",)), ("PNR_BM", 1, ()))  # CONTRACTS_SEED §11


def release_adapter(tmp_path, engine, *, kill: list[bool] | None = None) -> TTSAdapter:
    """The TTS adapter the release step uses (a fake vendor; the pre-render is batch work)."""
    meter = VendorMeter(tmp_path / "vendor_usage.jsonl", clock=FakeClock(),
                        kill_switch=(lambda: bool(kill and kill[0])))
    return TTSAdapter(engine, cache=AudioCache(tmp_path / "tts_cache", min_free_bytes=0), meter=meter,
                      profiles=PROFILES, auto_refresh=False)


def reviewed_files():
    """The phrase files with te, ta and es signed off, as if the founders had reviewed them."""
    return {lang: f if f.review_status == "reviewed" else
            f.model_copy(update={"review_status": "reviewed", "reviewer": "test"})
            for lang, f in load_phrase_files().items()}


def canonical_replies(templates: Templates, lang: str, menu) -> dict[str, Reply]:
    """The canonical order's read-back, its ack and a sold-out offer, through the Dialogue's own reply policy."""
    cart = make_cart(CANONICAL, menu=menu)
    view = ReplyView(menu=menu, cart=cart, minutes=25, channel="phone")  # the phone's wording ("press 1")
    adds = [add_op(code, qty, *mods) for code, qty, mods in CANONICAL]
    gone = RejectedOp(op=add_op("PNR_BM"), reason="unavailable", alternatives=["PALAK_PNR", "CHK_TIKKA_MASALA"])
    return {"readback": choose(None, None, "readback", lang, templates, view=view),
            "ack_added": choose(None, ApplyResult(cart=cart, applied=adds), "take_order", lang, templates, view=view),
            "sold_out_offer": choose(None, ApplyResult(cart=cart, applied=adds[:2], rejected=[gone]),
                                     "sold_out_offer", lang, templates, view=view)}


def squash(text: str) -> str:
    """Words only: what a cover must say, whatever pauses it puts between them."""
    return " ".join("".join(" " if ch in PAUSES else ch for ch in normalize(text)).split())


async def speak(tts, text: str, fmt: str = "ulaw_8000", priority: str = "phone") -> bytes:
    async with aclosing(tts.stream(text, "en", fmt, priority=priority)) as audio:
        return b"".join([frame async for frame in audio])


async def expected_audio(stitcher: Stitcher, phrases: PhraseCache, reply: Reply, fmt: str) -> bytes:
    out = b""
    for piece in await stitcher.plan(reply.text, reply.lang):
        out += silence(fmt, piece.ms) if piece.key is None else await phrases.get(piece.key, reply.lang, fmt)
    return out


async def test_stitched_readback_plays_with_tts_disabled(tmp_path):
    """No ElevenLabs key (live TTS off): the canonical read-back still plays, stitched from the segments the release
    step rendered earlier, in μ-law for the phone and PCM for the kiosk, with no vendor call and no characters. The
    kill switch and a failing vendor fall back the same way."""
    c = make_test_container()
    voice_tts.provide(c)
    stitch.provide(c)
    assert not c.tts.available and isinstance(c.stitcher, Stitcher) and isinstance(c.stitcher, StitchPort)

    files, menu = load_phrase_files(), seed_menu()
    specs = prerender.plan_phrases(files, menu, ["en"], url=URL)
    engine = FakeTTSEngine(ms_per_char=5)
    phrases = PhraseCache(c.phrases.root, min_free_bytes=0)  # the same folder the app's phrase cache reads
    done = await prerender.apply(specs, tts=release_adapter(tmp_path, engine), phrases=phrases)
    assert not done.failed and len(done.rendered) == len(specs)
    requests = len(engine.requests)

    reply = canonical_replies(Templates(files, defaults={"url": URL}), "en", menu)["readback"]
    assert reply.text == ("One Chicken Dum Biryani, one Chicken Dum Biryani (no onion) and one Paneer Butter Masala. "
                          "Fifty-two dollars and ninety-three cents, about twenty-five minutes. Say yes or press 1.")
    with pytest.raises(TTSUnavailable) as exc:
        await speak(c.tts, reply.text)
    assert exc.value.reason == "not_configured"

    said = {spec.key: spec.match for spec in specs}
    pieces = await c.stitcher.plan(reply.text, "en")
    assert [said[p.key] if p.key else p.ms for p in pieces] == [
        "one", "chicken dum biryani", 150, "one", "chicken dum biryani", 150, "no onion", 150, "and", "one",
        "paneer butter masala", 300, "fifty-two", "dollars", "and", "ninety-three", "cents", 150, "about",
        "twenty-five", "minutes. say yes or press 1"]
    for fmt in ("ulaw_8000", "pcm_16000"):
        audio = await c.stitcher.render(reply, {}, "en", fmt)
        assert audio and audio == await expected_audio(c.stitcher, phrases, reply, fmt)
        if fmt == "ulaw_8000":  # what the phone line sends: whole 160-byte media frames, 1-4 s of speech here
            assert all(len(frame) == 160 for frame in frames(audio, fmt)) and 1.0 < len(audio) / 8000 < 4.0
    assert len(engine.requests) == requests and c.tts.meter.month_used() == 0
    assert c.stitcher.stats()["renders"] == 2 and c.stitcher.stats()["segments"] == {"en": len(specs)}

    async def say(tts) -> bytes:  # the fallback chain a phone turn runs: live TTS, else the stitched reply
        try:
            return await speak(tts, reply.text)
        except TTSUnavailable:
            return await c.stitcher.render(reply, {}, reply.lang, "ulaw_8000") or b""

    kill = [True]
    live = release_adapter(tmp_path / "live", FakeTTSEngine(), kill=kill)
    assert await say(live) == await expected_audio(c.stitcher, phrases, reply, "ulaw_8000")  # kill switch on
    kill[0] = False
    broken = release_adapter(tmp_path / "broken", FakeTTSEngine(fail=TTSUnavailable("upstream")))
    assert await say(broken) == await expected_audio(c.stitcher, phrases, reply, "ulaw_8000")
    assert len(await say(live)) > 0 and live.meter.month_used() == len(reply.text)  # a healthy vendor speaks


async def test_segments_cover_canonical_readback_all_speak_languages():
    """With every phrase file reviewed, the plan covers the canonical read-back, its ack and a sold-out offer (an
    items prefix joined before it) in en, te, ta and es, saying every word in order, within the 20k-character
    phrase-cache budget. Today only English is reviewed and spoken, so only English gets audio."""
    files, menu = reviewed_files(), seed_menu()
    templates = Templates(files, defaults={"url": URL})
    langs = templates.speakable(SPEAK_LANGS)
    assert langs == ["en", "te", "ta", "es"]
    specs = prerender.plan_phrases(files, menu, langs, url=URL)
    assert sum(spec.chars for spec in specs) <= 20_000
    assert len({(spec.lang, spec.key) for spec in specs}) == len(specs)
    for lang in langs:
        mine = [spec for spec in specs if spec.lang == lang]
        index = SegmentIndex({spec.match: spec.key for spec in mine})
        said = {spec.key: spec.match for spec in mine}
        for name, reply in canonical_replies(templates, lang, menu).items():
            assert reply.lang == lang and reply.template_key == name, (lang, name)
            pieces = cover(normalize(reply.text), index)
            assert pieces is not None, (lang, name, reply.text)
            assert squash(" ".join(said[p.key] for p in pieces if p.key)) == squash(reply.text), (lang, name)

    real = load_phrase_files()
    spoken = Templates(real).speakable(["en", "te", "ta", "es"])
    assert spoken == ["en"]  # the drafts are unreviewed
    assert {spec.lang for spec in prerender.plan_phrases(real, menu, spoken, url=URL)} == {"en"}
    assert prerender.plan_phrases(real, menu, ["te", "es"], url=URL) == []  # never audio for an unreviewed file


def test_every_spoken_template_stitches():
    """Every template a guest can hear (not the kitchen's notify lines) is covered in every speak language with
    realistic slot values: totals above $100, order numbers digit by digit, dish lists, allergens, spice."""
    files, menu = reviewed_files(), seed_menu()
    templates = Templates(files, defaults={"url": URL})
    specs = prerender.plan_phrases(files, menu, SPEAK_LANGS, url=URL)
    cart = make_cart(CANONICAL, menu=menu)
    for lang in SPEAK_LANGS:
        index = SegmentIndex({spec.match: spec.key for spec in specs if spec.lang == lang})
        for total in (5293, 100, 99, 15207, 200_000):
            values = {"items": Items.from_lines(cart.lines, menu, explicit=total == 99), "total": Money(total),
                      "price": Money(total), "minutes": Count(25), "qty": Count(12), "order_no": OrderNo(1042),
                      "item": Dish.of("CHK_65", menu), "alt1": Dish.of("DAL_TADKA", menu),
                      "alt2": Dish.of("MASALA_DOSA", menu),
                      "candidates": Dishes.of(["CHK_BIRY", "GOAT_BIRY", "VEG_BIRY"], menu),
                      "allergens": AllergenNames(("dairy", "tree_nuts")), "allergen": AllergenNames(("peanuts",)),
                      "spice": Spice(2)}
            for key in sorted(templates.keys):
                if key.startswith(prerender.UNSPOKEN_PREFIXES):
                    continue
                slots = {slot: values[slot] for slot in templates.slots(key) if slot != "url"}  # url: the default
                reply = templates.render(key, lang, slots)
                assert cover(normalize(reply.text), index) is not None, (lang, key, total, reply.text)


def test_cover_fewest_segments_pauses_and_word_boundaries():
    index = SegmentIndex({"one": "k1", "one hundred": "k100", "hundred": "kh", "twelve": "k12", "dollars": "kd",
                          "say yes": "ky", "some": "ks"})
    assert cover(normalize("One hundred twelve dollars. Say yes!"), index) == [
        Piece("k100"), Piece("k12"), Piece("kd"), Piece(None, 300), Piece("ky")]  # fewest joins, no trailing pause
    assert cover(normalize("¿one, (twelve)"), index) == [Piece("k1"), Piece(None, 150), Piece("k12")]
    assert cover(normalize("two hundred"), index) is None  # "two" has no segment
    assert cover("someone", index) is None  # a segment never ends inside a word
    assert cover("…", index) == [] and cover("", index) == []


async def test_stitch_needs_every_segment_and_rereads_the_manifest(tmp_path):
    """None until every segment of the reply is on disk; a pre-render on a running server is picked up without a
    restart; a lost segment file or another language means None, never a partial reply."""
    phrases = PhraseCache(tmp_path / "phrases", min_free_bytes=0)
    stitcher = Stitcher(phrases)
    reply = Reply(text="Two Samosa. Anything else?", text_en="2 Samosa. Anything else?", lang="en",
                  source="template", template_key="ack_added")
    assert await stitcher.render(reply, {}, "en", "ulaw_8000") is None  # nothing rendered yet
    wanted = {"two", "samosa", "anything else"}
    specs = [s for s in prerender.plan_phrases(load_phrase_files(), seed_menu(), ["en"], url=URL) if s.match in wanted]
    assert {s.match for s in specs} == wanted
    await prerender.apply(specs, tts=release_adapter(tmp_path, FakeTTSEngine(ms_per_char=5)), phrases=phrases)
    audio = await stitcher.render(reply, {}, "en", "pcm_16000")
    assert audio == await expected_audio(stitcher, phrases, reply, "pcm_16000")
    assert await stitcher.render(reply.model_copy(update={"text": "Two Samosa, one Idli."}), {}, "en",
                                 "pcm_16000") is None  # no Idli segment
    assert await stitcher.render(reply.model_copy(update={"lang": "te"}), {}, "te", "pcm_16000") is None
    lost = next(s for s in specs if s.match == "samosa")
    for ext in ("pcm", "ulaw", "wav"):
        phrases.path(lost.key, "en", ext).unlink()
    assert await stitcher.render(reply, {}, "en", "ulaw_8000") is None
    with pytest.raises(ValueError):
        await stitcher.render(reply, {}, "en", "mp3")  # type: ignore[arg-type]
    assert stitcher.stats()["misses"] == 4 and stitcher.stats()["renders"] == 1


async def test_tts_429_alerts_and_the_reply_still_stitches(tmp_path, caplog):
    """A vendor 429 on the live path fires P29's tts_429 through the meter's hook (else an ERROR line its log bridge
    reads); other refusals do not; a failing hook never hides the TTS error; the reply is stitched instead."""
    phrases = PhraseCache(tmp_path / "phrases", min_free_bytes=0)
    specs = [s for s in prerender.plan_phrases(load_phrase_files(), seed_menu(), ["en"], url=URL)
             if s.key == "goodbye"]
    await prerender.apply(specs, tts=release_adapter(tmp_path, FakeTTSEngine(ms_per_char=5)), phrases=phrases)
    stitcher = Stitcher(phrases)
    reply = Templates.load(defaults={"url": URL}).render("goodbye", "en")

    live = release_adapter(tmp_path / "live", FakeTTSEngine(fail=TTSUnavailable("rate_limited", retry_after_s=3)))
    alerts: list[tuple[str, dict]] = []
    live.meter.on_alert = lambda what, info: alerts.append((what, info))
    with pytest.raises(TTSUnavailable) as exc:
        await speak(live, reply.text)
    assert exc.value.reason == "rate_limited" and alerts == [("tts_429", {"retry_after_s": 3})]
    assert await stitcher.render(reply, {}, reply.lang, "ulaw_8000") == await phrases.get("goodbye", "en", "ulaw_8000")

    def broken_hook(what, info):
        raise RuntimeError("alert sink down")

    live.meter.on_alert = broken_hook
    with pytest.raises(TTSUnavailable):
        await speak(live, reply.text)
    live.meter.on_alert = None
    with caplog.at_level(logging.ERROR, logger="restaurantos.voice.tts.base"), pytest.raises(TTSUnavailable):
        await speak(live, reply.text)
    lines = [r for r in caplog.records if r.getMessage() == "ALERT tts_429"]
    assert len(lines) == 1 and lines[0].levelno == logging.ERROR and lines[0].alert == "tts_429"

    other = release_adapter(tmp_path / "other", FakeTTSEngine(fail=TTSUnavailable("upstream")))
    other.meter.on_alert = lambda what, info: alerts.append((what, info))
    with pytest.raises(TTSUnavailable):
        await speak(other, reply.text)
    assert alerts == [("tts_429", {"retry_after_s": 3})]
