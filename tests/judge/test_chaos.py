"""P30b chaos drill (CONTRACTS_API #37, CONTRACTS_RUNTIME §10.9, ARCHITECTURE §5 V9): the real ``ChaosPort`` reads
``tenants.flags``, its banner is tenant-scoped, and every consumer falls back while a drill is on.

The app runs the real lifespan on P04's store, P10a's bus and P10b's hub with the registry slice these routes need,
plus P13's order agent and P14's dialogue (``FakeMetaClient`` behind them, so any model call would be recorded). The
GM planner's own chaos check (baseline instead of AI) is P23a's and is tested there.
"""

from __future__ import annotations

import pytest

from restaurantos.api import registry
from restaurantos.contracts.common import CallCtx
from restaurantos.contracts.errors import ChaosDown
from restaurantos.contracts.orders import Cart
from restaurantos.contracts.ws import VoiceDegraded
from restaurantos.judge import rush, scenario_control
from restaurantos.judge.chaos import TenantChaos
from restaurantos.voice.session import VoiceSession, voice_deps
from tests.fakes.container import isolated_registry
from tests.fakes.order_agent import CASES, FakeOrderAgent
from tests.integration.test_cp1a import frames_until, recv, run, serving
from tests.judge.test_rush import sandbox, settings, world
from tests.judge.test_scenario_control import admin_headers, app_container, error, judge_tables, session_headers

OFF = {"meta": False, "asr": False, "tts": False}
UNREADABLE = CASES["te_en_canonical"].utterance  # the fast path cannot read it: only the model could


@pytest.fixture(autouse=True)
def _manual_rush(monkeypatch):
    monkeypatch.setattr(rush, "AUTORUN", False)
    yield
    rush._RUNS.clear()


def chaos_tables() -> dict[str, tuple[registry.Entry, ...]]:
    tables = judge_tables()
    extra = tuple(e for e in registry.entries("PROVIDERS") if e.module in ("agents.order_agent", "orders.dialogue"))
    providers = tuple(sorted((*tables["PROVIDERS"], *extra), key=registry.PROVIDERS.index))
    return tables | {"PROVIDERS": providers}


def ctx(tid: str, session: str) -> CallCtx:
    return CallCtx(tenant_id=tid, session_id=session, trace_id=f"tr_{session}", data_class="judge", channel="kiosk",
                   spend_scope=tid)


def drill(client, headers: dict[str, str], target: str, on: bool, tenant: str | None = None):
    url = "/api/judge/chaos" + (f"?tenant={tenant}" if tenant else "")
    return client.post(url, json={"target": target, "on": on}, headers=headers)


def frames(ws, head: int, banners: int) -> tuple[list[tuple[str, dict]], list[tuple[str | None, str, dict]]]:
    """The persisted events up to ``head`` and the ephemeral frames, read on until ``banners`` ``system.degraded``
    frames arrived (a banner goes out right after its commit, so the last one follows the last event)."""
    events, others = frames_until(ws, head)
    while sum(f.get("type") == "system.degraded" for f in others) < banners:
        others.append(recv(ws))
    return ([(e["type"], e["payload"]) for e in events],
            [(f["tenant_id"], f["type"], f["payload"]) for f in others if f["kind"] == "ephemeral"])


async def said(session: VoiceSession, reply) -> list[bytes]:
    return [chunk async for chunk in session.say(reply)]


def test_chaos_forces_fallbacks(monkeypatch):
    isolated_registry(monkeypatch, **chaos_tables())
    c = app_container()
    with serving(c) as client:
        assert isinstance(c.chaos, TenantChaos)
        tid = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        mine = session_headers(c, tid)
        since = client.get("/api/state", headers=mine).json()["as_of_seq"]
        with client.websocket_connect(f"/ws/events?since={since}", headers=mine) as ws:
            assert recv(ws)["kind"] == "hello"
            for target in ("meta", "asr", "tts"):
                r = drill(client, mine, target, True)
                assert r.status_code == 200
            assert r.json() == {"meta": True, "asr": True, "tts": True}
            assert drill(client, mine, "tts", True).json() == r.json()  # a repeat writes nothing
            head = client.get("/api/state", headers=mine).json()["as_of_seq"]
            persisted, banners = frames(ws, head, 3)
        assert [payload for kind, payload in persisted if kind == "chaos.state"] == [
            {"meta": True, "asr": False, "tts": False}, {"meta": True, "asr": True, "tts": False},
            {"meta": True, "asr": True, "tts": True}]
        assert [(tenant, payload["component"], payload["state"], payload["chaos"])
                for tenant, kind, payload in banners if kind == "system.degraded"] == [
            (tid, "meta", "down", True), (tid, "asr", "down", True), (tid, "tts", "down", True)]
        assert client.get("/api/state", headers=mine).json()["tenant"]["flags"]["chaos"] == r.json()

        # the port raises above MetaClient for this tenant; a call without a tenant is never drilled
        for component in ("meta", "asr", "tts"):
            with pytest.raises(ChaosDown):
                c.chaos.check(tid, component)
            c.chaos.check(None, component)

        # meta: a typed turn the fast path cannot read degrades (item buttons) and the agent refuses before Meta
        dialogue = c.dialogue_factory.create(ctx(tid, "vs_drill"))
        turn = run(client, dialogue.handle_typed, UNREADABLE, turn_id="t1")
        assert turn.path == "degraded" and not turn.cart.lines
        empty = Cart(session_id="vs_drill", cart_id="crt_drill", language="en", channel="kiosk")
        menu = run(client, scenario_control.menu_of, c, tid)
        with pytest.raises(ChaosDown):
            run(client, c.order_agent.parse, UNREADABLE, empty, None, "kiosk", ctx(tid, "vs_drill"), menu=menu)
        assert c.meta.calls == []

        # asr: push-to-talk turns to typing; tts: the reply shows as captions
        notes: list[VoiceDegraded] = []

        async def notify(message) -> None:
            notes.append(message)

        voice = VoiceSession(ctx(tid, "vs_voice"), voice_deps(c, dialogue), notify=notify)
        assert run(client, voice.start_turn, "k1") is False
        assert run(client, said, voice, turn.reply) == []
        assert [(n.component, n.fallback) for n in notes] == [("asr", "typed"), ("tts", "captions")]

        # the drill is over: every component is back, and the banner says so
        for target in ("meta", "asr", "tts"):
            r = drill(client, mine, target, False)
        assert r.json() == OFF and c.chaos.state(tid) == OFF
        c.chaos.check(tid, "meta")
        assert client.get("/api/state", headers=mine).json()["tenant"]["flags"]["chaos"] == OFF


