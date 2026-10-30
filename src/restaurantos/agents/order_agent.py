"""OrderAgent: one strict-schema Spark call for a turn the fast path missed (ARCHITECTURE §5.3 and §5.7, CONTRACTS §7,
CONTRACTS_RUNTIME §10.5).

``parse(utterance, cart, last_question, channel, ctx, *, menu, model=None)``:

1. ``chaos.check(ctx.tenant_id, "meta")`` before anything else; ``ChaosDown`` propagates (CONTRACTS_RUNTIME §10.9).
2. Boundary checks: a non-empty utterance (whitespace collapsed, at most 500 characters), a known channel, and the
   contributor rule (F0 delta 7): ``PolicyViolation`` before any call.
3. ``chat_json(purpose=ORDER, strict schema of the full menu, cache_key order-o1-{menu_hash[:12]}, max_retries=0,
   timeout_s=5, extra_body.safety_identifier=sha256(session_id)[:32])`` under a hard deadline of 5 s + 1 s. The model
   and effort come from the ORDER route (``muse-spark-1.3``, minimal, 1,500 tokens) unless ``model`` is given.
4. Failover (P1-11): when the caller did not pin ``model`` and the primary fails with a timeout, an open breaker
   (``/v1/status`` outages open it too) or a 429/5xx, the same request goes once to ``muse-spark-1.2``. The returned
   CallRecord's ``model`` says which model answered (``is_failover`` → TurnPath ``llm_failover``).
5. The validator (CONTRACTS §7): ``NO_MODIFIERS_PLACEHOLDER`` is stripped, an op whose required fields are null is
   dropped (``cart.well_formed``), an op naming a line that is not in the CART block is dropped and turns into a
   clarification (the number a line added this turn gets depends on merges and on what ``apply_ops`` refuses, which
   the model cannot know), clarify candidates and the question's dish are kept only when on the menu,
   at most 32 ops are kept and free-text fields are capped. Availability and caps stay with ``cart.apply_ops``.

Every other failure (budget, mock mode, a replay miss, unreadable output) raises ``OrderParseUnavailable``, a
``contracts.errors.AIUnavailable`` with the reason in ``details``, so the Dialogue degrades to the fast path with
carriers, buttons, typed input or handoff. A cancellation (barge-in) always propagates, and the cart is never touched.

``warm(ctx)`` sends one parse-shaped call so the prefix is cached, at most every 240 s per cache key (a parse counts
as one) and never raises. ``provide(c)`` sets ``c.order_agent``; it reads ``c.chaos`` at call time because
``judge.chaos`` (P30b) is provided after this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from restaurantos.agents import order_prompt
from restaurantos.config import Purpose, Settings, is_contributor_model
from restaurantos.contracts.common import CHANNELS, CallCtx, Channel
from restaurantos.contracts.errors import AIUnavailable, ChaosDown
from restaurantos.contracts.menu import MenuSnapshot
from restaurantos.contracts.orders import Cart, CartOp, OrderTurnLLM
from restaurantos.contracts.schemas import NO_MODIFIERS_PLACEHOLDER, OrderSchemaVariant
from restaurantos.contracts.ws import CART_OPS_MAX
from restaurantos.domain.cart import well_formed
from restaurantos.meta import errors as me
from restaurantos.services.container import NoopChaos

if TYPE_CHECKING:
    from restaurantos.contracts.ports import ChaosPort, MetaPort
    from restaurantos.meta.spend import CallRecord
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

PARSE_TIMEOUT_S = 5.0
DEADLINE_MARGIN_S = 1.0  # the hard asyncio deadline over the HTTP timeout
WARM_EVERY_S = 240.0
FAILOVER_MODEL = "muse-spark-1.2"
WARM_UTTERANCE = "hello"
MAX_UTTERANCE_CHARS = 500
MAX_TEXT_CHARS = 300
UNAVAILABLE_REASONS = ("timeout", "circuit_open", "api_error", "budget", "mock_mode", "replay_miss", "bad_output",
                       "error")


class OrderParseUnavailable(AIUnavailable):
    """No parse this turn: the Dialogue degrades (ARCHITECTURE §5.7). ``reason`` is one of ``UNAVAILABLE_REASONS``."""

    def __init__(self, reason: str, model: str | None = None) -> None:
        super().__init__(f"order parse unavailable ({reason})",
                         details={"reason": reason, "model": model, "fallback": "fast_path"})
        self.reason = reason
        self.model = model


def _reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError | me.MetaTimeout):
        return "timeout"
    kinds: tuple[tuple[type[BaseException], str], ...] = (
        (me.CircuitOpen, "circuit_open"), (me.BudgetExceeded, "budget"), (me.MetaAPIError, "api_error"),
        (me.MockModeError, "mock_mode"), (me.ReplayMiss, "replay_miss"), (me.MetaError, "bad_output"),
        (ValidationError, "bad_output"), (ValueError, "bad_output"))
    return next((reason for kind, reason in kinds if isinstance(exc, kind)), "error")


def failover_worthy(exc: BaseException) -> bool:
    """A timeout, an open breaker or a 429/5xx: the model is unavailable, not the request wrong."""
    if isinstance(exc, TimeoutError | me.MetaTimeout | me.CircuitOpen):
        return True
    return isinstance(exc, me.MetaAPIError) and (exc.retryable or exc.status == 504)


def clean_utterance(utterance: str) -> str:
    """Whitespace collapsed and capped at ``MAX_UTTERANCE_CHARS``; ValueError when nothing is left."""
    if not isinstance(utterance, str):
        raise TypeError(f"utterance must be a str, got {type(utterance).__name__}")
    text = " ".join(utterance.split())[:MAX_UTTERANCE_CHARS]
    if not text:
        raise ValueError("empty utterance: nothing to parse")
    return text


def safety_identifier(session_id: str | None) -> str | None:
    """``sha256(session_id)[:32]``: Meta's abuse signal without the session id itself."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] if session_id else None


