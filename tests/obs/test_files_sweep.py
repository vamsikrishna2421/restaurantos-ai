"""P29 Meta file sweeper (ARCHITECTURE_OPS §12, bug #61): files older than an hour go, every 15 minutes."""

from __future__ import annotations

from restaurantos.meta.client import MetaClient
from restaurantos.obs.alerts import AlertManager
from restaurantos.obs.files_sweep import SweepResult, created_ms, sweep
from restaurantos.obs.status import StatusMonitor
from tests.fakes.clock import FAKE_NOW_MS, FakeClock
from tests.fakes.container import make_test_container, make_test_settings
from tests.fakes.meta import FakeMetaClient

MIN_MS = 60_000


async def test_meta_file_sweeper():
    now = FAKE_NOW_MS  # 2026-10-27T15:00:00Z
    meta = FakeMetaClient()
    meta.files = [
        {"id": "file-old", "created_at": (now - 120 * MIN_MS) // 1000, "purpose": "user_data"},  # unix seconds
        {"id": "file-new", "created_at": (now - 10 * MIN_MS) // 1000},
        {"id": "file-ms", "created_at": now - 61 * MIN_MS},  # epoch ms
        {"id": "file-iso", "created_at": "2026-10-27T13:00:00Z"},
        {"id": "file-edge", "created_at": (now - 59 * MIN_MS) // 1000},
        {"id": "file-undated"},
        {"id": "file-bad", "created_at": "yesterday"},
    ]
    result = await sweep(meta, now)
    assert result == SweepResult(listed=7, deleted=3, failed=0)
    assert sorted(f["id"] for f in meta.files) == ["file-bad", "file-edge", "file-new", "file-undated"]
    assert [call.kwargs["file_id"] for call in meta.calls if call.method == "delete_file"] == [
        "file-old", "file-ms", "file-iso"]

    async def refuse(file_id: str) -> dict:
        raise RuntimeError("delete refused")

    meta.files = [{"id": "a", "created_at": 1}, {"id": "b", "created_at": 2}]
    meta.delete_file = refuse  # one failure never stops the others, and nothing raises
    assert await sweep(meta, now) == SweepResult(listed=2, deleted=0, failed=2)


async def test_sweeper_skips_mock_mode_and_runs_every_15_minutes():
    settings = make_test_settings()
    client = MetaClient(settings, guard=None)  # type: ignore[arg-type] - the file API never reaches the guard
    try:
        assert await sweep(client, FAKE_NOW_MS) == SweepResult(skipped=True)
    finally:
        await client.aclose()

    clock = FakeClock()
    c = make_test_container(clock=clock)
    c.meta.files = [{"id": "stale", "created_at": (clock.now_real_ms() - 2 * 60 * MIN_MS) // 1000}]
    monitor, alerts = StatusMonitor(), AlertManager()
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert monitor.last_sweep == SweepResult(listed=1, deleted=1) and c.meta.files == []
    c.meta.files = [{"id": "stale2", "created_at": 1}]
    clock.advance(minutes=14)
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert c.meta.files != []  # not due yet
    clock.advance(minutes=1)
    await monitor.tick(c, alerts, clock.now_real_ms())
    assert c.meta.files == []


def test_created_ms_reads_seconds_ms_and_iso():
    assert created_ms({"created_at": 1_793_113_200}) == 1_793_113_200_000
    assert created_ms({"created_at": 1_793_113_200_000}) == 1_793_113_200_000
    assert created_ms({"created_at": "1793113200"}) == 1_793_113_200_000
    assert created_ms({"created_at": "2026-10-27T15:00:00+00:00"}) == 1_793_113_200_000
    assert created_ms({"created_at": "2026-10-27T15:00:00"}) == 1_793_113_200_000  # naive means UTC
    for bad in (None, True, "soon", -5, 0, float("nan"), [1]):
        assert created_ms({"created_at": bad}) is None
