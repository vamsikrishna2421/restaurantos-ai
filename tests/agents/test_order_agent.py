"""OrderAgent (P13; ARCHITECTURE §5.3 and §5.7, CONTRACTS §7, CONTRACTS_RUNTIME §10.5 and §10.9).

Replay tests run the real MetaClient in replay mode on ``tests/fixtures/ai_cache/order`` (hand-authored until the lead
re-records them; see the README there). Everything else uses FakeMetaClient or a small inline fake.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from restaurantos.agents import order_agent as oa
from restaurantos.agents import order_prompt
from restaurantos.agents.order_agent import OrderAgent, OrderParseUnavailable, provide, validate_turn
from restaurantos.config import AIMode, Purpose
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.errors import AIUnavailable, ChaosDown
from restaurantos.contracts.menu import Availability, LocalizedName, MenuItem
from restaurantos.contracts.orders import CartOp
from restaurantos.contracts.ports import OrderAgentPort
from restaurantos.domain.cart import apply_ops
from restaurantos.meta import pricing
from restaurantos.meta.client import MetaClient
from restaurantos.meta.errors import BudgetExceeded, CircuitOpen, MetaAPIError, MetaTimeout, PolicyViolation
from restaurantos.meta.spend import SpendGuard
from restaurantos.seed.loader import load_seed
from restaurantos.seed.loader import menu_hash as compute_menu_hash
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.meta import FakeMetaClient
from tests.fakes.order_agent import (
    CASES,
    FIXTURE_ROOT,
    REPLAY_CTX,
    FakeOrderAgent,
    canned,
    make_cart,
)

CTX = CallCtx(tenant_id="sbx_test", session_id="vs_test1", trace_id="tr_test", data_class="judge", channel="kiosk",
              spend_scope="sbx_test", spend_pool="judge")


class DownChaos:
    def __init__(self) -> None:
        self.checks: list[tuple[str | None, str]] = []

    def check(self, tenant_id: str | None, component: str) -> None:
        self.checks.append((tenant_id, component))
        raise ChaosDown(component, tenant_id)  # type: ignore[arg-type]

    def state(self, tenant_id: str) -> dict[str, bool]:
        return {"meta": True, "asr": False, "tts": False}


@pytest.fixture
def menu():
    return load_seed().menu


@pytest.fixture
def settings():
    return make_test_settings()


def fake_meta(settings, *outputs, clock=None):
    return FakeMetaClient({Purpose.ORDER: list(outputs)}, guard=SpendGuard.from_settings(settings), clock=clock)


def as_json(name: str) -> dict:
    return canned(name).model_dump(mode="json")


def lines_of(cart) -> list[tuple[str, int, list[str]]]:
    return sorted((line.item_code, line.qty, line.modifiers) for line in cart.lines)


# ----- replay (real MetaClient on hand-authored fixtures) ------------------------------------------------------------


@pytest.fixture
async def replay_agent():
    settings = make_test_settings(ai_mode=AIMode.REPLAY, ai_cache_dir=FIXTURE_ROOT)
    client = MetaClient(settings, SpendGuard.from_settings(settings))
    yield OrderAgent(client, settings, failover_model=None, timeout_s=30.0)
    await client.aclose()


async def replay(agent: OrderAgent, name: str, menu):
    case = CASES[name]
    cart = make_cart(case.cart_lines, menu=menu, channel=case.channel)
    try:
        turn, record = await agent.parse(case.utterance, cart, case.last_question, case.channel, REPLAY_CTX,
                                         menu=menu)
    except OrderParseUnavailable as exc:
        if exc.reason != "replay_miss":
            raise
        pytest.fail(f"no replay fixture for {name}: the o1 prompt, schema, seed menu or ORDER route changed. "
                    "Regenerate the stand-ins (.venv/bin/python -m tests.fakes.order_agent) or re-record (README).")
    assert record.status == "replay" and record.model == "muse-spark-1.3" and record.purpose == "order"
    return turn, cart


async def test_replay_canonical_te_en_three_ops_closing(replay_agent, menu):
    turn, cart = await replay(replay_agent, "te_en_canonical", menu)
    assert turn.language in ("te-en", "te") and turn.intent == "order" and turn.closing is True
    assert len(turn.ops) == 3 and all(op.op == "add" for op in turn.ops)
    result = apply_ops(cart, turn.ops, menu, Availability())
    assert not result.rejected
    assert lines_of(result.cart) == [("CHK_BIRY", 1, []), ("CHK_BIRY", 1, ["NO_ONION"]), ("PNR_BM", 1, [])]
    assert not turn.injection_suspected and not turn.needs_clarification


async def test_replay_ta_en_and_es(replay_agent, menu):
    ta, ta_cart = await replay(replay_agent, "ta_en", menu)
    assert ta.language in ("ta-en", "ta") and ta.intent == "order" and ta.closing is True
    assert lines_of(apply_ops(ta_cart, ta.ops, menu, Availability()).cart) == [("FILTER_COFFEE", 1, []),
                                                                               ("IDLI", 2, [])]
    es, es_cart = await replay(replay_agent, "es", menu)
    assert es.language in ("es", "es-en") and es.intent == "order" and es.closing is True
    assert lines_of(apply_ops(es_cart, es.ops, menu, Availability()).cart) == [("CHK_65", 1, ["MILD"]),
                                                                               ("MANGO_LASSI", 2, [])]


async def test_injection_flag_cart_unchanged(replay_agent, menu):
    turn, cart = await replay(replay_agent, "injection", menu)
    assert turn.injection_suspected is True and turn.ops == []
    after = apply_ops(cart, turn.ops, menu, Availability()).cart
    assert after == cart and after.total_cents == cart.total_cents == 1818  # one chicken biryani with 7% tax


def test_meta_pricing_estimates_the_order_request(menu):
    request = order_prompt.build_request(menu, [], None, "kiosk", "hello")
    body = {"model": "muse-spark-1.3", "messages": request.messages, "response_format": {
        "type": "json_schema", "json_schema": {"name": "OrderTurn", "schema": request.schema, "strict": True}}}
    est, images = pricing.estimate_body_usd("muse-spark-1.3", body)
    assert est > 0 and images == 0


# ----- the call itself ----------------------------------------------------------------------------------------------


async def test_cache_key_menu_hash(settings, menu):
    meta = fake_meta(settings, as_json("en"), as_json("en"))
    agent = OrderAgent(meta, settings)
    other = CTX.model_copy(update={"tenant_id": "sbx_other", "session_id": "vs_other", "spend_scope": "sbx_other"})
    for ctx in (CTX, other):
        await agent.parse(CASES["en"].utterance, make_cart(menu=menu), None, "kiosk", ctx, menu=menu)
    first, second = (call.kwargs for call in meta.calls_for(Purpose.ORDER, "chat_json"))
    assert first["cache_key"] == second["cache_key"] == f"order-o1-{menu.menu_hash[:12]}"
    assert first["messages"][0] == second["messages"][0]  # one cached prefix for every sandbox and session
    assert first["messages"][-1]["role"] == "user" and "UTTERANCE:" in first["messages"][-1]["content"]
    assert first["extra_body"] == {"safety_identifier": hashlib.sha256(b"vs_test1").hexdigest()[:32]}
    assert second["extra_body"]["safety_identifier"] != first["extra_body"]["safety_identifier"]
    assert (first["strict"], first["max_retries"], first["timeout_s"], first["schema_name"]) == (True, 0, 5.0,
                                                                                                "OrderTurn")
    assert first["model"] == "muse-spark-1.3" and first["schema"] == order_prompt.build_request(
        menu, [], None, "kiosk", "x").schema
    nameless = CTX.model_copy(update={"session_id": None})
    meta.add(Purpose.ORDER, as_json("en"))
    await agent.parse("two samosas", make_cart(menu=menu), None, "kiosk", nameless, menu=menu)
    assert meta.calls[-1].kwargs["extra_body"] is None


async def test_parse_uses_given_menu_and_model(settings, menu):
    names = LocalizedName(en="Pani Puri", te="పానీ పూరీ", te_roman="pani puri", ta="பானி பூரி", ta_roman="pani puri",
                          es="pani puri")
    extra = MenuItem(code="PANI_PURI", category="starter", station_id="STN_FRY", price_cents=799, is_veg=True,
                     spice_level=1, names=names, aliases=["golgappa"], prep_min=5, available=True)
    items = [*menu.items, extra]
    custom = menu.model_copy(update={"items": items, "menu_hash": compute_menu_hash(items, menu.modifiers)})
    output = canned("en").model_copy(update={"ops": [CartOp(op="add", item_code="PANI_PURI", qty=2)]})
    meta = fake_meta(settings, output.model_dump(mode="json"), CircuitOpen("open"))
    agent = OrderAgent(meta, settings)
    turn, record = await agent.parse("rendu pani puri", make_cart(menu=menu), None, "phone", CTX, menu=custom,
                                     model="muse-spark-1.2")
    call = meta.calls[0].kwargs
    assert call["model"] == record.model == "muse-spark-1.2" and agent.is_failover(record)
    assert "PANI_PURI" in call["schema"]["properties"]["ops"]["items"]["properties"]["item_code"]["anyOf"][0]["enum"]
    assert "PANI_PURI | Pani Puri" in call["messages"][0]["content"] and "golgappa" in call["messages"][0]["content"]
    assert call["cache_key"] == order_prompt.cache_key(custom) != order_prompt.cache_key(menu)
    assert [(op.item_code, op.qty) for op in turn.ops] == [("PANI_PURI", 2)]
    with pytest.raises(OrderParseUnavailable) as err:  # a pinned model never fails over
        await agent.parse("rendu pani puri", make_cart(menu=menu), None, "phone", CTX, menu=custom,
                          model="muse-spark-1.2")
    assert err.value.reason == "circuit_open" and len(meta.calls) == 2


async def test_chaos_down_raised_before_parse(settings, menu):
    meta = fake_meta(settings, as_json("en"))
    chaos = DownChaos()
    agent = OrderAgent(meta, settings, chaos=chaos)
    with pytest.raises(ChaosDown) as err:
        await agent.parse("", make_cart(menu=menu), None, "bogus", CTX, menu=menu)  # type: ignore[arg-type]
    assert err.value.component == "meta" and chaos.checks == [("sbx_test", "meta")]
    await agent.warm(CTX)  # a warm-up under chaos is skipped silently
    assert meta.calls == [] and meta.remaining(Purpose.ORDER) == 1


async def test_contributor_policy_violation(settings, menu):
    meta = fake_meta(settings, MetaTimeout("slow"))
    agent = OrderAgent(meta, settings)
    for ctx in (CTX, REPLAY_CTX):  # guest traffic, and even synthetic dev traffic outside environment dev
        with pytest.raises(PolicyViolation):
            await agent.parse("two samosas", make_cart(menu=menu), None, "kiosk", ctx, menu=menu,
                              model="muse-spark-1.3-contributor")
    contributor = OrderAgent(meta, settings, failover_model="muse-spark-1.2-contributor")
    with pytest.raises(PolicyViolation):  # never as a failover either (the primary times out first)
        await contributor.parse("two samosas", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    assert [c.kwargs["model"] for c in meta.calls] == ["muse-spark-1.3"]


async def test_timeout_degrades(settings, menu, monkeypatch):
    meta = fake_meta(settings, MetaTimeout("order request timed out"))
    agent = OrderAgent(meta, settings, failover_model=None)
    with pytest.raises(OrderParseUnavailable) as err:
        await agent.parse(CASES["en"].utterance, make_cart(menu=menu), None, "phone", CTX, menu=menu)
    assert isinstance(err.value, AIUnavailable) and err.value.status == 503
    assert err.value.reason == "timeout" and err.value.details["fallback"] == "fast_path"
    assert meta.calls[0].kwargs["timeout_s"] == 5.0 and meta.calls[0].kwargs["max_retries"] == 0

    class Hung:  # never answers: the hard deadline cancels the call (MetaClient bills it cancelled_billed)
        cancelled = False

        async def chat_json(self, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                Hung.cancelled = True
                raise

    monkeypatch.setattr(oa, "DEADLINE_MARGIN_S", 0.0)
    hung = OrderAgent(Hung(), settings, failover_model=None, timeout_s=0.05)  # type: ignore[arg-type]
    with pytest.raises(OrderParseUnavailable) as err:
        await hung.parse("two samosas", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    assert err.value.reason == "timeout" and Hung.cancelled

    meta = fake_meta(settings, MetaTimeout("order request timed out"), as_json("en"))
    turn, record = await OrderAgent(meta, settings).parse(CASES["en"].utterance, make_cart(menu=menu), None,
                                                          "phone", CTX, menu=menu)
    assert record.model == "muse-spark-1.2" and len(turn.ops) == 3  # Spark 1.2 failover (ARCHITECTURE §5.7)


async def test_failover_on_open_breaker_or_overload_only(settings, menu):
    meta = fake_meta(settings, CircuitOpen("circuit open for chat:muse-spark-1.3"), as_json("en"),
                     MetaAPIError(503, "overloaded", code="service_overloaded"), as_json("es"),
                     BudgetExceeded("daily", 10.0, 10.0), MetaAPIError(400, "bad request"),
                     CircuitOpen("open"), CircuitOpen("open too"))
    agent = OrderAgent(meta, settings)
    for name in ("en", "es"):
        turn, record = await agent.parse(CASES[name].utterance, make_cart(menu=menu), None, "phone", CTX, menu=menu)
        assert record.model == "muse-spark-1.2" and agent.is_failover(record) and turn == canned(name)
    reasons = []
    for _ in range(3):
        with pytest.raises(OrderParseUnavailable) as err:
            await agent.parse("two samosas", make_cart(menu=menu), None, "phone", CTX, menu=menu)
        reasons.append((err.value.reason, err.value.model))
    assert reasons == [("budget", "muse-spark-1.3"), ("api_error", "muse-spark-1.3"),
                       ("circuit_open", "muse-spark-1.2")]
    models = [c.kwargs["model"] for c in meta.calls]
    assert models == ["muse-spark-1.3", "muse-spark-1.2"] * 2 + ["muse-spark-1.3"] * 2 + ["muse-spark-1.3",
                                                                                           "muse-spark-1.2"]


async def test_bad_output_and_mock_mode_degrade(settings, menu):
    meta = fake_meta(settings, {"language": "en"}, '{"not": "json"')
    agent = OrderAgent(meta, settings)
    for _ in range(2):
        with pytest.raises(OrderParseUnavailable) as err:
            await agent.parse("two samosas", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
        assert err.value.reason == "bad_output"
    real = MetaClient(settings, SpendGuard.from_settings(settings))  # mock mode: no network, so degrade
    try:
        with pytest.raises(OrderParseUnavailable) as err:
            await OrderAgent(real, settings).parse("two samosas", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
        assert err.value.reason == "mock_mode"
    finally:
        await real.aclose()
    with pytest.raises(ValueError):
        await agent.parse("   \n ", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)


def test_validator_drops_ill_formed_placeholder_and_unknown_lines(menu):
    cart = make_cart([("CHK_BIRY", 2, ())], menu=menu)
    raw = canned("en").model_dump(mode="json") | {
        "ops": [{"op": "add", "line_ref": None, "item_code": "SAMOSA", "qty": 1, "modifiers": ["NOT_OFFERED"]},
                {"op": "add_modifiers", "line_ref": 1, "item_code": None, "qty": None, "modifiers": ["NOT_OFFERED"]},
                {"op": "add", "line_ref": None, "item_code": "IDLI", "qty": None, "modifiers": []},
                {"op": "set_qty", "line_ref": 7, "item_code": None, "qty": 1, "modifiers": []},
                {"op": "remove", "line_ref": 2, "item_code": None, "qty": None, "modifiers": []},
                {"op": "set_qty", "line_ref": 1, "item_code": None, "qty": 3, "modifiers": []}],
        "clarify_candidates": ["CHK_BIRY", "PIZZA", "CHK_BIRY"],
        "question": {"topic": "allergen", "item_code": "PIZZA", "allergen": "dairy"},
        "unrecognized": "x" * 1000}
    turn = validate_turn(raw, cart, menu)
    assert [(op.op, op.line_ref, op.item_code, op.modifiers) for op in turn.ops] == [
        ("add", None, "SAMOSA", []), ("set_qty", 1, None, [])]
    assert turn.needs_clarification  # L7 and L2 (not in the CART block) became a clarification
    assert turn.clarify_candidates == ["CHK_BIRY"] and turn.question.item_code is None
    assert len(turn.unrecognized) == oa.MAX_TEXT_CHARS
    result = apply_ops(cart, turn.ops, menu, Availability())
    assert not result.rejected and lines_of(result.cart) == [("CHK_BIRY", 3, []), ("SAMOSA", 1, [])]


def test_validator_line_refs_only_from_the_cart_block(menu):
    """CONTRACTS §7: line_ref is an L-number from the CART block. The number a line added in the same turn would get
    depends on merges and refusals the model cannot see, so such a ref is a clarification, never a guess."""
    def op(kind, ref=None, code=None, qty=None):
        return {"op": kind, "line_ref": ref, "item_code": code, "qty": qty, "modifiers": []}

    def check(cart, ops, availability=Availability()):
        turn = validate_turn(canned("en").model_dump(mode="json") | {"ops": ops}, cart, menu)
        return turn, apply_ops(cart, turn.ops, menu, availability)

    # An 86'd dish creates no line, so "L1" would have landed on the samosa.
    gone = Availability(unavailable={"PNR_TIKKA": "sold_out"})
    turn, result = check(make_cart(menu=menu), [op("add", code="PNR_TIKKA", qty=2), op("add", code="SAMOSA", qty=1),
                                                op("set_qty", 1, qty=3)], gone)
    assert turn.needs_clarification and [o.op for o in turn.ops] == ["add", "add"]
    assert lines_of(result.cart) == [("SAMOSA", 1, [])] and [r.reason for r in result.rejected] == ["unavailable"]
    # An add of a dish already in the cart merges into its line, so "L3" names no line at all.
    turn, result = check(make_cart([("SAMOSA", 1, ())], menu=menu), [op("add", code="SAMOSA", qty=1),
                                                                      op("add", code="MANGO_LASSI", qty=1),
                                                                      op("set_qty", 3, qty=4)])
    assert turn.needs_clarification and [o.op for o in turn.ops] == ["add", "add"] and not result.rejected
    assert lines_of(result.cart) == [("MANGO_LASSI", 1, []), ("SAMOSA", 2, [])]
    # Refs to the CART block's lines stay, in any order, and no clarification is asked for.
    turn, result = check(make_cart([("SAMOSA", 1, ()), ("IDLI", 2, ())], menu=menu),
                         [op("remove", 2), op("add", code="WATER", qty=1), op("set_qty", 1, qty=2)])
    assert not turn.needs_clarification and len(turn.ops) == 3
    assert lines_of(result.cart) == [("SAMOSA", 2, []), ("WATER", 1, [])]


async def test_lite_variant_parses_without_reply_fields(menu):
    settings = make_test_settings(order_schema_variant="lite")
    lite = {k: v for k, v in as_json("clarify").items() if k not in ("reply_speech", "reply_en")}
    meta = fake_meta(settings, lite)
    turn, _ = await OrderAgent(meta, settings).parse("one biryani please", make_cart(menu=menu), None, "kiosk", CTX,
                                                     menu=menu)
    schema = meta.calls[0].kwargs["schema"]
    assert "reply_speech" not in schema["properties"] and turn.reply_speech is None
    assert turn.clarify_candidates == ["CHK_BIRY", "GOAT_BIRY", "VEG_BIRY", "EGG_BIRY"]


async def test_warm_once_per_240s_and_never_raises(settings, menu):
    clock = FakeClock()
    meta = fake_meta(settings, as_json("en"), as_json("en"), as_json("en"), MetaAPIError(503, "down"), clock=clock)
    agent = OrderAgent(meta, settings, now_ms=clock.now_real_ms)
    await agent.warm(CTX)
    await agent.warm(CTX)
    clock.advance(seconds=239)
    await agent.warm(CTX)
    assert len(meta.calls) == 1
    first = meta.calls[0].kwargs
    assert first["cache_key"] == f"order-o1-{menu.menu_hash[:12]}" and first["max_retries"] == 0
    assert '"hello"' in first["messages"][-1]["content"] and "CART: (empty)" in first["messages"][-1]["content"]
    clock.advance(seconds=2)
    await agent.warm(CTX)
    assert len(meta.calls) == 2
    await agent.parse(CASES["en"].utterance, make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    clock.advance(seconds=300)
    await agent.warm(CTX)  # 503: logged, never raised
    clock.advance(seconds=300)
    await agent.warm(CTX)  # the script is exhausted: still never raises
    assert len(meta.calls) == 5


async def test_provide_reads_chaos_at_call_time(menu):
    c = make_test_container(install_ids=False, script={Purpose.ORDER: [as_json("en")]})
    provide(c)
    assert isinstance(c.order_agent, OrderAgent) and isinstance(c.order_agent, OrderAgentPort)
    turn, record = await c.order_agent.parse(CASES["en"].utterance, make_cart(menu=menu), None, "kiosk", CTX,
                                             menu=menu)
    assert record.status == "mock" and record.spend_scope == "sbx_test" and len(turn.ops) == 3
    c.chaos = DownChaos()  # judge.chaos (P30b) is provided after agents.order_agent
    with pytest.raises(ChaosDown):
        await c.order_agent.parse(CASES["en"].utterance, make_cart(menu=menu), None, "kiosk", CTX, menu=menu)


def test_registry_provider_wires_order_agent(monkeypatch):
    from fastapi.testclient import TestClient

    from restaurantos.api import registry
    from restaurantos.app import create_app
    from restaurantos.contracts import ids
    from tests.fakes.container import isolated_registry

    (entry,) = [e for e in registry.PROVIDERS if e.module == "agents.order_agent"]
    isolated_registry(monkeypatch, PROVIDERS=(entry,))
    previous = ids.current()
    try:
        c = make_test_container(install_ids=False)
        with TestClient(create_app(container=c)):
            assert isinstance(c.order_agent, OrderAgent) and c.order_agent.meta is c.meta
        assert [m.entry.module for m in registry.import_kind("PROVIDERS").missing] == []
    finally:
        ids.install(previous)


# ----- the fake (P14 depends on it) ---------------------------------------------------------------------------------


async def test_fake_order_agent_canned_scripted_and_chaos(settings, menu):
    agent = FakeOrderAgent(guard=SpendGuard.from_settings(settings))
    assert isinstance(agent, OrderAgentPort)
    turn, record = await agent.parse("NAAKU rendu Chicken Biryani kavali; okati without onions, inka oka paneer "
                                     "butter masala... anthe!", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    assert turn == canned("te_en_canonical") and turn is not CASES["te_en_canonical"].turn
    assert record.status == "mock" and record.cost_usd == 0 and record.model == "muse-spark-1.3"
    agent.add("clarify", MetaTimeout("slow"), lambda call: canned("es"))
    assert (await agent.parse("anything", make_cart(menu=menu), None, "kiosk", CTX, menu=menu))[0].needs_clarification
    with pytest.raises(MetaTimeout):
        await agent.parse("anything", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    assert (await agent.parse("x", make_cart(menu=menu), None, "phone", CTX, menu=menu, model="m"))[1].model == "m"
    with pytest.raises(LookupError):
        await agent.parse("something unscripted", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    assert len(agent.calls) == 5 and agent.calls[0].channel == "kiosk"
    chaos = DownChaos()
    down = FakeOrderAgent(chaos=chaos, default="off_menu")
    with pytest.raises(ChaosDown):
        await down.parse("anything", make_cart(menu=menu), None, "kiosk", CTX, menu=menu)
    assert down.calls == [] and chaos.checks == [("sbx_test", "meta")]
    for case in CASES.values():  # every canned case applies cleanly to the seed menu
        result = apply_ops(make_cart(case.cart_lines, menu=menu), case.turn.ops, menu, Availability())
        assert not result.rejected, case.name