def test_chaos_in_one_sandbox_does_not_degrade_another(monkeypatch):
    isolated_registry(monkeypatch, **chaos_tables())
    agent = FakeOrderAgent()
    c = app_container()
    c.order_agent = agent  # an override: the provider is skipped, and every parse is recorded
    with serving(c) as client:
        a = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        b = run(client, c.store.sandbox.create, pool="judge", caps_level="passcode", scenario="dinner_rush")
        ha, hb = session_headers(c, a), session_headers(c, b)
        since_b = client.get("/api/state", headers=hb).json()["as_of_seq"]
        with client.websocket_connect(f"/ws/events?since={since_b}", headers=hb) as ws_b:
            assert recv(ws_b)["kind"] == "hello"
            for target in ("meta", "asr", "tts"):
                assert drill(client, ha, target, True).status_code == 200
            assert error(drill(client, hb, "meta", True, tenant=a))[:2] == (403, "wrong_tenant")
            assert drill(client, hb, "tts", True).json() == {"meta": False, "asr": False, "tts": True}
            head_b = client.get("/api/state", headers=hb).json()["as_of_seq"]
            persisted, banners = frames(ws_b, head_b, 1)
        # B's socket saw only B's own drill: A's three banners never reached it
        assert persisted == [("chaos.state", {"meta": False, "asr": False, "tts": True})]
        assert [(tenant, payload["component"]) for tenant, kind, payload in banners if kind == "system.degraded"] == [
            (b, "tts")]

        assert c.chaos.state(a) == {"meta": True, "asr": True, "tts": True}
        assert c.chaos.state(b) == {"meta": False, "asr": False, "tts": True}
        for component in ("meta", "asr"):
            with pytest.raises(ChaosDown):
                c.chaos.check(a, component)
            c.chaos.check(b, component)
        assert client.get("/api/state", headers=hb).json()["tenant"]["flags"]["chaos"]["meta"] is False

        # A degrades without asking the model; B's identical turn reaches it
        turn_a = run(client, c.dialogue_factory.create(ctx(a, "vs_a")).handle_typed, UNREADABLE, turn_id="t1")
        turn_b = run(client, c.dialogue_factory.create(ctx(b, "vs_b")).handle_typed, UNREADABLE, turn_id="t1")
        assert turn_a.path == "degraded" and turn_b.path == "typed"
        assert [call.ctx.tenant_id for call in agent.calls] == [b]
        assert [line.item_code for line in turn_b.cart.lines] == ["CHK_BIRY", "CHK_BIRY", "PNR_BM"]


def test_live_drill_is_admin_only(monkeypatch):
    isolated_registry(monkeypatch, **judge_tables())
    c = app_container()
    with serving(c) as client:
        guest = session_headers(c, "live", role="guest")
        assert error(drill(client, guest, "meta", True))[:2] == (403, "forbidden_role")
        assert c.chaos.state("live") == OFF
        r = drill(client, admin_headers(c), "meta", True, tenant="live")
        assert r.status_code == 200 and r.json() == {"meta": True, "asr": False, "tts": False}
        with pytest.raises(ChaosDown):
            c.chaos.check("live", "meta")
        c.chaos.check("demo", "meta")  # still tenant-scoped: demo is untouched


async def test_drill_survives_a_restart_and_ends_with_a_reset():
    """The mirror is reloaded from ``tenants.flags`` at startup (``judge.rush``'s startup task) and refreshed when a
    reset clears the flags, whether the reset came from this package's route or straight from the store."""
    shared = settings()
    async with world(settings=shared) as c:
        tid = await sandbox(c)
        other = await sandbox(c)
        drilled = await c.chaos.set(tid, "asr", True, actor="judge")
        assert drilled.model_dump() == {"meta": False, "asr": True, "tts": False}
        await c.chaos.set(other, "meta", True, actor="judge")
        stopped_at = c.clock.now_real_ms()

    async with world(settings=shared, now_ms=stopped_at + 1000, seed=5) as again:
        assert isinstance(again.chaos, TenantChaos)
        with pytest.raises(ChaosDown):
            again.chaos.check(tid, "asr")
        again.chaos.check(tid, "meta")

        result = await scenario_control.load(again, tid, "dinner_rush")  # REST #35's path refreshes at once
        assert result.loaded == "dinner_rush"
        assert again.chaos.state(tid) == OFF
        again.chaos.check(tid, "asr")

        await again.store.sandbox.reset(other)  # a reset elsewhere (P30a): the judge.rush handler refreshes
        await again.bus.drain()
        assert again.chaos.state(other) == OFF
