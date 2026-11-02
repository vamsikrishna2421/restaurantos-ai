"""Win-back marketing (P45): bringing drifted regulars back, under consent and a manager's eye.

The goodwill engine (P43) already answers a lapsed regular *when they walk in on their own* — a
dish on the house, waiting. This service is the outbound half the founder asked for: find the
regulars who drifted, and invite them back. It is deliberately narrow, and the narrowness is the
feature:

- **Who can be contacted** is arithmetic, not a model judgement: the same lapsed-regular rule the
  goodwill signal uses (``REGULAR_MIN_VISITS``+ visits, last visit ``LAPSED_DAYS``+ ago), computed
  from the guest's own orders by ``services.customers``' attribution — and only when the guest's
  stored ``marketing_opt_in`` is true. Guests who never opted in are a count in the view, never a
  row, and the draft and send endpoints re-check the flag at the moment they act: asking to draft
  or send to a guest without opt-in is refused (``not_opted_in``), never silently skipped.
- **The message** is a draft until a manager sends it. In mock mode it is a deterministic template
  standing on the guest's facts (name, usual dish, days away); in live mode the model writes it
  through the existing Meta port (purpose ``gm``), and any failure falls back to the template — a
  draft is a starting point either way, and the manager can edit every word before sending. Since
  pass 17 the live call carries the usual dish's egress code, never its name (dish names are the
  kitchen's secret too), and the model's draft is translated back before it is stored.
- **The offer** is the standing lapsed-goodwill promise — their usual on the house. Sending mints
  that dish as a credit in the customers book through the book's one mint path (key ``winback:…``),
  so the returning guest's offer applies itself at placement exactly like a goodwill approval.
  If a credit already waits for the guest, no second one is minted; the send links to the waiting
  one instead.
- **The send** goes through the messaging port (``services.messaging``): simulated and labelled in
  mock mode; in live mode, with no provider configured, it refuses in plain words and the drafts
  stay drafts. Every send is recorded on the outreach row: text, approver, channel, simulated flag.
- **The return** is matched on read, deterministically: a sent outreach whose guest has since
  placed a (non-cancelled) order is marked ``returned`` with that order's id — the same
  evaluate-on-read shape as the goodwill feed, no scheduler.

The full number never leaves ``services.customers``' book: drafts and sends are keyed by the
deterministic case key, surfaces carry masked numbers, and the port receives the number's key only
inside the send call itself.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from restaurantos.config import AIMode, Purpose
from restaurantos.contracts.api import (
    WinbackDraftResult,
    WinbackGuest,
    WinbackOutreachView,
    WinbackSendResult,
    WinbackSkipped,
    WinbackView,
)
from restaurantos.contracts.common import CallCtx, biz_from_ms
from restaurantos.contracts.errors import BadRequest, NotFound
from restaurantos.domain import customers as cu
from restaurantos.meta.egress import EgressMap
from restaurantos.services.container import Container
from restaurantos.services.customers import CustomerService, Outreach, mask_phone
from restaurantos.services.messaging import UnavailableMessaging, adapter_for

log = logging.getLogger(__name__)

WINBACK_SHOWN = 50  # the whole drifted-and-contactable book, realistically; the view says when capped
WINBACK_NOTE = ("A regular drifts after 14 days away. Only guests who opted in to messages at the kiosk "
                "can be contacted, and only with a text a manager drafts, edits and sends — nothing "
                "goes out on its own. The offer is their usual on the house, held as a credit on "
                "their number until they come back.")


def case_key(history: cu.CustomerHistory) -> str:
    """The deterministic case key for one guest's current drift, re-derived from the same facts (a
    new drift — a later last visit — is a new case). It carries the MASKED number, not the number:
    unlike a suggestion key inside the book, this key travels to staff surfaces in the view, and
    the last visit's order id keeps it unique per guest."""
    last = history.visits[-1]
    return f"winback:{mask_phone(history.phone_key)}:lapsed:{last.id}"


def template_message(name: str | None, restaurant: str, usual_name: str | None, days_away: int) -> str:
    """The deterministic draft: the guest's own facts, the house's standing offer, nothing invented."""
    hello = f"Hi {name}," if name else "Hi,"
    dish = f" Your usual — {usual_name} — is on the house when you come back." if usual_name else \
        " A dish on the house is waiting when you come back."
    return (f"{hello} it's {restaurant}. We've missed you — it's been {days_away} days since your "
            f"last visit.{dish} Hope to see you soon.")


