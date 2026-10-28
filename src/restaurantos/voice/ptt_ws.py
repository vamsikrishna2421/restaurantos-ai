"""``/ws/voice?purpose=order|kitchen|sample&lang=auto|en|te|ta|es&resume=<session_id>`` (CONTRACTS_API §5; P16b): the
kiosk's push-to-talk socket around P16a's ``VoiceSession`` and P14's ``Dialogue``.

Before accept (HTTP denials): the Origin check, a session or admin scoped by ``security.auth``, the query (400), the
cook role for ``purpose=kitchen`` and an open-socket slot. ``resume`` gives the same caller its session back, from the
socket still holding it (a Wi-Fi blip the server has not noticed: that socket closes 4409 ``superseded``, its Dialogue
moves over) or from the per-tenant LRU keeping each closed socket's ``DialogueSession`` for 15 min; else a new
``vs_<ulid>``. One active turn at a time; a voice or text turn takes an ``llm_turn`` unit, push-to-talk and samples a
``voice_turn`` unit, buttons and kitchen text a ``button_turn`` unit; an empty ``cart_ops`` is refused. Audio follows
each ``turn_result`` until ``barge_in`` or the next turn.
``play_sample`` streams a stored clip (CONTRACTS_SEED §15) through the same path at real-time pace. ``purpose=kitchen``
hands finals and typed text to ``kitchen_voice.commands.handle_final`` (P37, imported lazily while enabled) and
answers ``kitchen_result`` (``command:"none"`` without it). Logs never carry what anyone said.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from restaurantos.api import registry
from restaurantos.api.deps import get_container
from restaurantos.audio import pcm16_to_bytes, read_wav, resample
from restaurantos.contracts import ws as wsc
from restaurantos.contracts.common import DEMO_TENANT, CallCtx, is_sandbox
from restaurantos.contracts.errors import AppError, BadRequest, ChaosDown, Forbidden, NotWired, RateLimited
from restaurantos.contracts.events import SessionPurpose
from restaurantos.contracts.ids import CROCKFORD, new_id
from restaurantos.contracts.orders import TurnResult
from restaurantos.meta.errors import MetaError
from restaurantos.obs import voice_hooks
from restaurantos.security import ratelimit
from restaurantos.security.auth import Auth, SessionAuth, require_ws_origin
from restaurantos.services.container import require
from restaurantos.voice.pacer import BYTES_PER_MS, FRAME_BYTES, FRAME_MS, MAX_TURN_MS, SAMPLE_RATE
from restaurantos.voice.session import VoiceSession, voice_deps

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.orders import DialogueSession, Reply
    from restaurantos.contracts.ports import DialoguePort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

RESUME_TTL_MS = 15 * 60_000
RESUME_PER_TENANT = 64
RESUME_TENANTS = 1_024
WARM_EVERY_MS = 240_000
TEXT_FRAME_MAX = 32_768
AUDIO_FRAME_MAX = SAMPLE_RATE * 2  # 1 s of PCM16; the kiosk sends 80 ms frames of 2,560 B
DRAIN_TIMEOUT_S = 10.0  # at close, a released turn may still finish into the Dialogue
CLOSE_TIMEOUT_S = 2.0
CLIP_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")
KitchenHandler = Callable[[str, CallCtx], Awaitable[Any]]


class VoiceQuery(BaseModel):  # validated before accept; ``tenant`` belongs to security.auth
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: SessionPurpose = "order"
    lang: Literal["auto", "en", "te", "ta", "es"] = "auto"
    resume: str | None = Field(default=None, pattern=f"^vs_[{CROCKFORD}]{{26}}$")


def parse_query(params: Any) -> VoiceQuery:
    try:
        return VoiceQuery.model_validate({key: params[key].strip() for key in ("purpose", "lang", "resume")
                                          if (params.get(key) or "").strip()})
    except ValidationError as exc:
        loc = exc.errors()[0].get("loc") or ("query",)
        raise BadRequest("Invalid /ws/voice query", details={"param": str(loc[0])}) from None


def voice_ctx(auth: Auth, tenant_id: str, session_id: str, settings: Settings, lang: str) -> CallCtx:
    """CONTRACTS_RUNTIME §10.9: a sandbox spends from its own scope and cap (by the session's level); ``live`` and
    ``demo`` voice sessions from their call cap; founders (admins, ``demo``) from the admin pool."""
    session = auth.session if auth.session is not None and auth.session.tid == tenant_id else None
    founder, sandbox = auth.is_admin or tenant_id == DEMO_TENANT, is_sandbox(tenant_id)
    passcode = session is not None and session.lvl == "passcode"
    cap = (settings.sandbox_usd_cap_passcode if passcode else settings.sandbox_usd_cap) if sandbox \
        else settings.call_usd_cap
    return CallCtx(tenant_id=tenant_id, session_id=session_id, trace_id=new_id("tr"), channel="kiosk",
                   data_class="founder_voice" if founder else "judge" if sandbox else "customer",
                   lang_hint=None if lang == "auto" else lang,  # type: ignore[arg-type]
                   spend_scope=tenant_id if sandbox else session_id, scope_cap_usd=cap,
                   spend_pool="admin" if founder or session is None else session.pool)


def kitchen_handler(app: Any) -> KitchenHandler | None:
    """P37's ``handle_final`` while its OPTIONAL_MODULES entry is enabled (not ``cut``) and imports; else None."""
    entry = next((e for e in registry.entries("OPTIONAL_MODULES") if e.module == "kitchen_voice.commands"), None)
    features = getattr(app.state, "features", None) or registry.Features()
    if entry is None or not features.enabled(entry, "OPTIONAL_MODULES", app.state.settings.environment):
        return None
    try:
        module = importlib.import_module(entry.path)
    except Exception as exc:
        name = exc.name if isinstance(exc, ModuleNotFoundError) else None
        if not name or not (entry.path == name or entry.path.startswith(f"{name}.")):  # broken, not just absent
            log.exception("kitchen voice: %s failed to import", entry.path)
        return None
    handler = getattr(module, entry.export or "handle_final", None)
    return handler if callable(handler) else None


class SampleRow(BaseModel):  # one row of data/samples/index.json (CONTRACTS_SEED §15; P30c writes it)
    model_config = ConfigDict(extra="ignore")
    id: str
    wav: str


class SampleLibrary:
    """Judge sample clips: ``index.json`` plus the 16 kHz mono WAVs beside it, read per play."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def pcm(self, clip_id: str) -> bytes | None:
        """The clip as PCM16 16 kHz (at most 30 s); None for an unknown or unreadable clip."""
        try:
            return await asyncio.to_thread(self._read, clip_id) if CLIP_ID.fullmatch(clip_id) else None
        except Exception as exc:  # a broken index or WAV is an unknown clip to the client
            log.warning("sample clip unreadable (%s)", type(exc).__name__)
            return None

    def _read(self, clip_id: str) -> bytes | None:
        index = self.root / "index.json"
        rows = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else []
        row = next((SampleRow.model_validate(r) for r in rows if isinstance(r, dict) and r.get("id") == clip_id), None)
        path = (self.root / row.wav).resolve() if row is not None else None
        if path is None or not path.is_relative_to(self.root.resolve()) or not path.is_file():
            return None
        samples, rate = read_wav(path.read_bytes())
        audio = pcm16_to_bytes(resample(samples, rate, SAMPLE_RATE) if rate != SAMPLE_RATE else samples)
        return audio[:MAX_TURN_MS * BYTES_PER_MS] or None


class VoiceSessions:
    """Per app (``app.state.voice_sessions``): the sockets holding a session now, the per-tenant resume LRU of
    ``(owner, DialogueSession, parked_ms)``, the sample clips and the clean-ups still running."""

    def __init__(self, samples: SampleLibrary) -> None:
        self.samples = samples
        self.live: dict[tuple[str, str], VoiceSocket] = {}
        self.cleanups: set[asyncio.Task[Any]] = set()
        self._parked: OrderedDict[str, OrderedDict[str, tuple[str, DialogueSession, int]]] = OrderedDict()

    def park(self, tenant_id: str, session_id: str, owner: str, session: DialogueSession, now_ms: int) -> None:
        sessions = self._parked.pop(tenant_id, None) or OrderedDict()
        sessions.pop(session_id, None)
        sessions[session_id] = (owner, session, now_ms)
        while len(sessions) > RESUME_PER_TENANT:
            sessions.popitem(last=False)
        self._parked[tenant_id] = sessions
        while len(self._parked) > RESUME_TENANTS:
            self._parked.popitem(last=False)

    def take(self, tenant_id: str, session_id: str, owner: str, now_ms: int) -> DialogueSession | None:
        # Only fresh and the caller's own; it leaves the LRU (its new socket parks it again at close).
        sessions = self._parked.get(tenant_id) or OrderedDict()
        parked = sessions.get(session_id)
        if parked is None or parked[0] != owner or now_ms - parked[2] > RESUME_TTL_MS:
            return None
        del sessions[session_id]
        return parked[1]


def sessions_of(app: Any, settings: Settings) -> VoiceSessions:
    if getattr(app.state, "voice_sessions", None) is None:
        app.state.voice_sessions = VoiceSessions(SampleLibrary(settings.data_dir / "samples"))
    return app.state.voice_sessions


class KitchenSession(VoiceSession):
    """``purpose=kitchen``: a final transcript becomes a kitchen command (P37), never a Dialogue turn."""

    def __init__(self, *args: Any, handler: Callable[[], KitchenHandler | None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._handler = handler

    async def on_final(self, text: str, turn_id: str, **_: Any) -> wsc.KitchenResult:  # type: ignore[override]
        return await self.command(text)

    async def command(self, text: str) -> wsc.KitchenResult:
        heard = " ".join(str(text).split())[:wsc.TEXT_TURN_MAX_CHARS]
        try:
            if heard and (handler := self._handler()) is not None:
                out = await handler(heard, self.ctx)
                return wsc.KitchenResult.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
        except Exception as exc:  # a failing or malformed command is heard as no command
            log.warning("voice %s: kitchen command failed (%s)", self.session_id, type(exc).__name__)
        return wsc.KitchenResult(command="none", heard=heard)


class VoiceSocket:
    """One accepted ``/ws/voice`` connection (see the module docstring)."""

    def __init__(self, ws: WebSocket, c: Container, table: VoiceSessions, *, ctx: CallCtx, purpose: SessionPurpose,
                 owner: str, llm_key: str, dialogue: DialoguePort | None,
                 kitchen: Callable[[], KitchenHandler | None]) -> None:
        self.ws, self.c, self.table, self.ctx, self.purpose = ws, c, table, ctx, purpose
        self.owner, self.llm_key, self.dialogue = owner, llm_key, dialogue
        self.key: tuple[str, str] = (str(ctx.tenant_id), str(ctx.session_id))
        if dialogue is None:  # the kitchen: ASR keywords from the menu, no Dialogue
            deps = voice_deps(c, None, menu=getattr(c.dialogue_factory, "menus", None))  # type: ignore[arg-type]
        self.voice = VoiceSession(ctx, voice_deps(c, dialogue, **voice_hooks(c, self.key[0])), notify=self.send) \
            if dialogue is not None else KitchenSession(ctx, deps, notify=self.send, handler=kitchen)
        self.superseded, self.gone, self.close_code, self.close_reason = False, False, 1000, ""
        self._lock, self._stop, self._tasks = asyncio.Lock(), asyncio.Event(), set[asyncio.Task[Any]]()
        self._active: str | None = None  # the turn in progress, from its first message until its result
        self._mic = self._sampling = False  # the active turn takes the client's frames / plays a stored clip
        self._worker: asyncio.Task[Any] | None = None  # the task finishing the active turn
        self._speaking: asyncio.Task[Any] | None = None
        self._gen = self._active_gen = 0  # barge-in and every new turn bump _gen: an older reply stays silent
        self._warm_after_ms = 0

    async def send(self, message: BaseModel) -> None:
        await self._out(message.model_dump_json())

    async def error(self, code: str, message: str) -> None:
        await self.send(wsc.VoiceError(code=code, message=message))

    async def _out(self, frame: str | bytes) -> None:
        if self.gone:
            return
        try:
            async with self._lock:
                await (self.ws.send_bytes(frame) if isinstance(frame, bytes) else self.ws.send_text(frame))
        except Exception:  # the peer is gone: the socket winds down
            self.gone = True
            self._stop.set()

    def supersede(self) -> None:  # the same caller resumed elsewhere: that socket has the Dialogue, this one closes
        self.superseded, self.close_code, self.close_reason = True, 4409, "superseded"
        self._stop.set()

    async def run(self) -> None:
        receiver = asyncio.create_task(self._receive(), name=f"voice:recv:{self.key[1]}")
        stopper = asyncio.create_task(self._stop.wait(), name=f"voice:stop:{self.key[1]}")
        try:
            await asyncio.wait({receiver, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            receiver.cancel()
            stopper.cancel()
            # Shielded: a handler cancelled at shutdown (or by the test client) still parks its session.
            cleanup = asyncio.create_task(self._shutdown(receiver, stopper), name=f"voice:close:{self.key[1]}")
            self.table.cleanups.add(cleanup)
            cleanup.add_done_callback(self.table.cleanups.discard)
            await asyncio.shield(cleanup)

    async def _shutdown(self, receiver: asyncio.Task[Any], stopper: asyncio.Task[Any]) -> None:
        await asyncio.wait({receiver, stopper})
        if not receiver.cancelled() and (exc := receiver.exception()) is not None:
            log.error("voice %s: the socket failed", self.key[1], exc_info=exc)
            self.close_code, self.close_reason = 1011, "internal_error"
        self._mic = False
        await self._hush()
        if (worker := self._worker) is not None and not worker.done():
            if self._sampling:
                worker.cancel()
            await asyncio.wait({worker}, timeout=DRAIN_TIMEOUT_S)  # a released turn still lands in the Dialogue
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks))
        await self.voice.close()
        self.detach()
        if not self.gone:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await self.ws.close(code=self.close_code, reason=self.close_reason)

    def detach(self) -> None:  # leave the live table and park the session, unless another socket took it over
        if self.table.live.get(self.key) is self:
            del self.table.live[self.key]
            if self.dialogue is not None and not self.superseded:
                self.table.park(*self.key, self.owner, self.dialogue.snapshot(), self.c.clock.now_real_ms())

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=f"voice:{name}:{self.key[1]}")
        self._tasks.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:  # e.g. a warm-up: best effort
            log.info("voice %s: %s failed (%s)", self.key[1], task.get_name(), type(exc).__name__)

    async def _receive(self) -> None:
        while (message := await self.ws.receive())["type"] != "websocket.disconnect":
            if message["type"] != "websocket.receive":
                continue
            if (data := message.get("bytes")) is not None:
                if self._mic and len(data) <= AUDIO_FRAME_MAX:  # other audio is late or not ours: dropped
                    await self.voice.feed(data)
            elif (text := message.get("text")) is not None:
                await self._frame(text)
        self.gone = True

    async def _frame(self, text: str) -> None:
        try:
            msg = wsc.VOICE_CLIENT_ADAPTER.validate_json(text) if len(text) <= TEXT_FRAME_MAX else None
        except ValidationError as exc:
            too_many = any(e.get("type") == "too_long" and "ops" in e.get("loc", ()) for e in exc.errors())
            msg = f"At most {wsc.CART_OPS_MAX} cart ops per message" if too_many else "Not a valid /ws/voice message"
        if msg is None or isinstance(msg, str):
            await self.error("bad_request", msg or "That message is too big")
        elif isinstance(msg, wsc.VoiceCartOps) and not msg.ops:  # nothing to do: no turn, no store work
            await self.error("bad_request", "A cart_ops message needs at least one op")
        elif isinstance(msg, wsc.VoiceBargeIn):
            await self._hush()
        elif isinstance(msg, wsc.VoiceWarm):
            self._warm()
        elif isinstance(msg, wsc.VoiceStop):
            self._release(msg.turn_key)
        elif isinstance(msg, wsc.VoiceCancel):
            await self._cancel(msg.turn_key)
        elif self._active is not None:
            await self.error("turn_active", "One turn at a time: wait for this turn's result")
        elif self.purpose == "kitchen" and isinstance(msg, wsc.VoiceCartOps | wsc.VoiceConfirm | wsc.VoicePlaySample):
            await self.error("bad_request", f"{msg.type} is not part of the kitchen microphone")
        else:
            await self._begin(msg)

    async def _begin(self, msg: Any) -> None:
        clip = await self.table.samples.pcm(msg.clip_id) if isinstance(msg, wsc.VoicePlaySample) else None
        if isinstance(msg, wsc.VoicePlaySample) and clip is None:
            return await self.error("not_found", "No such sample clip")
        voice = isinstance(msg, wsc.VoiceStart | wsc.VoicePlaySample)
        if not await self._admit(voice=voice, llm=voice or isinstance(msg, wsc.VoiceText)):
            return
        await self._hush()  # a new turn ends the previous reply's audio
        gen, self._worker, v = self._gen, None, self.voice
        if clip is not None or isinstance(msg, wsc.VoiceStart):
            await self._open(new_id("turn") if clip is not None else msg.turn_key, gen, clip)
        elif isinstance(v, KitchenSession):
            self._work(msg.turn_key, gen, functools.partial(v.command, msg.text))
        elif isinstance(msg, wsc.VoiceText):
            self._work(msg.turn_key, gen, functools.partial(v.on_text, msg.text, msg.turn_key))
        elif isinstance(msg, wsc.VoiceCartOps):
            self._work(msg.turn_key, gen, functools.partial(v.on_cart_ops, list(msg.ops), msg.turn_key))
        else:
            self._work(msg.turn_key, gen, functools.partial(v.on_confirm, msg.yes, msg.turn_key))

    async def _admit(self, *, voice: bool, llm: bool) -> bool:  # ASR's unit first; the kitchen never reaches the model
        try:
            if voice:
                ratelimit.voice_turn(self.owner)
            if llm and self.purpose != "kitchen":
                ratelimit.llm_turn(self.llm_key)
            elif not voice:  # buttons and kitchen text: no model, but each turn still reads and writes the store
                ratelimit.button_turn(self.owner)
            return True
        except RateLimited as exc:
            await self.error("rate_limited", f"Too many turns for now; try again in {exc.retry_after_s or 1} s")
            return False

    async def _open(self, key: str, gen: int, clip: bytes | None) -> None:  # push-to-talk: the client's or a clip
        self._active, self._active_gen, self._mic, self._sampling = key, gen, clip is None, clip is not None
        if not await self.voice.start_turn(key):  # ASR is off for this tenant: the client heard degraded{asr, typed}
            self._active, self._mic, self._sampling = None, False, False
        elif clip is not None:
            self._work(key, gen, functools.partial(self._play, key, clip))

    def _release(self, key: str) -> None:
        if key == self._active and self._mic:  # else stale, or not a turn taking the client's frames
            self._mic = False
            self._work(key, self._active_gen, functools.partial(self.voice.stop_turn, key))

    async def _cancel(self, key: str) -> None:
        if key != self._active or not self.voice.turn_active:  # else its ASR is done and its result is on the way
            return
        self._mic, worker = False, self._worker
        await self.voice.cancel_turn(key)  # the ASR session closes with its cost settled
        if self._sampling and worker is not None:
            worker.cancel()  # stop feeding the clip; its task releases the turn
            await asyncio.wait({worker})
        elif worker is None:  # still held down: no task will release the turn
            self._active = None

    async def _play(self, key: str, pcm: bytes) -> TurnResult | None:  # 80 ms frames on a real-time schedule
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            for n, offset in enumerate(range(0, len(pcm), FRAME_BYTES)):
                if (delay := start + n * FRAME_MS / 1000 - loop.time()) > 0:
                    await asyncio.sleep(delay)
                await self.voice.feed(pcm[offset:offset + FRAME_BYTES])
        except asyncio.CancelledError:
            await self.voice.cancel_turn(key)
            raise
        return await self.voice.stop_turn(key)

    def _work(self, key: str, gen: int, work: Callable[[], Awaitable[Any]]) -> None:
        self._active = key
        self._worker = self._spawn(self._finish(key, gen, work), "turn")

    async def _finish(self, key: str, gen: int, work: Callable[[], Awaitable[Any]]) -> None:
        try:
            result: Any = await work()
        except Exception as exc:
            result = exc
        finally:
            if self._active == key:  # the next turn may begin while this one's result goes out
                self._active, self._sampling = None, False
        if isinstance(result, Exception):
            known = isinstance(result, AppError | ChaosDown | MetaError | NotWired)  # refused, or a dependency is down
            log.warning("voice %s: a turn failed (%s)", self.key[1], type(result).__name__,
                        exc_info=None if known else result)
            await self.error(*((result.code, result.message) if isinstance(result, AppError) else
                               ("ai_unavailable", "The AI is unavailable right now") if known else
                               ("internal_error", "That turn failed; please try again")))
        elif isinstance(result, wsc.KitchenResult):
            await self.send(wsc.VoiceKitchenResult.model_validate(result.model_dump()))
        elif isinstance(result, TurnResult):
            await self.send(wsc.VoiceTurnResult.model_validate(result.model_dump()))
            if gen == self._gen and not self.gone:
                self._speaking = self._spawn(self._speak(result.reply), "speak")

    async def _speak(self, reply: Reply) -> None:
        utterance_id, started = new_id("utt"), False
        try:
            async with contextlib.aclosing(self.voice.say(reply)) as audio:
                async for chunk in audio:
                    if not started:
                        started = True
                        await self.send(wsc.VoiceTtsStart(utterance_id=utterance_id, text=reply.text,
                                                          text_en=reply.text_en))
                    await self._out(chunk)
        finally:
            if started:
                await self.send(wsc.VoiceTtsEnd(utterance_id=utterance_id))

    async def _hush(self) -> None:  # barge-in or a new turn: the reply stops (tts_end), older ones stay silent
        self._gen += 1
        task, self._speaking = self._speaking, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    def _warm(self) -> None:
        agent, now = self.c.order_agent, self.c.clock.now_real_ms()
        if agent is not None and self.purpose != "kitchen" and now >= self._warm_after_ms:
            self._warm_after_ms = now + WARM_EVERY_MS
            self._spawn(agent.warm(self.ctx), "warm")


@router.websocket("/ws/voice", dependencies=[Depends(require_ws_origin)])
async def voice_socket(websocket: WebSocket, auth: SessionAuth) -> None:
    c = get_container(websocket)
    query = parse_query(websocket.query_params)
    if query.purpose == "kitchen" and not auth.can("cook"):
        raise Forbidden("The kitchen microphone needs the cook role", details={"needs": ["cook"]})
    ip, owner, tenant_id = ratelimit.client_ip(websocket, c.settings), ratelimit.session_key_for(auth), auth.tenant_id
    table = sessions_of(websocket.app, c.settings)
    with ratelimit.ws_slot(ratelimit.ws_key(auth, ip)):
        factory = None if query.purpose == "kitchen" else require(c, "dialogue_factory")
        dialogue: DialoguePort | None = None
        session_id, resumed, parked = new_id("vs"), False, None
        if factory is not None and query.resume is not None:
            held = table.live.get((tenant_id, query.resume))
            if held is not None and held.owner == owner and held.dialogue is not None:
                held.supersede()  # a blip the server has not noticed: the live Dialogue moves to this socket
                dialogue, session_id, resumed = held.dialogue, query.resume, True
            elif (parked := table.take(tenant_id, query.resume, owner, c.clock.now_real_ms())) is not None:
                session_id, resumed = query.resume, True
        ctx = voice_ctx(auth, tenant_id, session_id, c.settings, query.lang)
        if factory is not None and dialogue is None:
            dialogue = factory.restore(ctx, parked) if parked is not None else factory.create(ctx)
        sock = VoiceSocket(websocket, c, table, ctx=ctx, purpose=query.purpose, owner=owner,
                           llm_key=ratelimit.llm_key(auth, ip), dialogue=dialogue,
                           kitchen=functools.cache(functools.partial(kitchen_handler, websocket.app)))
        table.live[sock.key] = sock
        try:
            await websocket.accept()
            languages = [] if dialogue is None else getattr(dialogue, "speak_languages", c.settings.speak_languages)
            await sock.send(wsc.VoiceReady(session_id=session_id, speak_languages=list(languages), resumed=resumed))
            if not resumed:
                await sock.voice.begin(query.purpose, query.lang)
        except BaseException:
            sock.detach()
            raise
        await sock.run()
