"""A canned ``PlanLLM`` event stream for the GM planner (CONTRACTS_RUNTIME §10.11; P23a), and the dinner_rush world the
planner tests share.

- ``DINNER_RUSH_ACTIONS`` / ``canned_plan()``: what a good plan looks like at the dinner_rush canonical moment (I3,
  Sat 18:48, paneer ACT; candidate ids as ``gm.situation`` numbers them there): 86 Paneer Tikka and Palak Paneer but
  keep Paneer Butter Masala selling, both alternatives, the Patel 3 kg bridge (the supplier note says only 3 packs are
  left), and Carlos, Arjun and Maria notified, with drafts in their languages.
- ``plan_text(plan)``: the JSON the model streams (``actions`` first, as ``GM_PLAN_SCHEMA`` orders it).
- ``plan_events(plan)``: one ``responses_stream`` as ``StreamEvent``s: progress, reasoning-summary deltas, the plan text
  in chunks. ``Pause(s)``, ``Progress(every, count)`` and ``Hook(fn)`` items pace a stream or act in the middle of it.
- ``GMFakeMeta``: ``FakeMetaClient`` whose streams honour those items (real ``asyncio.sleep``s, so planner tests run
  with a scaled ``Timing``) and log whether each stream ``completed`` or was ``closed`` early; a ``Slow(s, then)`` Chat
  output answers ``then`` after ``s`` seconds.
- ``rush_world(c)``: a tenant paused at I3 with an open paneer incident on the fake store, ``c.repo`` serving the
  dinner_rush rows (``tests/gm/test_situation.py``), P24's policy, ledger and approvals, and a judge watching.
- ``write_stand_in()``: a hand-authored replay fixture for ``test_replay_dinner_rush_plan``, recorded through the real
  ``MetaClient`` from a local SSE answer (see ``tests/fixtures/ai_cache/gm/README.txt``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy import insert

from restaurantos.config import Purpose
from restaurantos.contracts.common import CallCtx, TenantClockSpec
from restaurantos.contracts.gm import PlanLLM, PlanLLMAction
from restaurantos.governance import approvals as approvals_mod
from restaurantos.governance import ledger as ledger_mod
from restaurantos.governance import policy as policy_mod
from restaurantos.meta.client import StreamEvent
from restaurantos.services.container import Container
from restaurantos.store import repo as store_repo
from restaurantos.store import tables as t
from tests.fakes.meta import FakeMetaClient
from tests.gm.test_situation import I3, PANEER, SnapshotRepo, rush_snapshot

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ai_cache" / "gm"
TID = "sbx_01JC000000000000000000GM01"
INC = "inc_paneer"
STAND_IN = "hand-authored:P23a"

DINNER_RUSH_ACTIONS: tuple[tuple[str, str | None, str | None, str], ...] = (
    ("C03", None, None, "Paneer Tikka takes 180 g a serving; stopping it keeps the 800 g for the curries."),
    ("C02", None, None, "Palak Paneer competes for the same paneer, so Paneer Butter Masala can keep selling."),
    ("C04", None, None, "Guests asking for the paneer curries get an honest alternative."),
    ("C05", None, None, "Guests asking for Paneer Tikka get an honest alternative."),
    ("C06", None, None, "Patel lands at 19:28, before the stockout; the note says only 3 packs, so 3 kg, not 4."),
    ("C10", "Carlos: queda poco paneer, resérvalo para Paneer Butter Masala.",
     "Carlos: paneer is low, keep it for Paneer Butter Masala.", "Carlos cooks the paneer curries."),
    ("C11", "అర్జున్: పనీర్ తక్కువగా ఉంది, పనీర్ టిక్కా ఆపండి.", "Arjun: paneer is low, stop Paneer Tikka.",
     "Arjun runs the tandoor, where Paneer Tikka stops."),
    ("C12", "Maria: recoge 3 kg de paneer en Patel a las 19:28 cuando se apruebe.",
     "Maria: pick up 3 kg of paneer at Patel at 19:28 once approved.", "Maria does the Patel pickup."),
)
SUMMARY = ("Keep Paneer Butter Masala selling on the 800 g left, stop Paneer Tikka and Palak Paneer, and bridge with "
           "3 kg from Patel, who has only 3 packs.")
REASONING = ("Paneer runs out ≈19:28 at 1.2 kg/h. ", "Patel lands 19:28; the note says 3 packs, so the 3 kg bridge. ",
             "Paneer Butter Masala's forecast fits the stock until then.")


def canned_plan(actions: Iterable[tuple[str, str | None, str | None, str]] = DINNER_RUSH_ACTIONS, *,
                summary: str = SUMMARY) -> PlanLLM:
    return PlanLLM(actions=[PlanLLMAction(candidate_id=cid, message=msg, message_en=msg_en, rationale=why)  # type: ignore[arg-type]
                            for cid, msg, msg_en, why in actions],
                   summary=summary, severity="high", confidence="medium", questions_for_manager=[])


def plan_text(plan: PlanLLM | None = None) -> str:
    return json.dumps((plan or canned_plan()).model_dump(mode="json"), ensure_ascii=False)


@dataclass(frozen=True)
class Pause:
    """Nothing arrives for ``seconds`` (a real sleep)."""

    seconds: float


@dataclass(frozen=True)
class Progress:
    """``count`` progress events (None: forever), one every ``every`` seconds: the model is alive but quiet."""

    every: float
    count: int | None = None


@dataclass(frozen=True)
class Hook:
    """Awaited between two events, inside the consumer's task (a manager acting while the plan streams)."""

    fn: Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class Slow:
    """A Chat output that answers ``then`` after ``seconds``."""

    seconds: float
    then: Any