def _cap(text: str | None, limit: int = MAX_TEXT_CHARS) -> str | None:
    return text[:limit] if text is not None else None


def validate_turn(raw: Any, cart: Cart | None, menu: MenuSnapshot) -> OrderTurnLLM:
    """The model's output made safe to apply (CONTRACTS §7 validator; see the module docstring).

    Raises ``pydantic.ValidationError`` when ``raw`` is not an ``OrderTurnLLM``.
    """
    turn = raw if isinstance(raw, OrderTurnLLM) else OrderTurnLLM.model_validate(raw)
    items = {item.code for item in menu.items}
    lines = {line.line_no for line in cart.lines} if cart is not None else set()  # the CART block's L-numbers
    ops: list[CartOp] = []
    unknown_line = False
    for op in turn.ops[:CART_OPS_MAX]:
        if NO_MODIFIERS_PLACEHOLDER in op.modifiers:
            op = op.model_copy(update={"modifiers": [m for m in op.modifiers if m != NO_MODIFIERS_PLACEHOLDER]})
        if not well_formed(op):
            continue
        if op.op != "add" and op.line_ref not in lines:
            unknown_line = True
            continue
        ops.append(op)
    question = turn.question
    if question is not None and question.item_code is not None and question.item_code not in items:
        question = question.model_copy(update={"item_code": None})
    return turn.model_copy(update={
        "ops": ops,
        "question": question,
        "clarify_candidates": list(dict.fromkeys(code for code in turn.clarify_candidates if code in items)),
        "needs_clarification": turn.needs_clarification or unknown_line,
        "unrecognized": _cap(turn.unrecognized),
        "reply_speech": _cap(turn.reply_speech),
        "reply_en": _cap(turn.reply_en),
        "utterance_en": _cap(turn.utterance_en, 2 * MAX_UTTERANCE_CHARS) or "",
    })


def _cart_lines(cart: Cart | None) -> list[tuple[int, str, int, list[str]]]:
    return [(ln.line_no, ln.item_code, ln.qty, ln.modifiers) for ln in cart.lines] if cart is not None else []


def _seed_menu(settings: Settings | None = None) -> MenuSnapshot:
    from restaurantos.seed.loader import load_seed, resolve_template_tenant  # warm-up before any parse

    # The fallback menu is the one the deployment serves: tenants clone the selected template's menu.
    return load_seed(tenant_id=resolve_template_tenant(settings=settings)).menu


