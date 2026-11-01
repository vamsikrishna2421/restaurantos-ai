"""Golden canonical replay (P41a; ARCHITECTURE §6.9, ARCHITECTURE_OPS §15): merge-blocking from CP4.

The canonical story runs step by step through the real lifespan and the whole registry, as ``test_cp2`` runs it, from
a fixed seed: the judge's Start (a paused ``dinner_rush`` sandbox), Start rush and its walk-ins at 10×, the judge's
typed Telugu-English kiosk order (P13's parse), paneer in ACT, the Playbook and the streamed AI plan, the QR approval of
the T2 steps on a phone, and the executor's INTENT, EFFECT and VERIFY rows. The run must reproduce the sandbox's
ledger head committed in ``tests/fixtures/golden/head.txt``, which also pins the sha256 of ``config/policy.yaml``
(every row stamps it too).

Determinism: one fake real clock (the tenant clocks and the seeded, clock-aware ``IdGen`` read it) that moves only
when a step moves it; ``rush.AUTORUN`` and the BACKGROUND tasks off; and every step runs **gated**: the bus gets the
events a step committed only once the step is over (the hub still fans them out live), then ``bus.drain()`` runs
their consequences. One ``IdGen`` serves every task, so a handler minting an id while the step still mints its own
(the Dialogue rotating its cart after the order, the kiosk naming the reply's utterance) would swap their random
parts and move every id after them; the gate keeps the step's ids and its handlers' ids in a fixed order. (Ungated
runs give the same head today: the gate makes that order a guarantee instead of a matter of timing.)

Recorded model outputs: the real ``MetaClient`` replays ``tests/fixtures/ai_cache/golden/`` (``GOLDEN_AI``) through
P13's ``OrderAgent`` and P23a's planner, so a prompt, schema, seed or route change that alters a request fails the run
(both calls must replay). The te-en parse is P13's real muse-spark-1.3 recording of ``te_en_canonical`` (the story
sends exactly that request); the GM stream is a hand-authored stand-in (``hand-authored:P41a``: the canonical plan,
picked from the report it is sent). ``--bless`` (lead only; ``tests/integration/conftest.py``) regenerates them
(``write_fixtures``, $0, no network) and then rewrites ``head.txt`` from the replay; it needs a BUILD_LOG entry. A
mismatch names the first ledger row that moved. ``story()`` and ``Story`` also drive the chaos and security suites
(P41a, P41b), on the fakes unless they pass a model client: ``FakeOrderAgent`` and the canonical plan on ``GMFakeMeta``.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from pydantic import SecretStr

from restaurantos.api import registry
from restaurantos.config import REPO_ROOT, AIMode, Purpose
from restaurantos.contracts import ids
from restaurantos.contracts.hashing import canonical, sha256_hex
from restaurantos.judge import rush
from restaurantos.meta.cache import request_hash
from restaurantos.meta.client import MetaClient
from restaurantos.meta.spend import SpendGuard
from restaurantos.security import auth, ratelimit
from restaurantos.store import repo
from tests.fakes.clock import FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.gm import DINNER_RUSH_ACTIONS, GMFakeMeta, canned_plan, plan_events, sse_events
from tests.fakes.order_agent import CASES, FakeOrderAgent
from tests.integration.test_cp1 import wait_for
from tests.integration.test_cp1a import run, serving
from tests.voice.test_ptt_ws import recv as voice_recv
from tests.voice.test_ptt_ws import send

T = TypeVar("T")
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
HEAD_FILE = GOLDEN / "head.txt"
GOLDEN_AI = GOLDEN.parent / "ai_cache" / "golden"  # the golden story's model calls, keyed by request hash
STAND_IN = "hand-authored:P41a"
POLICY_FILE = REPO_ROOT / "config" / "policy.yaml"
ORIGIN = "https://restaurantos.fly.dev"
PASSCODE = "golden-judge-passcode-0123456789"
TE_EN = CASES["te_en_canonical"].utterance
# The plan a good model makes at the canonical moment (DINNER_RUSH_ACTIONS, in that order), found by content among the
# report's candidates: keep Paneer Butter Masala selling and bridge with Patel's last 3 packs.
CHOICE = (("set_item_availability", "PNR_TIKKA"), ("set_item_availability", "PALAK_PNR"),
          ("suggest_alternative", "PNR_BM,PALAK_PNR"), ("suggest_alternative", "PNR_TIKKA"),
          ("create_purchase_order", "SUP_PATEL"), ("notify_staff", "STF_CARLOS"), ("notify_staff", "STF_ARJUN"),
          ("notify_staff", "STF_MARIA"))
BLESS_HELP = ("Lead only: re-bless with `.venv/bin/python -m pytest tests/integration/test_golden_canonical.py "
              "--bless` and add a BUILD_LOG entry saying why the head moved.")
MISSED = ("A model call of the golden story found no fixture in tests/fixtures/ai_cache/golden/ (or fell back): the o1 "
          "or g1 prompt, a schema, the seed or a route changed what the story sends. --bless regenerates the fixtures "
          "($0, no network) before it rewrites the head. " + BLESS_HELP)
MetaFactory = Callable[[Any, SpendGuard, FakeClock], Any]  # (settings, guard, clock) -> MetaPort


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    previous = ids.current()
    ratelimit.reset()
    monkeypatch.setattr(rush, "AUTORUN", False)  # the rush is stepped by hand on the fake clock
    monkeypatch.setattr(registry, "BACKGROUND", ())  # gm.ticker, obs.status and maintenance would interleave
    yield
    rush._RUNS.clear()
    ratelimit.reset()
    ids.install(previous)


def report_in(planner_input: str) -> dict[str, Any]:
    """The situation report inside the planner's input (its one JSON-object line)."""
    return json.loads(next(ln for ln in planner_input.splitlines() if ln.startswith("{")))


