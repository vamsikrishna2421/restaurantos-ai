"""Dialogue (ARCHITECTURE §5.1, §5.5-§5.6; CONTRACTS_RUNTIME §10.5): one per voice session, owning the cart in its
``DialogueSession``. States: ``greet → take_order ⇄ clarify → readback → confirm → placed → closed → greet`` (a new
``cart_id``), with the side exits ``answer``, ``sold_out_offer`` and ``handoff``; the rules are in ``reply_policy``.

A turn: text (``handle_transcript``, ``handle_typed``) goes to the fast path first (``settings.fast_path_items`` is
its kill switch), then to a ``precomputed`` speculative parse when the phone turn manager hands one over, else to
``OrderAgent.parse`` after the chaos check; a parse that cannot happen (chaos, timeout, budget, no agent) degrades to
``degraded_one_item`` (phone) or ``buttons_offer`` (kiosk). Item buttons (``handle_cart_ops``), the confirm button and
DTMF (``handle_confirm``, ``handle_dtmf``: 1 yes, 2 change, 0 staff) never reach the model. Ops go through
``cart.apply_ops``; every cart change is announced with ``OrderService.update_cart``; ``reply_policy.choose`` picks
the words. On an explicit yes in the read-back the order is placed **exactly once**
(``OrderService.place(ctx, cart, idem_key=f"{session_id}:{cart_id}")``), then the cart id rotates and ``order_seq``
increments; a cancel clears and rotates the cart.

The session changes only in the last, await-free step of a turn, so a turn cancelled mid-parse (barge-in) or mid-write
leaves it untouched; turns are serialized by a lock. Two consecutive failures on one slot hand off (the turn manager
may open a staff task on ``state == "handoff"``). ``DialogueSession.failures`` also carries ``_corrected`` (1 once the
cart was corrected), so the per-line read-back survives ``/ws/voice?resume=``.

``provide(c)`` sets ``c.dialogue_factory`` to ``Dialogues``, whose ``StoreMenuSource`` reads the menu (cached per
``menu_version``), availability and quotes from the store.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy import select

from restaurantos.agents.order_agent import MAX_UTTERANCE_CHARS, validate_turn
from restaurantos.contracts.common import CallCtx, Channel, Lang, TenantClockSpec
from restaurantos.contracts.errors import AIUnavailable, ChaosDown, Conflict, NotFound, NotWired
from restaurantos.contracts.ids import new_id
from restaurantos.contracts.orders import (
    Cart,
    CartLine,
    CartOp,
    DialogueSession,
    FastPathResult,
    Intent,
    OrderTurnLLM,
    Quote,
    RejectedOp,
    Reply,
    TurnResult,
)
from restaurantos.contracts.ports import DialogueDeps
from restaurantos.contracts.ws import CART_OPS_MAX
from restaurantos.domain import load, quotes
from restaurantos.meta.errors import MetaError
from restaurantos.orders import reply_policy as policy
from restaurantos.orders.lexicon import Lexicons, load_lexicons
from restaurantos.orders.matcher import fast_path
from restaurantos.orders.templates import Templates, speak_language
from restaurantos.services.container import require
from restaurantos.store import repo as store_repo
from restaurantos.store.tables import tenants as tenants_table

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from restaurantos.contracts.menu import Availability, MenuSnapshot
    from restaurantos.contracts.ports import TenantClockPort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

DTMF_INTENTS: dict[str, Intent] = {"1": "confirm_yes", "2": "confirm_no", "0": "human_request"}
MAX_FAILURES = 2  # consecutive failures on one slot before the handoff
CORRECTED = "_corrected"  # DialogueSession.failures marker: the cart was corrected (per-line read-back)
_DEGRADES = (ChaosDown, AIUnavailable, MetaError, NotWired, TimeoutError)  # no parse this turn: degrade, never crash


@runtime_checkable
class QuoteSource(Protocol):
    """The pickup quote for cart lines, as ``OrderService.place`` would compute it now."""

    async def quote(self, tenant_id: str, lines: Sequence[CartLine]) -> Quote: ...


class Dialogue:
    """``DialoguePort`` (see the module docstring). ``Dialogue(ctx, deps)`` starts a session in ``greet``."""

    def __init__(self, ctx: CallCtx, deps: DialogueDeps, *, session: DialogueSession | None = None,
                 lexicons: Lexicons | None = None, quote_source: QuoteSource | None = None,
                 fast_path_items: bool = True) -> None:
        if not ctx.tenant_id:
            raise ValueError("a Dialogue needs ctx.tenant_id")
        self.tenant_id: str = ctx.tenant_id
        self.session_id = ctx.session_id or new_id("vs")
        self.ctx = ctx.model_copy(update={"session_id": self.session_id})
        self.deps = deps
        self.channel: Channel = ctx.channel or "kiosk"
        self._lexicons = lexicons or load_lexicons()
        self._quotes = quote_source
        self._fast_path_items = fast_path_items
        speakable = getattr(deps.templates, "speakable", None)
        self.speak_languages = speakable(deps.speak_languages) if callable(speakable) else list(deps.speak_languages)
        self._lock = asyncio.Lock()
        self._last_reply: Reply | None = None
        self._offer: policy.Offer | None = None
        if session is None:
            lang: Lang = ctx.lang_hint or "en"
            cart_id = new_id("crt")
            cart = Cart(session_id=self.session_id, cart_id=cart_id, language=lang, channel=self.channel)
            session = DialogueSession(cart=cart, cart_id=cart_id, lang=lang)
        elif session.cart.session_id != self.session_id or session.cart.cart_id != session.cart_id:
            raise ValueError("that DialogueSession belongs to another session or cart")
        self._s = session.model_copy(deep=True)

    @classmethod
    def restore(cls, ctx: CallCtx, deps: DialogueDeps, session: DialogueSession, **options: Any) -> Dialogue:
        """``/ws/voice?resume=``: the same cart, state and ``cart_id``."""
        return cls(ctx, deps, session=session, **options)

    def snapshot(self) -> DialogueSession:
        return self._s.model_copy(deep=True)

    def greeting(self) -> Reply:
        """The opening line (``greet_phone``/``greet_kiosk``) in the session's speak language; not a turn."""
        lang = speak_language(self._s.lang, self.speak_languages)
        return self.deps.templates.render("greet_phone" if self.channel == "phone" else "greet_kiosk", lang)

    # ----- DialoguePort ---------------------------------------------------------------------------------------------

    async def handle_transcript(self, text: str, *, turn_id: str,
                                precomputed: OrderTurnLLM | None = None) -> TurnResult:
        return await self._text(text, typed=False, precomputed=precomputed)

    async def handle_typed(self, text: str, *, turn_id: str) -> TurnResult:
        return await self._text(text, typed=True, precomputed=None)

    async def handle_cart_ops(self, ops: list[CartOp], *, turn_id: str) -> TurnResult:
        """Kiosk item buttons: validated by ``apply_ops`` like any op, never the model."""
        ops = [CartOp.model_validate(op) for op in ops][:CART_OPS_MAX]
        intent: Intent = "order" if all(op.op == "add" for op in ops) else "modify"
        return await self._run(FastPathResult(kind="items", intent=intent, ops=ops, closing=False,
                                              language=self._s.lang))

    async def handle_confirm(self, yes: bool, *, turn_id: str) -> TurnResult:
        """The kiosk confirm button, handled like DTMF 1 (yes) or 2 (change)."""
        return await self._run(self._key("confirm_yes" if yes else "confirm_no"), keys=True)

    async def handle_dtmf(self, digit: str) -> TurnResult | None:
        """1 yes, 2 change, 0 staff; None for any other key (``#`` codes belong to the phone line)."""
        intent = DTMF_INTENTS.get(str(digit).strip())
        return None if intent is None else await self._run(self._key(intent), keys=True)

    # ----- one turn -------------------------------------------------------------------------------------------------

    def _key(self, intent: Intent) -> FastPathResult:
        return FastPathResult(kind="control", intent=intent, ops=[], closing=False, language=self._s.lang)

    async def _run(self, result: FastPathResult, *, keys: bool = False) -> TurnResult:
        """A button or key turn: no parse, no model."""
        async with self._lock:
            turn, menu, avail = await self._begin()
            turn.result, turn.intent, turn.explicit, turn.keys = result, result.intent, keys, keys
            return await self._decide_and_finish(turn, menu, avail)

    async def _text(self, text: str, *, typed: bool, precomputed: OrderTurnLLM | None) -> TurnResult:
        text = " ".join(str(text).split())[:MAX_UTTERANCE_CHARS]
        async with self._lock:
            turn, menu, avail = await self._begin(text)
            turn.path = "fastpath"
            turn.spans.parse_start_ms = self.deps.clock.now_real_ms()
            hit = fast_path(text, menu, self._lexicons, self._s.cart, self._s.lang,
                            items=self._fast_path_items) if text else None
            if not text:
                turn.cue = "didnt_catch"
            elif hit is not None:
                self._take(turn, hit, menu)
            elif precomputed is not None:  # the speculative parse the phone turn manager already paid for
                turn.path = "llm"
                self._take(turn, validate_turn(precomputed, self._s.cart, menu), menu)
            elif (parsed := await self._parse(turn, text, menu, typed=typed)) is not None:
                self._take(turn, parsed, menu)
            turn.spans.parse_done_ms = self.deps.clock.now_real_ms()
            return await self._decide_and_finish(turn, menu, avail)

    async def _begin(self, transcript: str = "") -> tuple[policy.Turn, MenuSnapshot, Availability]:
        menu = await self.deps.menu.menu_snapshot(self.tenant_id)
        avail = await self.deps.menu.availability(self.tenant_id)
        s = self._s
        return policy.Turn(new_id("tr"), s.cart, s.state, s.lang, transcript=transcript), menu, avail

    async def _parse(self, turn: policy.Turn, text: str, menu: MenuSnapshot, *, typed: bool) -> OrderTurnLLM | None:
        try:
            self.deps.chaos.check(self.tenant_id, "meta")
            parsed, record = await self.deps.agent.parse(text, self._s.cart, self._s.last_question, self.channel,
                                                         self._ctx(turn), menu=menu)
        except _DEGRADES as exc:
            log.info("dialogue %s: no parse this turn (%s); degrading", self.session_id, type(exc).__name__)
            turn.path, turn.cue = "degraded", "degraded"
            return None
        turn.cost_micros = round(record.cost_usd * 1_000_000)
        failover = getattr(self.deps.agent, "is_failover", None)
        turn.path = "typed" if typed else "llm_failover" if callable(failover) and failover(record) else "llm"
        return validate_turn(parsed, self._s.cart, menu)  # idempotent for the real agent; the fakes skip it

    def _take(self, turn: policy.Turn, result: FastPathResult | OrderTurnLLM, menu: MenuSnapshot) -> None:
        turn.result, turn.intent, turn.closing, turn.language = result, result.intent, result.closing, result.language
        if isinstance(result, OrderTurnLLM):
            turn.utterance_en = result.utterance_en
            return
        turn.explicit = result.kind == "control"
        english = result.language == "en"
        turn.utterance_en = turn.transcript if english else policy.gloss(result, menu, self.deps.templates)

    async def _decide_and_finish(self, turn: policy.Turn, menu: MenuSnapshot, avail: Availability) -> TurnResult:
        if policy.decide(turn, self._s, menu, avail, offer=self._offer, last_reply=self._last_reply):
            await self._place(turn, menu)
        return await self._finish(turn, menu)

    async def _place(self, turn: policy.Turn, menu: MenuSnapshot) -> None:
        cart = self._s.cart
        if not cart.lines:
            return turn.to("take_order", "nothing_ordered")
        if "large_order_handoff" in cart.flags:
            return turn.to("handoff", "large_order")
        try:
            order = await self.deps.orders.place(self._ctx(turn), cart, idem_key=f"{self.session_id}:{cart.cart_id}")
        except Conflict as exc:
            return await self._refused(turn, exc, menu)
        turn.order, turn.rotate = order, True
        return turn.to("placed")

    async def _refused(self, turn: policy.Turn, exc: Conflict, menu: MenuSnapshot) -> None:
        """``place`` refused the cart (409): it was empty or too large, or a dish was 86'd since the read-back. Then
        the cart is rebuilt against fresh availability, so the guard names the sold-out dish and its alternatives."""
        reason = exc.details.get("reason")
        if reason != "invalid_cart":
            cue: policy.Cue = {"empty_cart": "nothing_ordered", "large_order_handoff": "large_order"}.get(
                str(reason), "human")  # type: ignore[assignment]
            return turn.to("take_order" if cue == "nothing_ordered" else "handoff", cue)
        cart = self._s.cart
        turn.cart = cart.model_copy(update={"lines": [], "subtotal_cents": 0, "tax_cents": 0, "total_cents": 0})
        turn.corrected = True
        ops = [CartOp(op="add", item_code=ln.item_code, qty=ln.qty, modifiers=list(ln.modifiers)) for ln in cart.lines]
        policy.apply_ops(turn, ops, self._s, menu, await self.deps.menu.availability(self.tenant_id))
        return None

    # ----- finishing a turn -----------------------------------------------------------------------------------------

    async def _finish(self, turn: policy.Turn, menu: MenuSnapshot) -> TurnResult:
        s = self._s
        failures = {CORRECTED: 1} if (CORRECTED in s.failures or turn.corrected) and not turn.rotate else {}
        if turn.failure is not None:
            count = s.failures.get(turn.failure, 0) + 1
            if count >= MAX_FAILURES:
                turn.state, turn.cue, turn.offer, turn.reply = "handoff", "failures", None, None
            else:
                failures[turn.failure] = count
        lang = policy.next_language(turn, s.lang)
        if turn.order is None and turn.cart.language != lang:  # the cart speaks the guest's language from turn one
            turn.cart = turn.cart.model_copy(update={"language": lang})
        rejected = list(turn.apply.rejected) if turn.apply is not None else []
        if turn.announce or rejected or _changed(turn.cart, s.cart):
            await self.deps.orders.update_cart(self._ctx(turn), turn.cart, rejected)
        view = policy.ReplyView(menu=menu, cart=turn.cart, channel=self.channel,
                                minutes=await self._minutes(turn, menu), explicit=CORRECTED in failures,
                                corrected=turn.corrected, order=turn.order, cue=turn.cue, candidates=turn.candidates,
                                then_reprompt=turn.then_reprompt)
        speak = speak_language(lang, self.speak_languages)
        reply = turn.reply or policy.choose(turn.result, turn.apply, turn.state, speak, self.deps.templates, view=view)
        return self._commit(turn, reply, failures, lang, rejected)

    def _commit(self, turn: policy.Turn, reply: Reply, failures: dict[str, int], lang: Lang,
                rejected: list[RejectedOp]) -> TurnResult:
        """The only place the session changes; no await from here on."""
        s = self._s
        s.last_turn_no += 1
        s.lang = lang
        if turn.rotate:
            s.order_seq += int(turn.order is not None)
            s.cart_id = new_id("crt")
            s.cart = Cart(session_id=self.session_id, cart_id=s.cart_id, language=lang, channel=self.channel)
        else:
            s.cart = turn.cart
        s.failures, s.state = failures, turn.state
        if turn.reply is None:
            s.last_question = policy.question_code(reply)
            self._last_reply = reply
        self._offer = turn.offer
        order = turn.order
        return TurnResult(session_id=self.session_id, turn_no=s.last_turn_no, transcript=turn.transcript,
                          utterance_en=turn.utterance_en, language=turn.language, intent=turn.intent,
                          closing=turn.closing, path=turn.path, cart=turn.cart if order else s.cart,
                          rejected=rejected, reply=reply, state=turn.state, order_id=order.id if order else None,
                          placed=order is not None, spans=turn.spans, ai_cost_micros=turn.cost_micros,
                          trace_id=turn.trace_id)

    async def _minutes(self, turn: policy.Turn, menu: MenuSnapshot) -> int | None:
        """The quote a read-back or a wait-time answer states, computed in code."""
        llm = turn.result if isinstance(turn.result, OrderTurnLLM) else None
        wait = llm is not None and llm.question is not None and llm.question.topic == "wait_time"
        if turn.state != "readback" and not wait:
            return None
        lines = turn.cart.lines or (_probe(menu) if wait else [])
        if not lines:
            return None
        if self._quotes is not None:
            try:
                return (await self._quotes.quote(self.tenant_id, lines)).minutes
            except Exception:  # the read-back still needs a number: estimate from prep times alone
                log.warning("dialogue %s: quote unavailable; estimating from the menu", self.session_id,
                            exc_info=True)
        try:
            return quotes.quote(lines, menu, (), (), self.deps.clock.now_biz_ms()).minutes
        except ValueError:
            return quotes.MIN_QUOTE_MIN

    def _ctx(self, turn: policy.Turn) -> CallCtx:
        return self.ctx.model_copy(update={"trace_id": turn.trace_id})