class OrderAgent:
    """``OrderAgentPort`` on Spark (see the module docstring)."""

    def __init__(self, meta: MetaPort, settings: Settings, *, chaos: ChaosPort | Callable[[], ChaosPort] | None = None,
                 now_ms: Callable[[], int] | None = None, variant: OrderSchemaVariant | None = None,
                 failover_model: str | None = FAILOVER_MODEL, timeout_s: float = PARSE_TIMEOUT_S,
                 warm_every_s: float = WARM_EVERY_S) -> None:
        self.meta = meta
        self.settings = settings
        if chaos is None or hasattr(chaos, "check"):
            port = chaos or NoopChaos()
            self._chaos: Callable[[], ChaosPort] = lambda: port  # type: ignore[assignment,return-value]
        else:
            self._chaos = chaos  # type: ignore[assignment]
        self.variant: OrderSchemaVariant = variant or settings.order_schema_variant
        self.failover_model = failover_model
        self.timeout_s = timeout_s
        self.warm_every_ms = int(warm_every_s * 1000)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._warmed: dict[str, int] = {}  # cache key -> real ms of the last call that sent that prefix
        self._last_menu: MenuSnapshot | None = None

    @property
    def primary_model(self) -> str:
        return self.settings.route(Purpose.ORDER).model

    def is_failover(self, record: CallRecord) -> bool:
        """True when ``record`` was answered by another model than the ORDER route's (TurnPath ``llm_failover``)."""
        return record.model != self.primary_model

    async def parse(self, utterance: str, cart: Cart, last_question: str | None, channel: Channel, ctx: CallCtx, *,
                    menu: MenuSnapshot, model: str | None = None) -> tuple[OrderTurnLLM, CallRecord]:
        self._chaos().check(ctx.tenant_id, "meta")
        text = clean_utterance(utterance)
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}")
        primary = model or self.primary_model
        self._check_policy(primary, ctx)
        request = order_prompt.build_request(menu, _cart_lines(cart), last_question, channel, text, self.variant)
        try:
            raw, record = await self._call(request, primary, ctx)
        except (me.PolicyViolation, ChaosDown):
            raise
        except Exception as exc:
            fallback = self.failover_model if model is None else None
            if not fallback or fallback == primary or not failover_worthy(exc):
                raise self._unavailable(exc, primary) from exc
            log.warning("order parse failing over from %s to %s (%s)", primary, fallback, _reason(exc))
            raw, record = await self._failover(request, fallback, ctx)
        try:
            turn = validate_turn(raw, cart, menu)
        except (ValidationError, ValueError) as exc:
            raise self._unavailable(exc, record.model) from exc
        self._warmed[request.cache_key] = self._now_ms()
        self._last_menu = menu
        return turn, record

    async def warm(self, ctx: CallCtx, *, menu: MenuSnapshot | None = None) -> None:
        """One cached-prefix call (the last parsed menu, else the seed menu); skipped within 240 s of the last call
        that sent the same prefix, or while chaos has Meta down. Never raises, so callers can fire and forget."""
        try:
            self._chaos().check(ctx.tenant_id, "meta")
            menu = menu or self._last_menu or _seed_menu(self.settings)
            request = order_prompt.build_request(menu, (), None, ctx.channel or "kiosk", WARM_UTTERANCE,
                                                 self.variant)
            now = self._now_ms()
            last = self._warmed.get(request.cache_key)
            if last is not None and now - last < self.warm_every_ms:
                return
            self._warmed[request.cache_key] = now  # claimed before the await: concurrent warm-ups send one call
            self._check_policy(self.primary_model, ctx)
            await self._call(request, self.primary_model, ctx)
        except asyncio.CancelledError:
            raise
        except ChaosDown:
            return
        except Exception as exc:
            log.info("order agent warm-up skipped: %s (%s)", _reason(exc), type(exc).__name__)

    # ----- internals ----------------------------------------------------------------------------------------------

    def _check_policy(self, model: str, ctx: CallCtx) -> None:
        if is_contributor_model(model) and not me.contributor_allowed(self.settings, ctx):
            raise me.PolicyViolation(f"{model} may not parse orders: contributor models only take synthetic dev "
                                     "traffic with no tenant or session")

    async def _call(self, request: order_prompt.OrderRequest, model: str, ctx: CallCtx) -> tuple[Any, CallRecord]:
        sid = safety_identifier(ctx.session_id)
        async with asyncio.timeout(self.timeout_s + DEADLINE_MARGIN_S):
            return await self.meta.chat_json(
                purpose=Purpose.ORDER, messages=request.messages, schema=request.schema, ctx=ctx,
                schema_name=request.schema_name, strict=True, cache_key=request.cache_key, model=model,
                max_retries=0, timeout_s=self.timeout_s, extra_body={"safety_identifier": sid} if sid else None)

    async def _failover(self, request: order_prompt.OrderRequest, model: str,
                        ctx: CallCtx) -> tuple[Any, CallRecord]:
        self._check_policy(model, ctx)
        try:
            return await self._call(request, model, ctx)
        except (me.PolicyViolation, ChaosDown):
            raise
        except Exception as exc:
            raise self._unavailable(exc, model) from exc

    @staticmethod
    def _unavailable(exc: BaseException, model: str) -> OrderParseUnavailable:
        reason = _reason(exc)
        log.warning("order parse unavailable on %s: %s (%s)", model, reason, type(exc).__name__,
                    exc_info=reason == "error")
        return OrderParseUnavailable(reason, model)


def provide(c: Container) -> None:
    """Registry provider ``agents.order_agent`` (P13): sets ``c.order_agent``."""
    c.order_agent = OrderAgent(c.meta, c.settings, chaos=lambda: c.chaos, now_ms=c.clock.now_real_ms)