def refs_by_code(tenant_id: str) -> dict[str, str]:
    """Pass 17: the seed catalog's egress code -> internal ref for one tenant, drawn exactly as the
    planner's start-of-run assignment draws them (``meta.egress.new_assignments``). The doubles
    below play the model: since pass 17 the report they read carries codes in its targets, so they
    translate the codes back to refs before resolving ``CHOICE`` — a real model never needs to,
    it answers by candidate id alone."""
    from restaurantos.meta.egress import new_assignments
    from restaurantos.seed.loader import load_seed

    seed = load_seed()
    rows = new_assignments(tenant_id, [], {"ingredient": [i.id for i in seed.ingredients],
                                           "item": [i.code for i in seed.menu.items]})
    return {row.code: row.ref for row in rows}


def canonical_actions(report: dict[str, Any], code_map: dict[str, str] | None = None,
                      ) -> list[tuple[str, str | None, str | None, str]]:
    """``CHOICE`` resolved to the report's candidate ids (the PO is the Patel candidate with the fewest packs: the
    note's 3), with the drafts and rationales of ``DINNER_RUSH_ACTIONS``. ``code_map`` (pass 17)
    translates a tokenized report's candidate targets back to the refs ``CHOICE`` is written in."""
    def ref(target: str) -> str:
        if not code_map:
            return target
        return ",".join(code_map.get(part, part) for part in target.split(","))

    best: dict[tuple[str, str], dict[str, Any]] = {}
    for cand in report["candidates"]:
        key = (cand["action_type"], ref(cand["target"]))
        if key not in best or cand["params"].get("packs", 0) < best[key]["params"].get("packs", 0):
            best[key] = cand
    return [(best[key]["id"], *rest) for key, (_, *rest) in zip(CHOICE, DINNER_RUSH_ACTIONS, strict=True)]


def gm_model(call: Any) -> list[Any]:
    """The GM model on ``GMFakeMeta``: the canonical plan, streamed."""
    return plan_events(canned_plan(canonical_actions(report_in(call.kwargs["input"]),
                                                     refs_by_code(call.ctx.tenant_id))))


def canned_meta(settings: Any, guard: SpendGuard, clock: FakeClock) -> GMFakeMeta:
    return GMFakeMeta({Purpose.GM: [gm_model] * 8}, guard=guard, clock=clock)  # every GM stream gets an answer


