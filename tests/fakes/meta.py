"""FakeMetaClient: a scripted ``MetaPort`` (CONTRACTS_RUNTIME §10.11).

``FakeMetaClient(script)`` maps a ``Purpose`` to the outputs its calls return, in order:

- ``chat_json``: a dict, JSON text or pydantic model (validated into ``schema`` when it is a model class);
- ``chat_text``: a string; ``responses``: a dict or model;
- ``chat_stream`` / ``responses_stream``: a list of ``StreamEvent`` (or plain strings, read as text deltas);
  ``done`` is appended with the call record when the script leaves it out;
- ``transcribe_file`` (purpose ``asr``): a ``TranscribeResult``, a dict of its fields, or the transcript string.

An exception instance in the script is raised by that call; a callable is called with the ``FakeCall`` and its result
used. Every call is recorded in ``calls``. Paid calls hold and settle a $0 ``mock`` CallRecord through the SpendGuard
when one is given, so the kill switch, unknown scopes and pool rules still apply. Nothing touches the network.

Timing matches ``MetaClient``: the async methods do everything when awaited. The two streams are async generators
there, so calling one does nothing and the spend hold (``BudgetExceeded``) and scripted errors surface on the first
iteration. The fake records the call and takes its scripted output when it is called (outputs follow call order),
but holds, evaluates a callable output and raises only on the first iteration.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from restaurantos.config import Purpose, default_routes
from restaurantos.contracts.common import CallCtx
from restaurantos.meta.client import MetaStatus, StreamEvent, TranscribeResult
from restaurantos.meta.errors import models_reported_down
from restaurantos.meta.spend import CallRecord, SpendGuard


class FakeScriptExhausted(LookupError):
    """A call found no scripted output left for its purpose."""


@dataclass
class FakeCall:
    method: str
    purpose: str | None
    ctx: CallCtx | None
    kwargs: dict[str, Any] = field(default_factory=dict)
    record: CallRecord | None = None


class FakeMetaClient:
    def __init__(self, script: Mapping[Purpose | str, Iterable[Any]] | None = None, *,
                 guard: SpendGuard | None = None, clock: Callable[[], int] | Any = None,
                 status: dict[str, Any] | None = None) -> None:
        self.script: dict[Purpose, deque[Any]] = {}
        for purpose, outputs in (script or {}).items():
            self.add(purpose, *outputs)
        self.guard = guard
        self._clock = clock
        self.status_payload = status or {"status": "ok"}
        self.files: list[dict[str, Any]] = []
        self.breakers: dict[str, str] = {}
        self.calls: list[FakeCall] = []
        self.closed = False

    # ----- script and inspection -------------------------------------------------------------------------------
    def add(self, purpose: Purpose | str, *outputs: Any) -> None:
        self.script.setdefault(Purpose(purpose), deque()).extend(outputs)

    def remaining(self, purpose: Purpose | str) -> int:
        return len(self.script.get(Purpose(purpose), ()))

    def calls_for(self, purpose: Purpose | str | None = None, method: str | None = None) -> list[FakeCall]:
        want = None if purpose is None else str(Purpose(purpose))
        return [c for c in self.calls if (want is None or c.purpose == want) and (method is None or c.method == method)]

    # ----- internals -------------------------------------------------------------------------------------------
    def _now_s(self) -> float | None:
        if self._clock is None:
            return None
        now = self._clock.now_real_ms() if hasattr(self._clock, "now_real_ms") else self._clock()
        return now / 1000

    def _call(self, method: str, purpose: Purpose | str, ctx: CallCtx, kwargs: dict[str, Any]) -> FakeCall:
        call = FakeCall(method, str(Purpose(purpose)), ctx, kwargs)
        self.calls.append(call)
        return call

    def _settle(self, call: FakeCall, endpoint: str) -> CallRecord:
        """Hold and settle $0 like MetaClient would (BudgetExceeded propagates); sets ``call.record``."""
        ctx = call.ctx
        assert ctx is not None
        purpose = Purpose(call.purpose)
        model = call.kwargs.get("model") or default_routes("test")[purpose].model
        fields: dict[str, Any] = {"ts": now} if (now := self._now_s()) is not None else {}
        record = CallRecord(purpose=str(purpose), model=model, endpoint=endpoint, status="mock", cost_usd=0.0,
                            session_id=ctx.session_id, tenant_id=ctx.tenant_id, trace_id=ctx.trace_id,
                            data_class=ctx.data_class, spend_scope=ctx.spend_scope, pool=ctx.spend_pool, **fields)
        if self.guard is not None:
            hold = self.guard.hold(0.0, scope=ctx.spend_scope, pool=ctx.spend_pool, scope_cap=ctx.scope_cap_usd)
            record = hold.settle(record)
        call.record = record
        return record

    def _paid(self, method: str, endpoint: str, purpose: Purpose | str, ctx: CallCtx,
              kwargs: dict[str, Any]) -> FakeCall:
        """Record the call, then hold and settle $0 like MetaClient would (BudgetExceeded propagates)."""
        call = self._call(method, purpose, ctx, kwargs)
        self._settle(call, endpoint)
        return call

    def _pop(self, call: FakeCall) -> Any:
        queue = self.script.get(Purpose(call.purpose))
        if not queue:
            raise FakeScriptExhausted(f"no scripted output left for purpose {call.purpose!r} ({call.method})")
        return queue.popleft()

    @staticmethod
    def _resolve(call: FakeCall, out: Any) -> Any:
        """A callable output is called with the call; an exception is raised."""
        if callable(out) and not isinstance(out, type | BaseModel):
            out = out(call)
        if isinstance(out, BaseException):
            raise out
        return out

    def _next(self, call: FakeCall) -> Any:
        return self._resolve(call, self._pop(call))

    @staticmethod
    def _as_json(out: Any) -> Any:
        if isinstance(out, BaseModel):
            return out.model_dump(mode="json")
        return json.loads(out) if isinstance(out, str | bytes) else out

    def _events(self, out: Any, record: CallRecord) -> list[StreamEvent]:
        items = [out] if isinstance(out, str | StreamEvent) else list(out)
        events = [StreamEvent("text", text=e) if isinstance(e, str) else e for e in items]
        if events and events[-1].kind == "done":
            return [*events[:-1], replace(events[-1], record=events[-1].record or record)]
        return [*events, StreamEvent("done", record=record)]

    def _stream(self, method: str, endpoint: str, purpose: Purpose | str, ctx: CallCtx,
                kwargs: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """Records the call and takes its output now (outputs follow call order); the spend hold and any scripted
        error happen on the first iteration, as in MetaClient's async generators."""
        call = self._call(method, purpose, ctx, kwargs)
        raw = self._pop(call)

        async def events() -> AsyncIterator[StreamEvent]:
            record = self._settle(call, endpoint)
            for event in self._events(self._resolve(call, raw), record):
                yield event

        return events()

    # ----- paid calls ------------------------------------------------------------------------------------------
    async def chat_json(self, *, purpose: Purpose, messages: list[dict[str, Any]],
                        schema: dict[str, Any] | type[BaseModel], ctx: CallCtx, schema_name: str = "Output",
                        strict: bool = True, cache_key: str | None = None, model: str | None = None,
                        effort: str | None = None, max_retries: int | None = None, timeout_s: float | None = None,
                        extra_body: dict[str, Any] | None = None) -> tuple[Any, CallRecord]:
        call = self._paid("chat_json", "chat", purpose, ctx, {
            "messages": messages, "schema": schema, "schema_name": schema_name, "strict": strict,
            "cache_key": cache_key, "model": model, "effort": effort, "max_retries": max_retries,
            "timeout_s": timeout_s, "extra_body": extra_body})
        out = self._next(call)
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            value = out if isinstance(out, schema) else schema.model_validate(self._as_json(out))
        else:
            value = self._as_json(out)
        assert call.record is not None
        return value, call.record

    async def chat_text(self, *, purpose: Purpose, messages: list[dict[str, Any]], ctx: CallCtx,
                        cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                        max_retries: int | None = None, timeout_s: float | None = None,
                        extra_body: dict[str, Any] | None = None) -> tuple[str, CallRecord]:
        call = self._paid("chat_text", "chat", purpose, ctx, {
            "messages": messages, "cache_key": cache_key, "model": model, "effort": effort,
            "max_retries": max_retries, "timeout_s": timeout_s, "extra_body": extra_body})
        out = self._next(call)
        assert call.record is not None
        return (out if isinstance(out, str) else json.dumps(self._as_json(out))), call.record

    def chat_stream(self, *, purpose: Purpose, messages: list[dict[str, Any]], ctx: CallCtx,
                    cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                    response_format: dict[str, Any] | None = None, timeout_s: float | None = None,
                    extra_body: dict[str, Any] | None = None) -> AsyncIterator[StreamEvent]:
        return self._stream("chat_stream", "chat_stream", purpose, ctx, {
            "messages": messages, "cache_key": cache_key, "model": model, "effort": effort,
            "response_format": response_format, "timeout_s": timeout_s, "extra_body": extra_body})

    async def responses(self, *, purpose: Purpose, input: Any, ctx: CallCtx, instructions: str | None = None,
                        tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
                        previous_response_id: str | None = None, reasoning_summary: str | None = None,
                        cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                        max_retries: int | None = None, timeout_s: float | None = None,
                        extra_body: dict[str, Any] | None = None) -> tuple[dict[str, Any], CallRecord]:
        call = self._paid("responses", "responses", purpose, ctx, {
            "input": input, "instructions": instructions, "tools": tools, "text_format": text_format,
            "previous_response_id": previous_response_id, "reasoning_summary": reasoning_summary,
            "cache_key": cache_key, "model": model, "effort": effort, "max_retries": max_retries,
            "timeout_s": timeout_s, "extra_body": extra_body})
        out = self._as_json(self._next(call))
        assert call.record is not None
        return out, call.record

    def responses_stream(self, *, purpose: Purpose, input: Any, ctx: CallCtx, instructions: str | None = None,
                         tools: list[dict[str, Any]] | None = None, text_format: dict[str, Any] | None = None,
                         previous_response_id: str | None = None, reasoning_summary: str | None = "auto",
                         cache_key: str | None = None, model: str | None = None, effort: str | None = None,
                         timeout_s: float | None = None,
                         extra_body: dict[str, Any] | None = None) -> AsyncIterator[StreamEvent]:
        return self._stream("responses_stream", "responses_stream", purpose, ctx, {
            "input": input, "instructions": instructions, "tools": tools, "text_format": text_format,
            "previous_response_id": previous_response_id, "reasoning_summary": reasoning_summary,
            "cache_key": cache_key, "model": model, "effort": effort, "timeout_s": timeout_s,
            "extra_body": extra_body})

    async def transcribe_file(self, wav: bytes, *, ctx: CallCtx, mode: str = "PUSH_TO_TALK",
                              keywords: list[str] | None = None, language_bias: list[str] | None = None,
                              max_retries: int | None = None,
                              timeout_s: float | None = None) -> tuple[TranscribeResult, CallRecord]:
        call = self._paid("transcribe_file", "asr_file", Purpose.ASR, ctx, {
            "wav_bytes": len(wav), "mode": mode, "keywords": keywords, "language_bias": language_bias,
            "max_retries": max_retries, "timeout_s": timeout_s})
        out = self._next(call)
        if isinstance(out, str):
            out = TranscribeResult(transcript=out, audio_ms=0)
        elif isinstance(out, dict):
            out = TranscribeResult(**out)
        assert call.record is not None
        return out, call.record

    # ----- free calls (no ctx, no spend) -----------------------------------------------------------------------
    def _free(self, method: str, **kwargs: Any) -> None:
        self.calls.append(FakeCall(method, None, None, kwargs))

    async def status(self) -> dict[str, Any]:
        self._free("status")
        return dict(self.status_payload)

    async def refresh_status(self) -> MetaStatus:
        data = await self.status()
        down = sorted(models_reported_down(data))
        for model in down:
            self.breakers[f"chat:{model}"] = self.breakers[f"responses:{model}"] = "open"
        overall = data.get("status")
        return MetaStatus(str(overall if isinstance(overall, str) else "unknown"), down, data)

    def breaker_states(self) -> dict[str, str]:
        return dict(self.breakers)

    def force_open(self, endpoint: str, model: str, seconds: float | None = None) -> None:
        self._free("force_open", endpoint=endpoint, model=model, seconds=seconds)
        self.breakers[f"{endpoint}:{model}"] = "open"

    async def list_files(self, *, purpose: str | None = None, limit: int | None = None,
                         order: str | None = None) -> list[dict[str, Any]]:
        self._free("list_files", purpose=purpose, limit=limit, order=order)
        files = [f for f in self.files if purpose is None or f.get("purpose") == purpose]
        return files[:limit] if limit else files

    async def delete_file(self, file_id: str) -> dict[str, Any]:
        self._free("delete_file", file_id=file_id)
        self.files = [f for f in self.files if f.get("id") != file_id]
        return {"id": file_id, "object": "file", "deleted": True}

    async def aclose(self) -> None:
        self.closed = True