class WinbackService:
    """The win-back reads and decisions for one app, over the customers book (per data dir)."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._customers = CustomerService(c)
        self.store = self._customers.store

    @property
    def _tz(self) -> str:
        return self._c.settings.tenant_timezone

    def _messaging(self):
        return self._c.messaging if self._c.messaging is not None else adapter_for(self._c)

    def _provider_note(self) -> str:
        messaging = self._messaging()
        if isinstance(messaging, UnavailableMessaging):
            return ("No messaging provider is configured for this restaurant, so messages can't be "
                    "sent yet. Drafts can still be prepared and edited.")
        return ("Simulated sending: no messaging provider is connected, so sends are recorded as "
                "simulated and no guest is actually texted.")

    # ----- shared reads -----------------------------------------------------------------------------------------

    async def _lapsed(self, conn: AsyncConnection, tenant_id: str,
                      ) -> list[tuple[cu.CustomerHistory, int, dict[str, str]]]:
        """Every lapsed regular's (history, days away, menu names), most-drifted first — the one
        lapsed computation both the view and the draft path stand on."""
        now_biz = await self._customers._now_biz(conn, tenant_id)
        histories = await self._customers._histories(conn, tenant_id)
        names, _prices = await self._customers._menu(conn, tenant_id)
        out = []
        for history in histories.values():
            visits = history.visits
            if len(visits) < cu.REGULAR_MIN_VISITS:
                continue
            days_away = cu.days_between(visits[-1].placed_biz_ms, now_biz, self._tz)
            if days_away >= cu.LAPSED_DAYS:
                out.append((history, days_away, names))
        out.sort(key=lambda row: -row[1])
        return out

    def _sync_returns(self, tenant_id: str,
                      histories: dict[str, cu.CustomerHistory]) -> list[Outreach]:
        """Mark sent outreaches returned when the guest has ordered since the send, then read the
        book back. Matching is on the tenant's business clock (``sent_biz_ms`` vs visit times)."""
        records = self.store.outreach(tenant_id)
        now_real = self._c.clock.now_real_ms()
        changed = False
        for record in records:
            if record.status != "sent" or record.sent_biz_ms is None:
                continue
            history = histories.get(record.phone_key)
            if history is None:
                continue
            returned = next((order for order in history.visits
                             if order.placed_biz_ms > record.sent_biz_ms), None)
            if returned is not None:
                self.store.mark_outreach_returned(tenant_id, record.outreach_id, returned.id, now_real)
                changed = True
        return self.store.outreach(tenant_id) if changed else records

    def _outreach_view(self, record: Outreach, names: dict[str, str],
                       records_by_key: dict[str, object]) -> WinbackOutreachView:
        record_guest = records_by_key.get(record.phone_key)
        return WinbackOutreachView(
            outreach_id=record.outreach_id, key=record.case_key,
            name=getattr(record_guest, "name", None), phone_masked=mask_phone(record.phone_key),
            message_text=record.message_text, offer_item_code=record.offer_item_code,
            offer_name=names.get(record.offer_item_code, record.offer_item_code)
            if record.offer_item_code else None,
            status=record.status,  # type: ignore[arg-type]
            source=record.source,  # type: ignore[arg-type]
            channel=record.channel, simulated=record.simulated,
            sent_biz=biz_from_ms(record.sent_biz_ms, self._tz) if record.sent_biz_ms is not None else None,
            returned_order_id=record.returned_order_id)

    # ----- the view ---------------------------------------------------------------------------------------------

    async def winback_view(self, tenant_id: str) -> WinbackView:
        customers = self._customers
        async with customers._store().read() as conn:
            histories = await customers._histories(conn, tenant_id)
            names, prices = await customers._menu(conn, tenant_id)
            lapsed = await self._lapsed(conn, tenant_id)
        records = self._sync_returns(tenant_id, histories)
        book = {record.phone_key: record for record in self.store.customers(tenant_id)}
        credits = {credit.phone_key: credit for credit in self.store.credits(tenant_id)
                   if credit.redeemed_order_id is None}
        latest: dict[str, Outreach] = {}
        for record in records:  # newest first: the first record per guest is their current case
            latest.setdefault(record.phone_key, record)

        contactable: list[WinbackGuest] = []
        not_contactable = 0
        for history, days_away, _names in lapsed:
            record = book.get(history.phone_key)
            if record is None or not record.marketing_opt_in:
                not_contactable += 1
                continue
            usual = cu.usual_items(history, limit=1)
            current = latest.get(history.phone_key)
            status = "none"
            outreach_id = None
            if current is not None and current.case_key == case_key(history):
                status, outreach_id = current.status, current.outreach_id
            elif current is not None and current.status == "draft":
                status, outreach_id = "draft", current.outreach_id
            contactable.append(WinbackGuest(
                key=case_key(history), name=history.name, phone_masked=mask_phone(history.phone_key),
                visits=len(history.visits),
                last_visit_biz=biz_from_ms(history.visits[-1].placed_biz_ms, self._tz),
                days_since_last=days_away,
                usual_item_code=usual[0][0] if usual else None,
                usual_name=names.get(usual[0][0], usual[0][0]) if usual else None,
                status=status, outreach_id=outreach_id,  # type: ignore[arg-type]
                pending_credit=customers._credit_view(credits.get(history.phone_key), names, prices)))
        return WinbackView(
            contactable=contactable[:WINBACK_SHOWN], not_contactable_count=not_contactable,
            outreach=[self._outreach_view(record, names, book) for record in records],
            provider_note=self._provider_note(), note=WINBACK_NOTE)

    # ----- drafting ---------------------------------------------------------------------------------------------

    async def _draft_text(self, tenant_id: str, history: cu.CustomerHistory, days_away: int,
                          names: dict[str, str]) -> tuple[str, str]:
        """(text, source): the model's draft in live mode, the template everywhere else — and the
        template again if the model call fails, because a draft must never block on a model."""
        usual = cu.usual_items(history, limit=1)
        usual_name = names.get(usual[0][0], usual[0][0]) if usual else None
        async with self._customers._store().read() as conn:
            tenant = await self._customers._repo().tenant(conn, tenant_id)
        fallback = template_message(history.name, tenant.name, usual_name, days_away)
        if self._c.settings.ai_mode is not AIMode.LIVE:
            return fallback, "template"
        try:
            # Pass 17: the model sees the usual dish's egress code, never its name — dish names are
            # the kitchen's secret, like its recipes. The guest's first name and the restaurant's
            # name stay: they are the message's recipient and sender, not menu data. The draft is
            # translated back before it is stored, so the guest reads the dish's real name. Any
            # failure here (mapping included) falls back to the template, like a failed model call.
            repo = self._customers._repo()
            async with self._customers._store().write() as tx:
                ingredients = await repo.ingredients(tx.conn, tenant_id)
                snapshot = await repo.menu_snapshot(tx.conn, tenant_id)
                codes = await repo.ensure_egress_codes(tx.conn, tenant_id, ingredients, snapshot.items)
            egress = EgressMap(codes, ingredients, snapshot.items)
            usual_code = egress.code_for("item", usual[0][0]) if usual else None
            ctx = CallCtx(tenant_id=tenant_id, session_id="winback", trace_id=f"winback:{case_key(history)}",
                          data_class="customer", spend_scope=tenant_id)
            text, _record = await self._c.meta.chat_text(
                purpose=Purpose.GM, ctx=ctx, messages=[
                    {"role": "system", "content":
                     "You write one short text message from a restaurant to a regular guest who has "
                     "not visited in a while, inviting them back. Warm, plain, under 60 words, no "
                     "discount codes, no pressure. The offer is exactly: their usual dish on the house "
                     "on their next visit. Output only the message text."},
                    {"role": "user", "content":
                     f"Restaurant: {tenant.name}. Guest first name: {history.name or 'a regular'}. "
                     f"Days since their last visit: {days_away}. Their usual dish: "
                     f"{usual_code or 'their favourite'}."}])
            text = egress.detokenize(text).strip()[:1000]
            return (text, "model") if text else (fallback, "template")
        except Exception:
            log.warning("win-back model draft failed; using the template", exc_info=True)
            return fallback, "template"

    async def draft(self, tenant_id: str, keys: list[str], decided_by: str | None) -> WinbackDraftResult:
        """Draft one message per selected guest. Any selected guest who is not a contactable lapsed
        regular — above all, one who never opted in — refuses the whole request: drafting for a
        guest who cannot be contacted is how accidental sends are born."""
        customers = self._customers
        async with customers._store().read() as conn:
            lapsed = await self._lapsed(conn, tenant_id)
            names, _prices = await customers._menu(conn, tenant_id)
        by_key = {case_key(history): (history, days) for history, days, _n in lapsed}
        book = {record.phone_key: record for record in self.store.customers(tenant_id)}
        chosen: list[tuple[cu.CustomerHistory, int]] = []
        for key in keys:
            found = by_key.get(key)
            if found is None:
                raise BadRequest("One of the selected guests is not a lapsed regular right now",
                                 code="not_lapsed")
            history, days = found
            record = book.get(history.phone_key)
            if record is None or not record.marketing_opt_in:
                raise BadRequest("That guest hasn't opted in to messages, so they can't be contacted",
                                 code="not_opted_in")
            chosen.append((history, days))

        records = self.store.outreach(tenant_id)
        latest: dict[str, Outreach] = {}
        for record in records:
            latest.setdefault(record.phone_key, record)
        drafts: list[WinbackOutreachView] = []
        skipped: list[WinbackSkipped] = []
        now_real = self._c.clock.now_real_ms()
        for history, days in chosen:
            key = case_key(history)
            current = latest.get(history.phone_key)
            if current is not None and current.status == "draft":
                drafts.append(self._outreach_view(current, names, book))  # already drafted: hand it back
                continue
            if current is not None and current.status in ("sent", "returned"):
                skipped.append(WinbackSkipped(
                    key=key, reason="Already contacted for this drift" if current.status == "sent"
                    else "Already back since the last message"))
                continue
            text, source = await self._draft_text(tenant_id, history, days, names)
            usual = cu.usual_items(history, limit=1)
            record = self.store.create_outreach(
                tenant_id, history.phone_key, key, text, usual[0][0] if usual else None, source,
                decided_by, now_real)
            drafts.append(self._outreach_view(record, names, book))
        return WinbackDraftResult(drafts=drafts, skipped=skipped)

    # ----- editing ------------------------------------------------------------------------------------------------

    async def edit_draft(self, tenant_id: str, outreach_id: str,
                         message_text: str) -> WinbackOutreachView:
        record = self.store.outreach_by_id(tenant_id, outreach_id)
        if record is None:
            raise NotFound(f"unknown win-back draft {outreach_id!r}")
        if record.status != "draft":
            raise BadRequest("That message has already been sent — a sent message is a record",
                             code="already_sent")
        self.store.update_outreach_text(tenant_id, outreach_id, message_text)
        updated = self.store.outreach_by_id(tenant_id, outreach_id)
        assert updated is not None
        async with self._customers._store().read() as conn:
            names, _prices = await self._customers._menu(conn, tenant_id)
        book = {r.phone_key: r for r in self.store.customers(tenant_id)}
        return self._outreach_view(updated, names, book)

    # ----- approve & send -----------------------------------------------------------------------------------------

    async def send(self, tenant_id: str, outreach_ids: list[str],
                   approved_by: str | None) -> WinbackSendResult:
        """Approve and send the named drafts. Everything is validated before anything is sent:
        each id must be a standing draft, each guest's opt-in is re-checked now (a guest who opted
        out after drafting is refused, and nothing goes), and the channel must be able to send —
        in live mode with no provider, the refusal is the plain provider note. A successful send
        mints (or links) the promised usual-on-the-house credit, then records the send."""
        records: list[Outreach] = []
        for outreach_id in outreach_ids:
            record = self.store.outreach_by_id(tenant_id, outreach_id)
            if record is None:
                raise NotFound(f"unknown win-back draft {outreach_id!r}")
            if record.status != "draft":
                raise BadRequest("That message has already been sent", code="already_sent")
            records.append(record)
        book = {record.phone_key: record for record in self.store.customers(tenant_id)}
        for record in records:
            guest = book.get(record.phone_key)
            if guest is None or not guest.marketing_opt_in:
                raise BadRequest("A selected guest hasn't opted in to messages, so nothing was sent",
                                 code="not_opted_in")
        messaging = self._messaging()
        if isinstance(messaging, UnavailableMessaging):
            raise BadRequest(self._provider_note(), code="no_provider")

        customers = self._customers
        async with customers._store().read() as conn:
            now_biz = await customers._now_biz(conn, tenant_id)
            names, _prices = await customers._menu(conn, tenant_id)
        now_real = self._c.clock.now_real_ms()
        sent: list[WinbackOutreachView] = []
        for record in records:
            outcome = await messaging.send_text(tenant_id=tenant_id, phone_key=record.phone_key,
                                                text=record.message_text)
            if outcome.status != "sent":
                raise BadRequest(outcome.detail, code="no_provider")
            credit_id: str | None = None
            if record.offer_item_code:
                waiting = self.store.pending_credit(tenant_id, record.phone_key)
                if waiting is not None:
                    credit_id = waiting.credit_id  # a dish already waits: the send links to it
                else:
                    credit = self.store.mint_credit(tenant_id, record.phone_key,
                                                    record.offer_item_code, record.case_key,
                                                    approved_by, now_real)
                    credit_id = credit.credit_id
            self.store.mark_outreach_sent(tenant_id, record.outreach_id, channel="sms",
                                          simulated=outcome.simulated, approved_by=approved_by,
                                          credit_id=credit_id, now_ms=now_real, now_biz_ms=now_biz)
            updated = self.store.outreach_by_id(tenant_id, record.outreach_id)
            assert updated is not None
            sent.append(self._outreach_view(updated, names, book))
        return WinbackSendResult(sent=sent)