def _changed(after: Cart, before: Cart) -> bool:
    return after.cart_id != before.cart_id or after.lines != before.lines or after.flags != before.flags


def _probe(menu: MenuSnapshot) -> list[CartLine]:
    """One available dish per station: what "how long is the wait?" quotes while the cart is empty."""
    first: dict[str, Any] = {}
    for item in menu.items:
        if item.available:
            first.setdefault(item.station_id, item)
    return [CartLine(line_no=n, item_code=item.code, qty=1, unit_price_cents=item.price_cents,
                     line_total_cents=item.price_cents) for n, item in enumerate(first.values(), start=1)]


# ----- the provider ---------------------------------------------------------------------------------------------------


class StoreMenuSource:
    """``MenuSource`` and ``QuoteSource`` on the store, one read each: the menu cached per tenant and ``menu_version``
    (an 86 or a scenario reset bumps it), availability read fresh every turn, and quotes computed as
    ``OrderService.place`` computes them (open tickets, active offsets, the tenant's business clock)."""

    CACHE_SIZE = 64

    def __init__(self, c: Container) -> None:
        self._c = c
        self._menus: OrderedDict[str, MenuSnapshot] = OrderedDict()
        self._specs: dict[str, TenantClockSpec] = {}

    def _repo(self) -> Any:
        return self._c.repo if self._c.repo is not None else store_repo

    async def _menu(self, conn: AsyncConnection, tenant_id: str) -> MenuSnapshot:
        row = (await conn.execute(select(tenants_table.c.menu_version, tenants_table.c.clock)
                                  .where(tenants_table.c.id == tenant_id))).one_or_none()
        if row is None:
            raise NotFound(f"unknown tenant {tenant_id!r}")
        self._specs[tenant_id] = TenantClockSpec.model_validate(row.clock)
        menu = self._menus.get(tenant_id)
        if menu is None or menu.menu_version != row.menu_version:
            menu = await self._repo().menu_snapshot(conn, tenant_id)
        self._menus[tenant_id] = menu
        self._menus.move_to_end(tenant_id)
        while len(self._menus) > self.CACHE_SIZE:
            self._menus.popitem(last=False)
        return menu

    async def menu_snapshot(self, tenant_id: str) -> MenuSnapshot:
        async with require(self._c, "store").read() as conn:
            return await self._menu(conn, tenant_id)

    async def availability(self, tenant_id: str) -> Availability:
        async with require(self._c, "store").read() as conn:
            return await self._repo().availability(conn, tenant_id)

    async def quote(self, tenant_id: str, lines: Sequence[CartLine]) -> Quote:
        repo = self._repo()
        async with require(self._c, "store").read() as conn:
            menu = await self._menu(conn, tenant_id)
            now_biz = self.clock(tenant_id).now_biz_ms()
            offsets = await repo.active_offsets(conn, tenant_id, now_biz)
            tickets = await repo.open_tickets(conn, tenant_id)
        loads = load.station_loads(tickets, menu.stations, menu, offsets, now_biz)
        return quotes.quote(lines, menu, loads, offsets, now_biz, tz=self._c.settings.tenant_timezone)

    def clock(self, tenant_id: str) -> TenantClockPort:
        return _TenantClock(self._c, self._specs, tenant_id)


