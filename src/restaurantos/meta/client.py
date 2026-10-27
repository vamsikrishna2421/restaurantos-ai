"""Async client for Meta's Model API: Muse Spark (chat and responses), SAM, the Voice Transcribe file
endpoint, status and files.

Every paid call takes a ``CallCtx`` (CONTRACTS_RUNTIME §10.9) and goes through: contributor guard → mode
(mock never touches the network; live and record need a live tag) → circuit breaker ``{endpoint}:{model}``
→ spend hold on the ctx's scope and pool (worst case) → retries with jitter, each under a fresh hold → a ledger
record with the real usage and cost. Once a request has gone out its hold is settled, never released: timeouts,
cancellations and unreadable usage are billed conservatively (F0 delta 3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from ..config import AIMode, Purpose, Settings, is_contributor_model
from ..contracts.common import CallCtx
from . import errors as e
from . import pricing
from .cache import ResponseCache, request_hash
from .spend import CallRecord, Hold, SpendGuard
from .sse import iter_sse_json

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)
CostFn = Callable[[dict[str, Any]], tuple[float, dict[str, Any]]]
Opts = dict[str, Any] | None

# Streamed and plain calls to one API share a breaker per model, e.g. "chat:muse-spark-1.3".
BREAKER_FAMILY = {"chat": "chat", "chat_stream": "chat", "responses": "responses",
                  "responses_stream": "responses", "asr_file": "asr_file"}


@dataclass
class StreamEvent:
    kind: str  # text | reasoning | function_call | progress | done
    text: str = ""
    data: dict[str, Any] | None = None
    record: CallRecord | None = None


@dataclass
class TranscribeResult:
    transcript: str
    audio_ms: int
    turns: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None


@dataclass
class MetaStatus:
    """``GET /v1/status``: the overall status and the models it reports down."""

    overall: str
    models_down: list[str]
    raw: dict[str, Any]


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _with_extra(body: dict[str, Any], extra_body: Opts) -> dict[str, Any]:
    """Passthrough fields (safety_identifier, prompt_cache_retention, metadata); they never override ours."""
    if extra_body:
        if clash := sorted(set(extra_body) & set(body)):
            raise ValueError(f"extra_body may not override {clash}")
        body.update(extra_body)
    return body


class MetaClient:
    def __init__(self, settings: Settings, guard: SpendGuard, *, transport: httpx.AsyncBaseTransport | None = None,
                 cache: ResponseCache | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.guard = guard
        self.cache = cache or ResponseCache(settings.cache_dir)
        self.breaker = e.CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_s, monotonic)
        self.last_status: MetaStatus | None = None
        self._sleep = sleep
        self._http = httpx.AsyncClient(base_url=settings.meta_base_url, transport=transport, timeout=60.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----- paid calls (per-call options: max_retries, timeout_s, extra_body) --------------
    async def chat_json(
        self, *, purpose: Purpose, messages: list[dict[str, Any]], schema: dict[str, Any] | type[M], ctx: CallCtx,
        schema_name: str = "Output", strict: bool = True, cache_key: str | None = None, model: str | None = None,
        effort: str | None = None, max_retries: int | None = None, timeout_s: float | None = None,
        extra_body: Opts = None,
    ) -> tuple[Any, CallRecord]:
        """Structured output via constrained decoding. Returns (parsed, record); when ``schema`` is a pydantic
        model the result is validated into an instance of it. Live voice turns pass ``max_retries=0``."""
        model_cls = schema if isinstance(schema, type) and issubclass(schema, BaseModel) else None
        json_schema = model_cls.model_json_schema() if model_cls else schema
        body = self._chat_body(purpose, messages, cache_key, model, effort)
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": schema_name, "schema": json_schema, "strict": strict}}
        data, rec = await self._json_call("chat", "/chat/completions", _with_extra(body, extra_body), purpose, ctx,
                                          max_retries, timeout_s)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not content:
            raise e.MetaError(f"empty structured output (finish_reason={choice.get('finish_reason')})")
        parsed = json.loads(content)
        return (model_cls.model_validate(parsed) if model_cls else parsed), rec

    async def chat_text(
        self, *, purpose: Purpose, messages: list[dict[str, Any]], ctx: CallCtx, cache_key: str | None = None,
        model: str | None = None, effort: str | None = None, max_retries: int | None = None,
        timeout_s: float | None = None, extra_body: Opts = None,
    ) -> tuple[str, CallRecord]:
        body = _with_extra(self._chat_body(purpose, messages, cache_key, model, effort), extra_body)
        data, rec = await self._json_call("chat", "/chat/completions", body, purpose, ctx, max_retries, timeout_s)
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content") or "", rec

    async def chat_stream(
        self, *, purpose: Purpose, messages: list[dict[str, Any]], ctx: CallCtx, cache_key: str | None = None,
        model: str | None = None, effort: str | None = None, response_format: dict[str, Any] | None = None,
        timeout_s: float | None = None, extra_body: Opts = None,
    ) -> AsyncIterator[StreamEvent]:
        """Text deltas, then ``done`` with the record. Streams never retry."""
        body = self._chat_body(purpose, messages, cache_key, model, effort)
        body |= {"stream": True, "stream_options": {"include_usage": True}}
        if response_format:
            body["response_format"] = response_format
        stream = self._stream_call("chat_stream", "/chat/completions", _with_extra(body, extra_body), purpose, ctx,
                                   timeout_s)
        async with aclosing(stream):
            async for ev, rec in stream:
                if rec is not None:
                    yield StreamEvent("done", record=rec)
                    return
                for ch in ev.get("choices") or []:
                    if delta := (ch.get("delta") or {}).get("content"):
                        yield StreamEvent("text", text=delta)

    async def responses(
        self, *, purpose: Purpose, input: Any, ctx: CallCtx, instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
        previous_response_id: str | None = None, reasoning_summary: str | None = None, cache_key: str | None = None,
        model: str | None = None, effort: str | None = None, max_retries: int | None = None,
        timeout_s: float | None = None, extra_body: Opts = None,
    ) -> tuple[dict[str, Any], CallRecord]:
        """Responses API (also SAM: ``sam-*`` models are billed per image)."""
        body = self._responses_body(purpose, input, instructions, tools, text_format, previous_response_id,
                                    reasoning_summary, cache_key, model, effort)
        return await self._json_call("responses", "/responses", _with_extra(body, extra_body), purpose, ctx,
                                     max_retries, timeout_s)

    async def responses_stream(
        self, *, purpose: Purpose, input: Any, ctx: CallCtx, instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
        previous_response_id: str | None = None, reasoning_summary: str | None = "auto", cache_key: str | None = None,
        model: str | None = None, effort: str | None = None, timeout_s: float | None = None, extra_body: Opts = None,
    ) -> AsyncIterator[StreamEvent]:
        """Reasoning-summary deltas, output-text deltas, function calls and a ``progress`` event for every other
        SSE event (so idle timers see the model is alive), then ``done`` whose ``data`` is the final response.
        Streams never retry."""
        body = self._responses_body(purpose, input, instructions, tools, text_format, previous_response_id,
                                    reasoning_summary, cache_key, model, effort)
        body["stream"] = True
        final: dict[str, Any] | None = None
        stream = self._stream_call("responses_stream", "/responses", _with_extra(body, extra_body), purpose, ctx,
                                   timeout_s)
        async with aclosing(stream):
            async for ev, rec in stream:
                if rec is not None:
                    yield StreamEvent("done", data=final, record=rec)
                    return
                etype = ev.get("type", "")
                if etype == "response.reasoning_summary_text.delta":
                    yield StreamEvent("reasoning", text=ev.get("delta", ""))
                elif etype == "response.output_text.delta":
                    yield StreamEvent("text", text=ev.get("delta", ""))
                elif etype == "response.output_item.done" and (ev.get("item") or {}).get("type") == "function_call":
                    yield StreamEvent("function_call", data=ev["item"])
                elif etype in ("response.completed", "response.incomplete"):
                    final = ev.get("response")
                else:
                    yield StreamEvent("progress", data={"type": etype})

    async def transcribe_file(
        self, wav: bytes, *, ctx: CallCtx, mode: str = "PUSH_TO_TALK", keywords: list[str] | None = None,
        language_bias: list[str] | None = None, max_retries: int | None = None, timeout_s: float | None = None,
    ) -> tuple[TranscribeResult, CallRecord]:
        route = self.settings.route(Purpose.ASR)
        req: dict[str, Any] = {"model": route.model, "audioEncoding": "WAV", "mode": mode}
        req |= {k: v for k, v in (("keywords", keywords), ("languageBias", language_bias)) if v}
        est = pricing.asr_cost_usd(max(pricing.wav_seconds(wav), 1.0))  # exact for audio we can measure
        files = {"request": (None, json.dumps(req), "application/json"), "audio": ("audio.wav", wav, "audio/wav")}
        timeout = timeout_s or route.timeout_s

        async def send() -> httpx.Response:
            headers = self._auth({"Accept": "application/json"})
            return await self._http.post("/asr/transcribe", files=files, headers=headers, timeout=timeout)

        data, rec = await self._guarded("asr_file", req, Purpose.ASR, route.model, est, send, ctx, extra=wav,
                                        cost_fn=pricing.asr_file_cost, max_retries=max_retries, exact_est=True)
        return TranscribeResult(data.get("transcript", ""), int(data.get("audioDurationMs", 0)),
                                data.get("turns") or [], data.get("sessionId")), rec

    # ----- free endpoints: status, files, breakers ------------------------------------------
    async def status(self) -> dict[str, Any]:
        """``GET /v1/status`` (unauthenticated)."""
        data = (await self._plain("GET", "/status", auth=False)).json()
        return data if isinstance(data, dict) else {"status": str(data)}

    async def refresh_status(self) -> MetaStatus:
        """Read ``/v1/status`` and proactively open the breakers of every model it reports down."""
        data = await self.status()
        down = sorted(e.models_reported_down(data))
        for model in down:
            for family in set(BREAKER_FAMILY.values()):
                self.breaker.force_open(f"{family}:{model}")
        overall = data.get("status")
        if isinstance(overall, dict):
            overall = overall.get("indicator") or overall.get("status") or overall.get("description")
        self.last_status = MetaStatus(str(overall or "unknown"), down, data)
        return self.last_status

    async def list_files(self, *, purpose: str | None = None, limit: int | None = None,
                         order: str | None = None) -> list[dict[str, Any]]:
        """Uploaded files (following ``has_more`` pages); the sweeper deletes the stale ones."""
        params: dict[str, Any] = {k: v for k, v in (("purpose", purpose), ("limit", limit), ("order", order)) if v}
        files: list[dict[str, Any]] = []
        for _ in range(100):
            data = (await self._plain("GET", "/files", params=params)).json()
            page = [f for f in (data.get("data") or []) if isinstance(f, dict)] if isinstance(data, dict) else []
            files.extend(page)
            if not page or not data.get("has_more") or (limit and len(files) >= limit):
                break
            params["after"] = data.get("last_id") or page[-1].get("id")
        return files[:limit] if limit else files

    async def delete_file(self, file_id: str) -> dict[str, Any]:
        if not file_id or "/" in file_id:
            raise ValueError("invalid file id")
        data = (await self._plain("DELETE", f"/files/{quote(file_id, safe='')}")).json()
        return data if isinstance(data, dict) else {}

    def breaker_states(self) -> dict[str, str]:
        return self.breaker.states()

    def force_open(self, endpoint: str, model: str, seconds: float | None = None) -> None:
        """Open one model's breaker now (failover drills; ``/v1/status`` outages)."""
        self.breaker.force_open(f"{BREAKER_FAMILY.get(endpoint, endpoint)}:{model}", seconds)

    # ----- request bodies -----------------------------------------------------------------
    def _chat_body(self, purpose: Purpose, messages: list[dict[str, Any]], cache_key: str | None,
                   model: str | None, effort: str | None) -> dict[str, Any]:
        route = self.settings.route(purpose)
        body: dict[str, Any] = {"model": model or route.model, "messages": messages,
                                "max_completion_tokens": route.max_output_tokens}
        if (eff := effort or route.effort) is not None:
            body["reasoning_effort"] = eff
        if cache_key:
            body["prompt_cache_key"] = cache_key
        return body

    def _responses_body(self, purpose: Purpose, input: Any, instructions: str | None, tools: list | None,
                        text_format: dict | None, previous_response_id: str | None, reasoning_summary: str | None,
                        cache_key: str | None, model: str | None, effort: str | None) -> dict[str, Any]:
        route = self.settings.route(purpose)
        body: dict[str, Any] = {"model": model or route.model, "input": input,
                                "max_output_tokens": route.max_output_tokens}
        reasoning = {k: v for k, v in (("effort", effort or route.effort), ("summary", reasoning_summary)) if v}
        optional = {"reasoning": reasoning, "instructions": instructions, "tools": tools,
                    "prompt_cache_key": cache_key, "text": {"format": text_format} if text_format else None,
                    "previous_response_id": previous_response_id}
        body.update({k: v for k, v in optional.items() if v})
        if not previous_response_id:
            body["store"] = False
        return body

    def _auth(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key()}", **(extra or {})}

    # ----- guarded execution --------------------------------------------------------------
    def _prepare(self, endpoint: str, body: dict[str, Any], purpose: Purpose, model: str, ctx: CallCtx,
                 extra: bytes | None = None) -> tuple[str, dict[str, Any]]:
        """Policy and mode checks shared by every paid call; returns (request hash, CallRecord fields)."""
        if is_contributor_model(model) and not e.contributor_allowed(self.settings, ctx):
            raise e.PolicyViolation(f"{model} only takes synthetic dev traffic with no tenant or session "
                                    f"(purpose={purpose})")
        if self.settings.ai_mode is AIMode.MOCK:
            raise e.MockModeError(f"mock mode: no network for {endpoint}")
        if self.settings.ai_mode is not AIMode.REPLAY:
            e.require_live_tag(self.settings)
        key = request_hash(endpoint, body, extra)
        return key, {"purpose": str(purpose), "model": model, "endpoint": endpoint, "session_id": ctx.session_id,
                     "tenant_id": ctx.tenant_id, "trace_id": ctx.trace_id, "data_class": ctx.data_class,
                     "spend_scope": ctx.spend_scope, "pool": ctx.spend_pool, "live_tag": self.settings.live_tag,
                     "request_hash": key}

    def _replay(self, endpoint: str, key: str) -> dict[str, Any]:
        hit = self.cache.load(endpoint, key)
        if hit is None:
            raise e.ReplayMiss(f"no recording for {endpoint} {key[:12]}")
        return hit

    def _admit(self, endpoint: str, model: str, est_usd: float, ctx: CallCtx) -> tuple[str, bool, Hold]:
        """Breaker first, then the spend hold; a refused hold hands a half-open probe slot back."""
        bkey = f"{BREAKER_FAMILY[endpoint]}:{model}"
        probe = self.breaker.acquire(bkey)
        try:
            hold = self.guard.hold(est_usd, scope=ctx.spend_scope, pool=ctx.spend_pool, scope_cap=ctx.scope_cap_usd)
        except BaseException:
            self.breaker.release(bkey, probe)
            raise
        return bkey, probe, hold

    def _on_api_error(self, exc: e.MetaAPIError, bkey: str, probe: bool) -> None:
        if exc.status == 402:
            self._billing_alert(exc)
        if e.counts_for_breaker(exc):
            self.breaker.failure(bkey, probe)
        else:
            self.breaker.release(bkey, probe)

    def _billing_alert(self, exc: e.MetaAPIError) -> None:
        """HTTP 402: Meta stopped accepting our billing, so stop every paid call until an admin intervenes."""
        self.guard.set_kill_switch(True)
        log.error("ALERT meta_402: Meta returned HTTP 402 (%s); the persistent AI kill switch is on",
                  exc.code or "billing_error", extra={"alert": "meta_402"})

    async def _json_call(self, endpoint: str, path: str, body: dict[str, Any], purpose: Purpose, ctx: CallCtx,
                         max_retries: int | None, timeout_s: float | None) -> tuple[dict[str, Any], CallRecord]:
        model = body["model"]
        est, images = pricing.estimate_body_usd(model, body)
        timeout = timeout_s or self.settings.route(purpose).timeout_s

        async def send() -> httpx.Response:
            return await self._http.post(path, json=body, headers=self._auth(), timeout=timeout)

        return await self._guarded(endpoint, body, purpose, model, est, send, ctx, max_retries=max_retries,
                                   cost_fn=lambda data: pricing.call_cost_usd(model, data.get("usage"), images))

    async def _guarded(self, endpoint: str, body: dict[str, Any], purpose: Purpose, model: str, est_usd: float,
                       send: Callable[[], Awaitable[httpx.Response]], ctx: CallCtx, *, cost_fn: CostFn,
                       extra: bytes | None = None, max_retries: int | None = None,
                       exact_est: bool = False) -> tuple[dict[str, Any], CallRecord]:
        key, base = self._prepare(endpoint, body, purpose, model, ctx, extra)
        if self.settings.ai_mode is AIMode.REPLAY:
            return self._replay(endpoint, key)["response"], self.guard.record(CallRecord(status="replay", **base))
        bkey, probe, hold = self._admit(endpoint, model, est_usd, ctx)
        wire, t0 = e.Wire(), time.monotonic()

        def record(status: str, error: str | None, billed: bool) -> CallRecord:
            cost = est_usd if exact_est else self.guard.conservative_cost(str(purpose), model, est_usd)
            return CallRecord(status=status, error=error, cost_usd=cost if billed else 0.0, latency_ms=_ms(t0),
                              **base)

        def readmit() -> None:  # a retry is a new paid request: hold again, so the kill switch and caps apply
            nonlocal hold
            hold.release()  # a no-op once the lost attempt settled it
            hold = self.guard.hold(est_usd, scope=ctx.spend_scope, pool=ctx.spend_pool, scope_cap=ctx.scope_cap_usd)

        try:  # a lost attempt (it went out, its answer did not come back) is billed on its own hold
            self.settings.api_key()  # no key (or a blank one): refused before anything is on the wire, not billed
            resp = await e.send_with_retries(
                send, what=f"{purpose} request", wire=wire, sleep=self._sleep,
                retries=self.settings.max_retries if max_retries is None else max(0, max_retries),
                max_retry_after_s=self.settings.max_retry_after_s, before_retry=readmit,
                lost=lambda: hold.settle(record("error", "lost_attempt", billed=True)))
            data = resp.json()
        except e.MetaTimeout as exc:
            self.breaker.failure(bkey, probe)
            hold.settle(record("timeout_billed" if exc.sent else "error", "MetaTimeout", billed=exc.sent))
            raise
        except e.MetaAPIError as exc:
            self._on_api_error(exc, bkey, probe)
            gateway = exc.status == 504  # the server worked until its own time limit: bill it like a timeout
            status = "timeout_billed" if gateway else "error"
            hold.settle(record(status, e.error_label(exc), billed=gateway or wire.lost))
            raise
        except ValueError as exc:  # a 200 whose body was not JSON: the work was done, so it is billed
            self.breaker.failure(bkey, probe)
            hold.settle(record("error", "BadJSON", billed=True))
            raise e.MetaError(f"{endpoint}: response was not JSON") from exc
        except BaseException as exc:  # cancelled (barge-in, hang-up, a caller's timeout), a refused retry, or a bug
            self.breaker.release(bkey, probe)
            if wire.in_flight:  # the request is on the wire, so it is billed
                cancelled = isinstance(exc, asyncio.CancelledError)
                hold.settle(record("cancelled_billed" if cancelled else "error", type(exc).__name__, billed=True))
            hold.release()  # nothing was on the wire (e.g. cancelled during a retry back-off)
            raise
        self.breaker.success(bkey, probe)
        cost, fields = pricing.priced(lambda: cost_fn(data), est_usd, endpoint)
        rec = hold.settle(CallRecord(status="ok", cost_usd=cost, latency_ms=_ms(t0), **base, **fields))
        if self.settings.ai_mode is AIMode.RECORD:
            self.cache.save(endpoint, key, {"response": data})
        return data, rec

    async def _stream_call(self, endpoint: str, path: str, body: dict[str, Any], purpose: Purpose, ctx: CallCtx,
                           timeout_s: float | None) -> AsyncIterator[tuple[dict[str, Any], CallRecord | None]]:
        """Yields (event, None) per SSE event, then ({}, record) once at the end."""
        model = body["model"]
        key, base = self._prepare(endpoint, body, purpose, model, ctx)
        if self.settings.ai_mode is AIMode.REPLAY:
            last_t = 0.0
            for item in self._replay(endpoint, key)["events"]:
                if self.settings.replay_paced:
                    await self._sleep(max(0.0, item["t"] - last_t))
                    last_t = item["t"]
                yield item["event"], None
            yield {}, self.guard.record(CallRecord(status="replay", **base))
            return
        est, images = pricing.estimate_body_usd(model, body)
        bkey, probe, hold = self._admit(endpoint, model, est, ctx)
        timeout = timeout_s or self.settings.route(purpose).timeout_s
        t0, ttft, usage, recorded = time.monotonic(), None, None, []
        headers: dict[str, str] | None = None

        def settle(status: str, error: str | None, billed: bool) -> None:
            cost = 0.0
            if billed:  # the real usage when the stream reported it, else the conservative estimate
                cost = (pricing.priced(lambda: pricing.call_cost_usd(model, usage, images), est, endpoint)[0] if usage
                        else self.guard.conservative_cost(str(purpose), model, est))
            hold.settle(CallRecord(status=status, error=error, cost_usd=cost, latency_ms=_ms(t0), ttft_ms=ttft,
                                   **base))

        try:
            headers = self._auth()  # no key (or a blank one): refused before anything is sent, so not billed
            async with self._http.stream("POST", path, json=body, headers=headers, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise e.api_error(resp)
                async for ev in iter_sse_json(resp.aiter_lines()):
                    if ttft is None:  # first event: the model is alive, so a half-open probe has succeeded
                        ttft = _ms(t0)
                        self.breaker.success(bkey, probe)
                        probe = False
                    usage = ev.get("usage") or (ev.get("response") or {}).get("usage") or usage
                    if (err := e.stream_error(ev)) is not None:
                        raise err
                    if self.settings.ai_mode is AIMode.RECORD:
                        recorded.append({"t": round(time.monotonic() - t0, 3), "event": ev})
                    yield ev, None
        except e.MetaAPIError as exc:  # an HTTP error (billed only for 504) or a terminal SSE error (billed)
            self._on_api_error(exc, bkey, probe)
            gateway = exc.status == 504  # no first event in time while the server read the input: a billed timeout
            settle("timeout_billed" if gateway else "error", e.error_label(exc), billed=gateway or ttft is not None)
            raise
        except httpx.TransportError as exc:
            err, sent = e.transport_failure(exc, f"{purpose} stream")
            if e.counts_for_breaker(err):
                self.breaker.failure(bkey, probe)
            else:  # refused locally before sending: not an outage
                self.breaker.release(bkey, probe)
            timed_out = isinstance(err, e.MetaTimeout) and sent
            settle("timeout_billed" if timed_out else "error", type(exc).__name__, billed=sent)
            raise err from None  # never chain the httpx error: its message can quote a header
        except BaseException as exc:  # the consumer stopped early (GeneratorExit), a cancellation, or a bug
            self.breaker.release(bkey, probe)
            if hold.is_open:
                cancelled = isinstance(exc, asyncio.CancelledError | GeneratorExit)
                settle("cancelled_billed" if cancelled else "error", type(exc).__name__, billed=headers is not None)
            raise
        if ttft is None:
            self.breaker.success(bkey, probe)
        cost, fields = pricing.priced(lambda: pricing.call_cost_usd(model, usage, images), est, endpoint)
        rec = hold.settle(CallRecord(status="ok", cost_usd=cost if usage else est, latency_ms=_ms(t0), ttft_ms=ttft,
                                     **base, **fields))
        if self.settings.ai_mode is AIMode.RECORD:
            self.cache.save(endpoint, key, {"events": recorded})
        yield {}, rec

    async def _plain(self, method: str, path: str, *, auth: bool = True,
                     params: dict[str, Any] | None = None) -> httpx.Response:
        """Free requests (status, files): the same mode rules, one attempt, no spend."""
        if self.settings.ai_mode in (AIMode.MOCK, AIMode.REPLAY):
            raise e.MockModeError(f"{self.settings.ai_mode} mode: no network for {path}")
        e.require_live_tag(self.settings)
        try:
            resp = await self._http.request(method, path, params=params, headers=self._auth() if auth else None,
                                            timeout=15.0)
        except httpx.TransportError as exc:
            raise e.transport_failure(exc, f"{method} {path}")[0] from None  # never chain: it can quote a header
        if resp.status_code >= 400:
            err = e.api_error(resp)
            if err.status == 402:
                self._billing_alert(err)
            raise err
        return resp
