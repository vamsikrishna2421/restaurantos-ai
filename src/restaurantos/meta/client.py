"""Async client for Meta's Model API: Muse Spark (chat + responses), Voice Transcribe file
endpoint and status.

Every call goes through: data policy → spend hold (worst case) → record/replay cache →
retries with jitter + circuit breaker → ledger record with real usage and cost.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from ..config import AIMode, Purpose, Settings, is_contributor_model
from . import pricing
from .cache import ResponseCache, request_hash
from .errors import CircuitOpen, MetaAPIError, MetaError, MetaTimeout, MockModeError, PolicyViolation, ReplayMiss
from .spend import CallRecord, SpendGuard
from .sse import iter_sse_json

M = TypeVar("M", bound=BaseModel)


@dataclass
class StreamEvent:
    kind: str  # text | reasoning | function_call | done
    text: str = ""
    data: dict[str, Any] | None = None
    record: CallRecord | None = None


@dataclass
class TranscribeResult:
    transcript: str
    audio_ms: int
    turns: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None


class CircuitBreaker:
    def __init__(self, failures: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.failures, self.cooldown_s, self._clock = failures, cooldown_s, clock
        self._count: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def check(self, key: str) -> None:
        if self._open_until.get(key, 0.0) > self._clock():
            raise CircuitOpen(f"circuit open for {key}")

    def success(self, key: str) -> None:
        self._count[key] = 0
        self._open_until.pop(key, None)

    def failure(self, key: str) -> None:
        self._count[key] = self._count.get(key, 0) + 1
        if self._count[key] >= self.failures:
            self._open_until[key] = self._clock() + self.cooldown_s
            self._count[key] = 0


def _asr_cost(data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    seconds = (data.get("audioDurationMs") or 0) / 1000
    return pricing.asr_cost_usd(seconds), {"audio_seconds": seconds}


def _counts_for_breaker(exc: MetaError) -> bool:
    """Only service-side trouble opens the circuit; our own 4xx mistakes do not."""
    return not isinstance(exc, MetaAPIError) or exc.retryable


def _api_error(resp: httpx.Response) -> MetaAPIError:
    message, etype, code = resp.reason_phrase or "error", None, None
    try:
        err = resp.json().get("error") or {}
        message, etype, code = err.get("message") or message, err.get("type"), err.get("code")
    except ValueError:
        pass
    retry_after = None
    if (ra := resp.headers.get("retry-after")) is not None:
        try:
            retry_after = float(ra)
        except ValueError:
            retry_after = None
    return MetaAPIError(resp.status_code, message, error_type=etype, code=code, retry_after_s=retry_after)


class MetaClient:
    def __init__(
        self,
        settings: Settings,
        guard: SpendGuard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: ResponseCache | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.guard = guard
        self.cache = cache or ResponseCache(settings.cache_dir)
        self._sleep = sleep
        self._breaker = CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_s)
        self._http = httpx.AsyncClient(base_url=settings.meta_base_url, transport=transport, timeout=60.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----- public API ---------------------------------------------------------------------
    async def chat_json(
        self,
        *,
        purpose: Purpose,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | type[M],
        schema_name: str = "Output",
        cache_key: str | None = None,
        session_id: str | None = None,
        tenant_id: str | None = None,
        synthetic: bool = False,
        model: str | None = None,
        effort: str | None = None,
    ) -> tuple[Any, CallRecord]:
        """Structured output via constrained decoding. Returns (parsed, record); when ``schema``
        is a pydantic model the result is validated into an instance of it."""
        model_cls = schema if isinstance(schema, type) and issubclass(schema, BaseModel) else None
        json_schema = model_cls.model_json_schema() if model_cls else schema
        body = self._chat_body(purpose, messages, cache_key, model, effort)
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": schema_name, "schema": json_schema}}
        data, rec = await self._json_call("chat", "/chat/completions", body, purpose, session_id, tenant_id, synthetic)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content")
        if not content:
            raise MetaError(f"empty structured output (finish_reason={choice.get('finish_reason')})")
        parsed = json.loads(content)
        return (model_cls.model_validate(parsed) if model_cls else parsed), rec

    async def chat_text(
        self, *, purpose: Purpose, messages: list[dict[str, Any]], cache_key: str | None = None,
        session_id: str | None = None, tenant_id: str | None = None, synthetic: bool = False,
        model: str | None = None, effort: str | None = None,
    ) -> tuple[str, CallRecord]:
        body = self._chat_body(purpose, messages, cache_key, model, effort)
        data, rec = await self._json_call("chat", "/chat/completions", body, purpose, session_id, tenant_id, synthetic)
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content") or "", rec

    async def chat_stream(
        self, *, purpose: Purpose, messages: list[dict[str, Any]], cache_key: str | None = None,
        session_id: str | None = None, tenant_id: str | None = None, synthetic: bool = False,
        model: str | None = None, effort: str | None = None, response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self._chat_body(purpose, messages, cache_key, model, effort)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        if response_format:
            body["response_format"] = response_format
        stream = self._stream_call("chat_stream", "/chat/completions", body, purpose, session_id, tenant_id, synthetic)
        async with aclosing(stream):
            async for ev, rec in stream:
                if rec is not None:
                    yield StreamEvent("done", record=rec)
                    return
                for ch in ev.get("choices") or []:
                    if delta := (ch.get("delta") or {}).get("content"):
                        yield StreamEvent("text", text=delta)

    async def responses(
        self, *, purpose: Purpose, input: Any, instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
        previous_response_id: str | None = None, reasoning_summary: str | None = None,
        session_id: str | None = None, tenant_id: str | None = None, synthetic: bool = False,
        model: str | None = None, effort: str | None = None,
    ) -> tuple[dict[str, Any], CallRecord]:
        body = self._responses_body(purpose, input, instructions, tools, text_format, previous_response_id,
                                    reasoning_summary, model, effort)
        return await self._json_call("responses", "/responses", body, purpose, session_id, tenant_id, synthetic)

    async def responses_stream(
        self, *, purpose: Purpose, input: Any, instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
        previous_response_id: str | None = None, reasoning_summary: str | None = "auto",
        session_id: str | None = None, tenant_id: str | None = None, synthetic: bool = False,
        model: str | None = None, effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yields reasoning-summary deltas, output-text deltas, function calls, then ``done``
        whose ``data`` is the final response object."""
        body = self._responses_body(purpose, input, instructions, tools, text_format, previous_response_id,
                                    reasoning_summary, model, effort)
        body["stream"] = True
        final: dict[str, Any] | None = None
        stream = self._stream_call("responses_stream", "/responses", body, purpose, session_id, tenant_id, synthetic)
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
                elif etype in ("error", "response.failed"):
                    err = ev.get("error") or (ev.get("response") or {}).get("error") or {}
                    raise MetaAPIError(500, err.get("message", "stream failed"), code=err.get("code"))

    async def transcribe_file(
        self, wav: bytes, *, mode: str = "PUSH_TO_TALK", keywords: list[str] | None = None,
        language_bias: list[str] | None = None, session_id: str | None = None, tenant_id: str | None = None,
    ) -> tuple[TranscribeResult, CallRecord]:
        route = self.settings.route(Purpose.ASR)
        req: dict[str, Any] = {"model": route.model, "audioEncoding": "WAV", "mode": mode}
        if keywords:
            req["keywords"] = keywords
        if language_bias:
            req["languageBias"] = language_bias
        est_seconds = max(len(wav) / 32_000, 1.0)  # 16 kHz PCM16 worst case
        files = {"request": (None, json.dumps(req), "application/json"), "audio": ("audio.wav", wav, "audio/wav")}

        async def send() -> httpx.Response:
            headers = self._auth({"Accept": "application/json"})
            return await self._http.post("/asr/transcribe", files=files, headers=headers, timeout=route.timeout_s)

        data, rec = await self._guarded(
            "asr_file", req, Purpose.ASR, route.model, pricing.asr_cost_usd(est_seconds), send,
            session_id, tenant_id, synthetic=False, extra=wav,
            cost_fn=_asr_cost,
        )
        return TranscribeResult(data.get("transcript", ""), int(data.get("audioDurationMs", 0)),
                                data.get("turns") or [], data.get("sessionId")), rec

    async def status(self) -> dict[str, Any]:
        resp = await self._http.get("/status", timeout=10)
        resp.raise_for_status()
        return resp.json()

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
                        model: str | None, effort: str | None) -> dict[str, Any]:
        route = self.settings.route(purpose)
        body: dict[str, Any] = {"model": model or route.model, "input": input,
                                "max_output_tokens": route.max_output_tokens}
        reasoning: dict[str, Any] = {}
        if (eff := effort or route.effort) is not None:
            reasoning["effort"] = eff
        if reasoning_summary:
            reasoning["summary"] = reasoning_summary
        if reasoning:
            body["reasoning"] = reasoning
        if instructions:
            body["instructions"] = instructions
        if tools:
            body["tools"] = tools
        if text_format:
            body["text"] = {"format": text_format}
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        else:
            body["store"] = False
        return body

    def _auth(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.settings.api_key()}"}
        if extra:
            headers.update(extra)
        return headers

    # ----- guarded execution --------------------------------------------------------------
    def _check_policy(self, model: str, purpose: Purpose, synthetic: bool) -> None:
        if is_contributor_model(model) and not synthetic:
            raise PolicyViolation(f"{model} may only receive synthetic data (purpose={purpose})")

    def _text_cost(self, model: str) -> Callable[[dict[str, Any]], tuple[float, dict[str, Any]]]:
        def cost(data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
            usage = pricing.normalize_usage(data.get("usage"))
            return pricing.token_cost_usd(model, usage), {
                "input_tokens": usage.input_tokens, "cached_tokens": usage.cached_tokens,
                "output_tokens": usage.output_tokens, "reasoning_tokens": usage.reasoning_tokens}
        return cost

    async def _json_call(self, endpoint: str, path: str, body: dict[str, Any], purpose: Purpose,
                         session_id: str | None, tenant_id: str | None,
                         synthetic: bool) -> tuple[dict[str, Any], CallRecord]:
        model = body["model"]
        est = pricing.estimate_text_call_usd(model, len(json.dumps(body)), int(
            body.get("max_completion_tokens") or body.get("max_output_tokens") or 2000))
        timeout = self.settings.route(purpose).timeout_s

        async def send() -> httpx.Response:
            return await self._http.post(path, json=body, headers=self._auth(), timeout=timeout)

        return await self._guarded(endpoint, body, purpose, model, est, send, session_id, tenant_id,
                                   synthetic, cost_fn=self._text_cost(model))

    async def _guarded(self, endpoint: str, body: dict[str, Any], purpose: Purpose, model: str, est_usd: float,
                       send: Callable[[], Awaitable[httpx.Response]], session_id: str | None, tenant_id: str | None,
                       synthetic: bool, *, cost_fn: Callable[[dict[str, Any]], tuple[float, dict[str, Any]]],
                       extra: bytes | None = None) -> tuple[dict[str, Any], CallRecord]:
        self._check_policy(model, purpose, synthetic)
        key = request_hash(endpoint, body, extra)
        mode = self.settings.ai_mode
        base = {"purpose": str(purpose), "model": model, "endpoint": endpoint, "session_id": session_id,
                "tenant_id": tenant_id, "request_hash": key}
        if mode is AIMode.MOCK:
            raise MockModeError(f"mock mode: no network for {endpoint}")
        if mode is AIMode.REPLAY:
            hit = self.cache.load(endpoint, key)
            if hit is None:
                raise ReplayMiss(f"no recording for {endpoint} {key[:12]}")
            rec = CallRecord(status="replay", latency_ms=0, **base)
            self.guard.record(rec)
            return hit["response"], rec
        self._breaker.check(endpoint)
        hold = self.guard.hold(est_usd, session_id=session_id)
        t0 = time.monotonic()
        try:
            resp = await self._send_with_retries(send, purpose)
            data = resp.json()
        except MetaError as exc:
            if _counts_for_breaker(exc):
                self._breaker.failure(endpoint)
            hold.settle(CallRecord(status="error", error=type(exc).__name__,
                                   latency_ms=int((time.monotonic() - t0) * 1000), **base))
            raise
        except ValueError as exc:
            self._breaker.failure(endpoint)
            hold.settle(CallRecord(status="error", error="BadJSON", cost_usd=est_usd, **base))
            raise MetaError(f"{endpoint}: response was not JSON") from exc
        except BaseException:
            hold.release()
            raise
        self._breaker.success(endpoint)
        cost, fields = cost_fn(data)
        rec = CallRecord(status="ok", cost_usd=cost, latency_ms=int((time.monotonic() - t0) * 1000), **base, **fields)
        hold.settle(rec)
        if mode is AIMode.RECORD:
            self.cache.save(endpoint, key, {"response": data})
        return data, rec

    async def _send_with_retries(self, send: Callable[[], Awaitable[httpx.Response]],
                                 purpose: Purpose) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = await send()
            except httpx.TimeoutException as exc:
                raise MetaTimeout(f"{purpose} request timed out") from exc
            except httpx.TransportError as exc:
                err = MetaAPIError(503, f"transport error: {type(exc).__name__}")
            else:
                if resp.status_code < 400:
                    return resp
                err = _api_error(resp)
            if not err.retryable or attempt >= self.settings.max_retries:
                raise err
            delay = err.retry_after_s if err.retry_after_s is not None else 0.5 * (2 ** attempt)
            if delay > self.settings.max_retry_after_s:
                raise err  # e.g. overload with Retry-After: 60 → degrade now instead of hanging
            await self._sleep(delay + random.uniform(0, 0.25 * (attempt + 1)))  # noqa: S311 - jitter only
            attempt += 1

    async def _stream_call(self, endpoint: str, path: str, body: dict[str, Any], purpose: Purpose,
                           session_id: str | None, tenant_id: str | None,
                           synthetic: bool) -> AsyncIterator[tuple[dict[str, Any], CallRecord | None]]:
        """Yields (event, None) per SSE event, then ({}, record) once at the end."""
        model = body["model"]
        self._check_policy(model, purpose, synthetic)
        key = request_hash(endpoint, body)
        mode = self.settings.ai_mode
        base = {"purpose": str(purpose), "model": model, "endpoint": endpoint, "session_id": session_id,
                "tenant_id": tenant_id, "request_hash": key}
        if mode is AIMode.MOCK:
            raise MockModeError(f"mock mode: no network for {endpoint}")
        if mode is AIMode.REPLAY:
            hit = self.cache.load(endpoint, key)
            if hit is None:
                raise ReplayMiss(f"no recording for {endpoint} {key[:12]}")
            last_t = 0.0
            for item in hit["events"]:
                if self.settings.replay_paced:
                    await self._sleep(max(0.0, item["t"] - last_t))
                    last_t = item["t"]
                yield item["event"], None
            rec = CallRecord(status="replay", **base)
            self.guard.record(rec)
            yield {}, rec
            return
        self._breaker.check(endpoint)
        est = pricing.estimate_text_call_usd(model, len(json.dumps(body)), int(
            body.get("max_completion_tokens") or body.get("max_output_tokens") or 2000))
        hold = self.guard.hold(est, session_id=session_id)
        timeout = self.settings.route(purpose).timeout_s
        t0 = time.monotonic()
        ttft: int | None = None
        usage: dict[str, Any] | None = None
        recorded: list[dict[str, Any]] = []
        completed = False
        try:
            async with self._http.stream("POST", path, json=body, headers=self._auth(), timeout=timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _api_error(resp)
                async for ev in iter_sse_json(resp.aiter_lines()):
                    now = time.monotonic() - t0
                    if ttft is None:
                        ttft = int(now * 1000)
                    usage = ev.get("usage") or (ev.get("response") or {}).get("usage") or usage
                    if mode is AIMode.RECORD:
                        recorded.append({"t": round(now, 3), "event": ev})
                    yield ev, None
            completed = True
        except httpx.TimeoutException as exc:
            self._breaker.failure(endpoint)
            hold.settle(CallRecord(status="error", error="MetaTimeout", cost_usd=est, **base))
            raise MetaTimeout(f"{purpose} stream timed out") from exc
        except MetaError as exc:
            if _counts_for_breaker(exc):
                self._breaker.failure(endpoint)
            hold.settle(CallRecord(status="error", error=type(exc).__name__, **base))
            raise
        finally:
            if not completed and hold.is_open:
                # Consumer stopped early: bill the worst case so the guard never under-counts.
                hold.settle(CallRecord(status="aborted", cost_usd=est, **base))
        self._breaker.success(endpoint)
        u = pricing.normalize_usage(usage)
        rec = CallRecord(status="ok", cost_usd=pricing.token_cost_usd(model, u) if usage else est,
                         latency_ms=int((time.monotonic() - t0) * 1000), ttft_ms=ttft,
                         input_tokens=u.input_tokens, cached_tokens=u.cached_tokens, output_tokens=u.output_tokens,
                         reasoning_tokens=u.reasoning_tokens, **base)
        hold.settle(rec)
        if mode is AIMode.RECORD:
            self.cache.save(endpoint, key, {"events": recorded})
        yield {}, rec