class _TenantClock:
    """``TenantClockPort``: business time from the tenant's clock spec as of the last store read, else real time."""

    def __init__(self, c: Container, specs: dict[str, TenantClockSpec], tenant_id: str) -> None:
        self._c, self._specs, self._tenant_id = c, specs, tenant_id

    def _clock(self) -> TenantClockPort:
        return self._c.clock.for_tenant(self._specs.get(self._tenant_id) or TenantClockSpec())

    def now_biz_ms(self) -> int:
        return self._clock().now_biz_ms()

    def now_real_ms(self) -> int:
        return self._c.clock.now_real_ms()

    def biz_iso(self) -> str:
        return self._clock().biz_iso()


class _NoAgent:
    """Stands in while ``c.order_agent`` is unwired: every parse degrades (fast path, buttons, typed input)."""

    async def parse(self, *args: Any, **kwargs: Any) -> Any:
        raise NotWired("order_agent")

    async def warm(self, ctx: CallCtx) -> None:
        return None


class Dialogues:
    """``DialogueFactory`` (``c.dialogue_factory``): one ``Dialogue`` per voice session, sharing the phrase files, the
    lexicons and the store-backed menu source. ``speak_languages`` is what ``/ws/voice`` announces in ``ready``."""

    def __init__(self, c: Container, *, templates: Templates | None = None, lexicons: Lexicons | None = None) -> None:
        self._c = c
        self.templates = templates or Templates.load(defaults={"url": spoken_url(c.settings.public_base_url)})
        self.lexicons = lexicons or load_lexicons()
        self.menus = StoreMenuSource(c)
        self.speak_languages = self.templates.speakable(c.settings.speak_languages)
        if skipped := [lang for lang in c.settings.speak_languages if lang not in self.speak_languages]:
            log.warning("speak_languages %s not spoken: their phrase files are not reviewed", skipped)

    def deps(self, ctx: CallCtx) -> DialogueDeps:
        """The session's ports, read now: every provider (``judge.chaos`` included) has run by the first session."""
        if not ctx.tenant_id:
            raise ValueError("a dialogue needs ctx.tenant_id")
        return DialogueDeps(orders=require(self._c, "orders"), agent=self._c.order_agent or _NoAgent(),
                            menu=self.menus, templates=self.templates, clock=self.menus.clock(ctx.tenant_id),
                            speak_languages=list(self.speak_languages), chaos=self._c.chaos)

    def create(self, ctx: CallCtx) -> Dialogue:
        return Dialogue(ctx, self.deps(ctx), **self._options())

    def restore(self, ctx: CallCtx, session: DialogueSession) -> Dialogue:
        return Dialogue.restore(ctx, self.deps(ctx), session, **self._options())

    def _options(self) -> dict[str, Any]:
        return {"lexicons": self.lexicons, "quote_source": self.menus,
                "fast_path_items": self._c.settings.fast_path_items}


def spoken_url(url: str) -> str:
    """The public URL as it is said aloud: no scheme, no trailing slash ("restaurantos.fly.dev")."""
    return url.split("://", 1)[-1].rstrip("/")


def provide(c: Container) -> None:
    """PROVIDERS ``orders.dialogue`` (P14): sets ``c.dialogue_factory``. The phrase files and lexicons load here, so a
    broken file fails the boot; the order service, the agent and chaos are read per session."""
    c.dialogue_factory = Dialogues(c)