def replayed(settings: Any, guard: SpendGuard, clock: FakeClock) -> MetaClient:
    """The golden's model client: the real ``MetaClient`` replaying ``GOLDEN_AI`` (a miss raises ``ReplayMiss``)."""
    return MetaClient(make_test_settings(data_dir=settings.data_dir, ai_mode=AIMode.REPLAY, ai_cache_dir=GOLDEN_AI),
                      guard)


class Gate:
    """Holds committed events away from the bus while a step runs; ``release`` (on the app's loop) hands them over in
    ``seq`` order. ``commit_fanout`` looks ``bus.enqueue_committed`` up at every commit, so the hub is not held."""

    def __init__(self, bus: Any) -> None:
        self._enqueue, self._held, self.closed = bus.enqueue_committed, [], False
        bus.enqueue_committed = self._hook

    def _hook(self, events: list[Any]) -> None:
        if self.closed:
            self._held.extend(events)
        else:
            self._enqueue(events)

    def release(self) -> None:
        held, self._held, self.closed = self._held, [], False
        if held:
            self._enqueue(held)


@dataclass
class Turn:
    """One kiosk turn: its ``turn_result`` and the frames that said its reply (``said``)."""

    result: dict[str, Any]
    said: list[Any] = field(default_factory=list)


@dataclass
class Story:
    """The app on fakes, driven one gated step at a time (see the module docstring)."""

    client: Any
    c: Any
    clock: FakeClock
    gate: Gate
    tid: str = ""
    headers: dict[str, str] | None = None
    voice_sessions: list[str] = field(default_factory=list)  # every kiosk session's id, in order

    def step(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """``fn(*args, **kwargs)`` with the bus held, then its events handed over and their consequences drained."""
        self.gate.closed = True
        try:
            return fn(*args, **kwargs)
        finally:
            run(self.client, self.gate.release)
            run(self.client, self.c.bus.drain)

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """``fn(c, tid, *args)`` awaited on the app's loop, as one step."""
        return self.step(run, self.client, fn, self.c, self.tid, *args)

    def http(self, method: str, path: str, *, ok: int | None = 200, auth_: bool = True,
             headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        """One request as one step: the judge's cookie (``auth_``), no cookie, or ``headers``; ``ok`` None: any."""
        headers = headers if headers is not None else (self.headers or {}) if auth_ else {"origin": ORIGIN}
        r = self.step(self.client.request, method, path, headers=headers, **kwargs)
        assert ok is None or r.status_code == ok, f"{method} {path}: {r.status_code} {r.text}"
        return r

    def start(self) -> None:
        """The judge's Start: a paused dinner_rush sandbox and its judge cookie."""
        r = self.http("POST", "/api/session", auth_=False, json={"passcode": PASSCODE})
        self.tid = r.json()["tenant_id"]
        self.headers = {"origin": ORIGIN, "cookie": f"{auth.SESSION_COOKIE}={r.cookies[auth.SESSION_COOKIE]}"}
        self.client.cookies.clear()

    def rush_to(self, phase: str) -> None:
        """Start rush at 10×, then step the runner on the fake clock until it reaches ``phase``."""
        assert self.http("POST", "/api/judge/rush", json={"action": "start", "speed": 10}).json()["running"]
        while (delay := self.call(rush.step)) is not None and self.phase() != phase:
            self.clock.advance(delay)

    def phase(self) -> str:
        return run(self.client, rush_phase, self.c, self.tid)

    def kiosk(self, *messages: dict[str, Any]) -> list[Turn]:
        """One kiosk session as one step: each message's turn, its reply said before the next message goes out, and
        the session parked (its last trace written) before the gate opens."""
        def talk() -> list[Turn]:
            turns: list[Turn] = []
            with self.client.websocket_connect("/ws/voice?lang=auto", headers=self.headers) as ws:
                ready = voice_recv(ws)
                self.voice_sessions.append(ready["session_id"])
                for n, message in enumerate(messages):
                    send(ws, **({"turn_key": f"k{n}"} | message))
                    turns.append(Turn(frames_until(ws, "turn_result")[-1], said(ws)))
            wait_for(lambda: not self.client.app.state.voice_sessions.live)
            return turns
        return self.step(talk)

    def approve_on_phone(self, plan: dict[str, Any]) -> dict[str, Any]:
        """The console's QR link; the phone (no cookie) previews the pending T2 steps and approves them."""
        ticked = sorted(s["idx"] for s in plan["steps"] if s["tier"] == "T2" and s["decision"] == "pending")
        token = self.http("POST", f"/api/gm/plans/{plan['id']}/qr", json={}).json()["url"].rsplit("/", 1)[1]
        preview = self.http("POST", f"/api/approve/{token}/preview", auth_=False, json={"selected_idxs": ticked})
        body = {"decision": "approve", "selected_idxs": ticked, "digest": preview.json()["digest"]}
        return self.http("POST", f"/api/approve/{token}", auth_=False, json=body).json()

    def plans(self) -> dict[str, dict[str, Any]]:
        """The tenant's plans by source (``/api/state``)."""
        return {p["source"]: p for p in self.http("GET", "/api/state").json()["plans"]}


def frames_until(ws: Any, *kinds: str) -> list[Any]:
    """``/ws/voice`` frames (dicts, or bytes of audio) up to and including the first of type ``kinds``."""
    out: list[Any] = []
    while not (out and isinstance(out[-1], dict) and out[-1]["type"] in kinds):
        out.append(voice_recv(ws, 5.0))
    return out


def said(ws: Any) -> list[Any]:
    """The frames that say a reply: ``degraded{tts}`` alone, or ``tts_start``, audio (and a ``degraded`` when the
    vendor drops the stream) up to ``tts_end``."""
    out = frames_until(ws, "tts_start", "degraded")
    return out + frames_until(ws, "tts_end") if out[-1]["type"] == "tts_start" else out


async def rush_phase(c: Any, tid: str) -> str:
    async with c.store.read() as conn:
        return (await repo.tenant(conn, tid)).flags.rush.phase


@contextmanager
def story(*, meta: MetaFactory = canned_meta, settings: dict[str, Any] | None = None,
          **overrides: Any) -> Iterator[Story]:
    """The app on fakes for one story: ``meta(settings, guard, clock)`` is the model client, ``settings`` adds test
    settings, ``overrides`` replace container fields (``order_agent`` defaults to ``FakeOrderAgent``; pass None for
    P13's real agent on ``meta``). The judge passcode is set and the fast path off unless ``settings`` say so."""
    fields = {"public_base_url": ORIGIN, "judge_passcode": PASSCODE, "fast_path_items": False} | (settings or {})
    test_settings = make_test_settings(**fields)
    clock = FakeClock(tz=test_settings.tenant_timezone)
    guard = SpendGuard.from_settings(test_settings, clock=lambda: clock.now_real_ms() / 1000)
    overrides.setdefault("order_agent", FakeOrderAgent(guard=guard))
    c = make_test_container(settings=test_settings, guard=guard, clock=clock, meta=meta(test_settings, guard, clock),
                            store=None, bus=None, hub=None, **overrides)
    with serving(c) as client:
        run(client, c.bus.drain)  # the startup replay of the seed's events
        yield Story(client, c, clock, Gate(c.bus))


@dataclass(frozen=True)
class Head:
    ledger_head: str
    policy_sha256: str
    rows: list[str]  # one summary line per ledger row

    def render(self) -> str:
        return "\n".join([
            "# Golden canonical replay: tests/integration/test_golden_canonical.py (P41a).",
            "# " + BLESS_HELP,
            f"ledger_head: {self.ledger_head}", f"policy_sha256: {self.policy_sha256}", f"rows: {len(self.rows)}",
            "# seq kind idx actor authorizer sha256(payload)[:12] hash[:12]",
            *(f"row {line}" for line in self.rows), ""])

    @classmethod
    def parse(cls, text: str) -> Head:
        fields, rows = {}, []
        for line in text.splitlines():
            if line.startswith("row "):
                rows.append(line[4:])
            elif ": " in line and not line.startswith("#"):
                key, value = line.split(": ", 1)
                fields[key] = value.strip()
        return cls(fields.get("ledger_head", ""), fields.get("policy_sha256", ""), rows)


def summary(row: dict[str, Any]) -> str:
    idx = "-" if row["step_idx"] is None else row["step_idx"]
    return (f"{row['seq']} {row['kind']} {idx} {row['actor']} {row['authorizer'] or '-'} "
            f"{sha256_hex(canonical(row['payload']))[:12]} {row['hash'][:12]}")


def canonical_story(s: Story, status: str) -> tuple[Turn, Turn, dict[str, Any], dict[str, Any]]:
    """The canonical story on ``s``: the read-back and placed turns, the AI plan and the phone's decision. Its two
    model calls must leave spend records of ``status`` ("replay": both found their fixture)."""
    records: list[Any] = []
    s.c.guard.subscribe(records.append)
    s.start()
    s.rush_to("await_order")
    read_back, placed = s.kiosk({"type": "text", "text": TE_EN}, {"type": "confirm", "yes": True})
    calls = [(r.purpose, r.endpoint, r.status) for r in records]  # one GM stream and no Chat call: no fallback
    assert calls == [("order", "chat", status), ("gm", "responses_stream", status)], f"{calls}\n{MISSED}"
    assert s.call(rush.step) == 0 and s.phase() == "post_call"  # the judge's order tipped paneer: no injection
    ai = s.plans()["ai"]
    return read_back, placed, ai, s.approve_on_phone(ai)


def replay() -> tuple[Head, dict[int, dict[str, Any]]]:
    """The canonical story on the recorded model outputs; its ledger head, and the rows a failure message quotes."""
    with story(meta=replayed, order_agent=None) as s:
        read_back, placed, ai, decided = canonical_story(s, "replay")
        verify = s.http("GET", "/api/ledger/verify").json()
        rows = s.http("GET", "/api/ledger").json()
        final = s.http("GET", f"/api/gm/plans/{ai['id']}").json()
    assert (read_back.result["state"], placed.result["placed"]) == ("readback", True)
    assert (final["status"], decided["approval"]["decision"]) == ("done", "approve")
    assert verify["ok"] and verify["rows"] == len(rows) < 500, verify
    policy = sha256_hex(POLICY_FILE.read_bytes())
    assert verify["policy_hash"] == policy  # the rows stamp the policy file's bytes
    return Head(verify["head_hash"], policy, [summary(r) for r in rows]), {r["seq"]: r for r in rows}


def divergence(expected: Head, got: Head, rows: dict[int, dict[str, Any]]) -> str:
    """Where the run left the blessed chain: the first row whose summary differs, and its payload now."""
    lines = [f"ledger head {got.ledger_head} != blessed {expected.ledger_head or '(none)'}"]
    if got.policy_sha256 != expected.policy_sha256:
        lines.append(f"config/policy.yaml changed: sha256 {got.policy_sha256} != blessed {expected.policy_sha256}")
    for n, (old, new) in enumerate(zip(expected.rows, got.rows, strict=False), start=1):
        if old != new:
            payload = json.dumps(rows[n]["payload"], ensure_ascii=False, sort_keys=True)
            lines += [f"first row that moved: seq {n}", f"  blessed: {old}", f"  now:     {new}",
                      f"  payload now: {payload[:600]}"]
            break
    else:
        lines.append(f"rows: {len(got.rows)} now, {len(expected.rows)} blessed (the shorter chain is a prefix)")
    return "\n".join(lines)


def write_fixtures() -> list[Path]:
    """Regenerate ``GOLDEN_AI`` for this tree: the story once more with the real ``MetaClient`` in record mode, its
    network an ``httpx.MockTransport`` answering as ``stand_in`` does (nothing leaves the machine, no key is read, $0),
    so each file name is the request hash a replay computes. A real recording of the same request in P13's or P23a's
    root is copied over the stand-in; files the story no longer asks for are removed."""
    written: list[Path] = []

    def recorder(settings: Any, guard: SpendGuard, clock: FakeClock) -> MetaClient:
        offline = make_test_settings(data_dir=settings.data_dir, ai_mode=AIMode.RECORD, ai_cache_dir=GOLDEN_AI,
                                     live_tag=STAND_IN, model_api_key=SecretStr("hand-authored-not-a-key"))
        # pass 17: the stand-in resolves the canonical plan from a tokenized report, so it needs the
        # story tenant's code map — read lazily from the story's own store (the sandbox exists by
        # the time the GM call arrives; the seeded IdGen makes the tenant the same on every run)
        code_map = lambda: refs_by_code(_story_tenant_id(offline.data_dir))  # noqa: E731
        return MetaClient(offline, guard, transport=httpx.MockTransport(
            lambda request: stand_in(request, written, code_map())))

    with story(meta=recorder, order_agent=None) as s:
        canonical_story(s, "ok")
    for stale in set(GOLDEN_AI.rglob("*.json")) - set(written):
        stale.unlink()
    for path in written:
        for root in ("order", "gm"):
            real = GOLDEN_AI.parent / root / path.relative_to(GOLDEN_AI)
            if real.is_file() and b"hand-authored" not in (data := real.read_bytes()):
                path.write_bytes(data)
    return written


def _story_tenant_id(data_dir: Path) -> str:
    """The sandbox tenant a story's store holds (pass 17: its egress codes key the stand-in's plan)."""
    import sqlite3

    with sqlite3.connect(data_dir / "ros.db") as conn:
        (tid,) = conn.execute("SELECT id FROM tenants WHERE kind = 'sandbox'").fetchone()
    return tid


def stand_in(request: httpx.Request, written: list[Path], code_map: dict[str, str] | None = None) -> httpx.Response:
    """The model, offline: P13's canned te-en parse on Chat Completions; on Responses, the canonical plan for the report
    it is sent, streamed as P23a's stand-in streams it."""
    body = json.loads(request.content)
    endpoint = "responses_stream" if request.url.path.endswith("/responses") else "chat"
    written.append(GOLDEN_AI / endpoint / f"{request_hash(endpoint, body)}.json")
    if endpoint == "responses_stream":
        events = sse_events(canned_plan(canonical_actions(report_in(body["input"]), code_map)))
        events[-1]["response"]["metadata"] = {"fixture": STAND_IN}
        sse = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
    turn = CASES["te_en_canonical"].turn.model_dump(mode="json")
    if "reply_speech" not in body["response_format"]["json_schema"]["schema"]["properties"]:
        turn = {k: v for k, v in turn.items() if k not in ("reply_speech", "reply_en")}  # the lite schema
    return httpx.Response(200, json={
        "id": "chatcmpl-hand-authored-p41a", "object": "chat.completion", "created": 1793113200, "model": body["model"],
        "system_fingerprint": STAND_IN,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(turn, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 160, "total_tokens": 1360}})


def test_golden_canonical_replay(request: pytest.FixtureRequest) -> None:
    bless = request.config.getoption("--bless", default=False)
    if bless:
        write_fixtures()  # the model's answers to what the story sends now
    got, rows = replay()
    if bless:
        HEAD_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEAD_FILE.write_text(got.render(), encoding="utf-8")
        warnings.warn(f"blessed {HEAD_FILE}: ledger_head {got.ledger_head} ({len(got.rows)} rows); add a BUILD_LOG "
                      "entry saying why the head moved", stacklevel=1)
        return
    assert HEAD_FILE.exists(), f"{HEAD_FILE} is missing. {BLESS_HELP}"
    expected = Head.parse(HEAD_FILE.read_text(encoding="utf-8"))
    assert (got.ledger_head, got.policy_sha256) == (expected.ledger_head, expected.policy_sha256), (
        divergence(expected, got, rows) + "\n" + BLESS_HELP)


def test_golden_replay_is_deterministic() -> None:
    """Two runs, two apps, one chain: nothing in the story depends on scheduling or the wall clock."""
    first, _ = replay()
    second, rows = replay()
    assert second == first, divergence(first, second, rows)
