"""P30c recorded runs (CONTRACTS_API §3 #39, MASTER_PLAN §5): captured from persisted events, timed from the first,
phone digits blanked, always watermarked RECORDED; a file that says otherwise is never served.

Capture runs on the FakeStore (many event types, a hand-moved clock) and on P04's real store with the seed
(``demo`` as a rehearsal leaves it); the routes run on the app's real lifespan with P11's real session cookie.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from restaurantos import config
from restaurantos.api import registry
from restaurantos.contracts import ids
from restaurantos.contracts.api import RecordedRun, RecordedRunDetail
from restaurantos.contracts.common import DEMO_TENANT
from restaurantos.contracts.errors import BadRequest, NotFound
from restaurantos.judge import recorded
from restaurantos.security import auth
from restaurantos.security.auth import SESSION_COOKIE, SESSION_TTL_MS, SessionClaims
from restaurantos.seed import materialize
from tests.fakes.clock import FakeClock
from tests.fakes.container import isolated_registry, make_test_container, make_test_settings
from tests.integration.test_cp1a import run, serving

ORIGIN = "https://restaurantos.fly.dev"
SBX_A = "sbx_" + "0" * 25 + "A"
ROUTES = registry.Entry("api.routes_samples", "P30c", "CP3")


@pytest.fixture(autouse=True)
def _fresh_ids():
    previous = ids.current()
    yield
    ids.install(previous)


def session(c: Any, tenant_id: str = SBX_A) -> dict[str, str]:
    claims = SessionClaims(sid="s_recorded_0001", tid=tenant_id, role="judge", lvl="default", pool="public",
                           exp=c.clock.now_real_ms() + SESSION_TTL_MS)
    return {"cookie": f"{SESSION_COOKIE}={auth.encode_session(claims, c.settings)}"}


def write_run(c: Any, run_id: str, body: dict[str, Any]) -> None:
    folder = recorded.runs_dir(c.settings)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{run_id}.json").write_text(json.dumps(body), encoding="utf-8")


async def test_recorded_always_watermarked():
    clock = FakeClock()
    c = make_test_container(clock=clock)
    await c.store.add_tenant(DEMO_TENANT)
    script = [("rush.state", {"running": True, "phase": "pre_call", "speed": 10}, 0),
              ("call.started", {"call_id": "call_1", "caller_last4": "1967", "lane": "public"}, 2_000),
              ("call.bound", {"call_id": "call_1", "caller_last4": "1967"}, 500),
              ("chaos.state", {"meta": True, "asr": False, "tts": False}, 2_500)]
    for event_type, payload, wait_ms in script:
        clock.advance(wait_ms)
        async with c.store.write() as tx:
            await tx.append_event(DEMO_TENANT, event_type, payload, "system")

    run_ = await recorded.capture(c.store, c.settings, tenant_id=DEMO_TENANT, title=" Dinner rush, phone order ",
                                  now_ms=clock.now_real_ms())
    assert run_.watermark == "RECORDED" and run_.title == "Dinner rush, phone order" and run_.id.startswith("rec_")
    assert [(ev.t_ms, ev.event.type) for ev in run_.events] == [
        (0, "rush.state"), (2_000, "call.started"), (2_500, "call.bound"), (5_000, "chaos.state")]
    assert run_.duration_ms == 5_000
    assert all(ev.event.payload.get("caller_last4", "absent") in (None, "absent") for ev in run_.events)
    part = await recorded.capture(c.store, c.settings, tenant_id=DEMO_TENANT, title="Call only",
                                  now_ms=clock.advance(1), after_seq=1, until_seq=3)
    assert [ev.event.seq for ev in part.events] == [2, 3] and part.duration_ms == 500

    write_run(c, "rec_hand", {"id": "rec_hand", "title": "Hand-made", "recorded_at": "2026-10-27T15:00:00.000Z",
                              "duration_ms": 0, "events": [{"t_ms": 0, "event": {
                                  "seq": 1, "tenant_id": DEMO_TENANT, "type": "call.started",
                                  "ts": "2026-10-27T15:00:00.000Z", "biz_time": "2026-10-27T11:00:00.000-04:00",
                                  "actor": "system", "payload": {"call_id": "c", "caller_last4": "4321"}}}]})
    write_run(c, "rec_live", {"id": "rec_live", "title": "Claims to be live", "watermark": "LIVE",
                              "recorded_at": "2026-10-27T15:00:00.000Z", "duration_ms": 0, "events": []})
    write_run(c, "rec_moved", {"id": "rec_other", "title": "Renamed file", "recorded_at": "2026-10-27T15:00:00.000Z",
                               "duration_ms": 0, "events": []})
    (recorded.runs_dir(c.settings) / "rec_junk.json").write_text("{not json", encoding="utf-8")

    runs = await recorded.list_runs(c.settings)
    assert [r.id for r in runs] == [part.id, run_.id, "rec_hand"]  # newest first; refused files left out
    assert {r.watermark for r in runs} == {"RECORDED"} and all(type(r) is RecordedRun for r in runs)
    hand = await recorded.get_run(c.settings, "rec_hand")
    assert hand.watermark == "RECORDED" and hand.events[0].event.payload["caller_last4"] is None
    for refused in ("rec_live", "rec_moved", "rec_junk", "nope", "../ros"):
        with pytest.raises(NotFound):
            await recorded.get_run(c.settings, refused)
    with pytest.raises(BadRequest):
        recorded.build("   ", [ev.event for ev in run_.events], run_id="rec_x", recorded_at_ms=0)
    with pytest.raises(BadRequest):
        await recorded.capture(c.store, c.settings, tenant_id="sbx_empty", title="Nothing", now_ms=0)


async def test_capture_from_a_shell(tmp_path, monkeypatch, capsys):
    """``python -m restaurantos.judge.recorded`` against the database of the current settings (P04's real store)."""
    settings = make_test_settings(data_dir=tmp_path)
    c = make_test_container(settings=settings, real_store=True)
    try:
        await materialize.startup(c)
    finally:
        await c.store.aclose()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    assert await recorded._main(["--tenant", DEMO_TENANT, "--title", "Quiet demo"]) == 0
    run_id = capsys.readouterr().out.split(":")[0]
    saved = await recorded.get_run(settings, run_id)
    assert saved.watermark == "RECORDED" and [ev.event.type for ev in saved.events] == ["scenario.loaded"]
    assert await recorded._main(["--tenant", "sbx_gone", "--title", "Nothing"]) == 1  # no traceback, a message
    assert "at least one event" in capsys.readouterr().err


def test_recorded_routes_serve_runs_with_the_watermark(monkeypatch):
    """The routes on P04's real store: a run captured from ``demo`` after the seed loaded it."""
    isolated_registry(monkeypatch, REQUIRED_ROUTERS=(ROUTES,))
    c = make_test_container(settings=make_test_settings(public_base_url=ORIGIN), real_store=True)
    with serving(c) as client:
        run(client, materialize.startup, c)
        captured = run(client, recorded.capture, c.store, c.settings, tenant_id=DEMO_TENANT, title="Quiet demo",
                       now_ms=c.clock.now_real_ms())
        assert [ev.event.type for ev in captured.events] == ["scenario.loaded"]
        client.cookies.clear()
        r = client.get("/api/judge/recorded", headers=session(c))
        assert r.status_code == 200 and [item["watermark"] for item in r.json()] == ["RECORDED"]
        r = client.get(f"/api/judge/recorded/{captured.id}", headers=session(c))
        detail = RecordedRunDetail.model_validate(r.json())
        assert r.status_code == 200 and detail.watermark == "RECORDED" and detail == captured
        assert client.get("/api/judge/recorded/rec_missing", headers=session(c)).status_code == 404
        assert client.get("/api/judge/recorded").status_code == 401
        r = client.get(f"/api/judge/recorded?tenant={DEMO_TENANT}", headers=session(c))
        assert (r.status_code, r.json()["error"]["code"]) == (403, "wrong_tenant")