def plan_events(plan: PlanLLM | None = None, *, reasoning: Sequence[str] = REASONING, chunk: int = 9,
                text: str | None = None) -> list[Any]:
    """A Responses stream of ``plan``: created, in_progress, summary deltas, the text in ``chunk``-sized pieces.
    ``done`` (with the call record) is appended by ``GMFakeMeta``."""
    body = plan_text(plan) if text is None else text
    events: list[Any] = [StreamEvent("progress", data={"type": "response.created"}),
                         StreamEvent("progress", data={"type": "response.in_progress"})]
    events += [StreamEvent("reasoning", text=part) for part in reasoning]
    events.append(StreamEvent("progress", data={"type": "response.output_item.added"}))
    events += [StreamEvent("text", text=body[i:i + chunk]) for i in range(0, len(body), chunk)]
    events.append(StreamEvent("progress", data={"type": "response.output_text.done"}))
    return events


class GMFakeMeta(FakeMetaClient):
    """``FakeMetaClient`` with paced streams; ``stream_log`` gets "completed" or "closed" for every stream it served."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stream_log: list[str] = []

    def _stream(self, method: str, endpoint: str, purpose: Purpose | str, ctx: CallCtx,
                kwargs: dict[str, Any]) -> Any:
        call = self._call(method, purpose, ctx, kwargs)
        raw = self._pop(call)

        async def events() -> Any:
            completed = False
            try:
                record = self._settle(call, endpoint)
                out = self._resolve(call, raw)
                items = [out] if isinstance(out, str | StreamEvent | Pause | Progress | Hook) else list(out)
                for item in items:
                    if isinstance(item, Pause):
                        await asyncio.sleep(item.seconds)
                    elif isinstance(item, Hook):
                        await item.fn()
                    elif isinstance(item, Progress):
                        n = 0
                        while item.count is None or n < item.count:
                            await asyncio.sleep(item.every)
                            n += 1
                            yield StreamEvent("progress", data={"type": "response.in_progress"})
                    else:
                        event = StreamEvent("text", text=item) if isinstance(item, str) else item
                        if event.kind == "done":
                            completed = True
                            yield replace(event, record=event.record or record)
                            return
                        yield event
                completed = True
                yield StreamEvent("done", record=record)
            finally:
                self.stream_log.append("completed" if completed else "closed")

        return events()

    async def chat_json(self, **kwargs: Any) -> Any:  # type: ignore[override]
        queue = self.script.get(Purpose(kwargs["purpose"]))
        delay = 0.0
        if queue and isinstance(queue[0], Slow):
            slow = queue.popleft()
            delay = slow.seconds
            queue.appendleft(slow.then)
        result = await super().chat_json(**kwargs)
        if delay:
            await asyncio.sleep(delay)
        return result


# ----- the dinner_rush world ----------------------------------------------------------------------------------------


class RushRepo(SnapshotRepo):
    """The dinner_rush rows at ``now`` for the situation reads; tenants and plan numbers from the store itself."""

    async def tenant(self, conn: Any, tid: str) -> Any:
        return await store_repo.tenant(conn, tid)

    async def next_no(self, tx: Any, tid: str, kind: Any) -> int:
        return await store_repo.next_no(tx, tid, kind)


async def rush_world(c: Container, *, tid: str = TID, incident_id: str = INC, watched: bool = True,
                     band: str = "act", now_biz_ms: int = I3, provide_planner: bool = True) -> str:
    """A sandbox paused at ``now_biz_ms`` (default I3) with an open paneer incident (epoch 0, no AI runs yet) on
    ``c.store``; ``c.repo`` serves the dinner_rush rows; P24's providers; a judge heartbeat now unless not
    ``watched``. Returns the tenant id."""
    clock = TenantClockSpec(mode="sim", anchor_biz_ms=now_biz_ms, anchor_real_ms=c.clock.now_real_ms(), speed=0)
    await c.store.add_tenant(tid, clock=clock)  # type: ignore[union-attr]
    async with c.store.write() as tx:  # type: ignore[union-attr]
        await tx.conn.execute(insert(t.incidents).values(
            tenant_id=tid, id=incident_id, kind="shortage", ingredient_id=PANEER, band=band, status="open",
            plan_epoch=0, ai_runs=0, opened_biz_ms=now_biz_ms, resolved_biz_ms=None, report=None, report_digest=None,
            decision_digest=None))
    c.repo = RushRepo(rush_snapshot(now_biz_ms))  # type: ignore[assignment]
    policy_mod.provide(c)
    ledger_mod.provide(c)
    approvals_mod.provide(c)
    if provide_planner:
        from restaurantos.gm import planner

        planner.provide(c)
    if watched:
        c.hub.heartbeat(tid, "judge", c.clock.now_real_ms())  # type: ignore[union-attr]
    return tid


# ----- the hand-authored replay fixture -----------------------------------------------------------------------------


def sse_events(plan: PlanLLM | None = None, *, chunk: int = 24) -> list[dict[str, Any]]:
    """``plan_events`` as the Responses SSE payloads ``MetaClient.responses_stream`` parses."""
    text, rid = plan_text(plan), "resp_hand_authored_p23a"
    out: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
                                 {"type": "response.in_progress", "response": {"id": rid, "status": "in_progress"}}]
    out += [{"type": "response.reasoning_summary_text.delta", "delta": part} for part in REASONING]
    out += [{"type": "response.output_text.delta", "delta": text[i:i + chunk]} for i in range(0, len(text), chunk)]
    out.append({"type": "response.completed", "response": {
        "id": rid, "status": "completed", "model": "muse-spark-1.3", "metadata": {"fixture": STAND_IN},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 4100, "input_tokens_details": {"cached_tokens": 512}, "output_tokens": 1900,
                  "output_tokens_details": {"reasoning_tokens": 1400}}}})
    return out


def write_stand_in(root: Path = FIXTURE_ROOT) -> list[Path]:
    """Record ``canned_plan()`` as the replay fixture of ``test_replay_dinner_rush_plan``: the real planner and the
    real ``MetaClient`` in record mode, answered by an ``httpx.MockTransport`` with ``sse_events()``, so the file name
    is exactly the request hash a replay computes. Nothing leaves this machine and no key is read; the response carries
    ``metadata.fixture = "hand-authored:P23a"``. A real recording already at that hash is kept (it was paid for);
    ``scripts/rerecord.py`` replaces a stand-in with a real recording."""
    import tempfile

    import httpx
    from pydantic import SecretStr

    from restaurantos.config import AIMode, Settings
    from restaurantos.meta.cache import request_hash
    from restaurantos.meta.client import MetaClient
    from restaurantos.meta.spend import SpendGuard
    from tests.fakes.container import make_test_container

    written: list[Path] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        written.append(Path(root) / "responses_stream" / f"{request_hash('responses_stream', body)}.json")
        data = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in sse_events())
        return httpx.Response(200, content=(data + "data: [DONE]\n\n").encode(),
                              headers={"content-type": "text/event-stream"})

    async def run(data_dir: Path) -> None:
        settings = Settings(_env_file=None, environment="test", ai_mode=AIMode.RECORD, key_files=False,  # type: ignore[call-arg]
                            data_dir=data_dir, ai_cache_dir=Path(root), live_tag=STAND_IN,
                            model_api_key=SecretStr("hand-authored-not-a-key"))
        guard = SpendGuard.from_settings(settings)
        meta = MetaClient(settings, guard, transport=httpx.MockTransport(answer))
        c = make_test_container(settings=settings, guard=guard, meta=meta)
        try:
            await rush_world(c)
            plan = await c.planner.run(INC, trigger="act")  # type: ignore[union-attr]
            if plan.source != "ai":
                raise RuntimeError(f"the stand-in run ended as {plan.source}, not ai")
        finally:
            await meta.aclose()
            await c.store.aclose()  # type: ignore[union-attr]

    recorded = {path: path.read_bytes() for path in (Path(root) / "responses_stream").glob("*.json")
                if STAND_IN.encode() not in path.read_bytes()}
    with tempfile.TemporaryDirectory(prefix="p23a-fixtures-") as tmp:
        asyncio.run(run(Path(tmp)))
    for path in written:
        if path in recorded:  # the hash did not change: the real recording stays
            path.write_bytes(recorded[path])
    return written


if __name__ == "__main__":  # .venv/bin/python -m tests.fakes.gm
    for path in write_stand_in():
        print(path)
