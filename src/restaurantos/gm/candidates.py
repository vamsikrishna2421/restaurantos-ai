"""Candidate actions (ARCHITECTURE §6.2, CONTRACTS §6 and §8.1): at most 16, ingredient-level, every number by code.

The model only picks candidate ids. Code computes everything a candidate carries: params, tier, cost, the English
``effect`` sentence and ``effect_i18n``, its KDS action line in en/te/ta/es rendered from deterministic tables (menu
and station names, kitchen labels and the few ``WORDS`` below). For the incident's ingredient:

- **86**: ``set_item_availability(dish, false)`` for each dish using it that still sells (T2);
- **alternatives**: one ``suggest_alternative`` per category group (same category and veg flag, at most 3 dishes)
  with the top 2 of ``availability.group_alternatives`` (price closeness, then servings free); every dish using the
  ingredient is excluded (T1);
- **bridge / to-close PO**: for each viable same-day supplier, the packs covering the current burn from arrival to
  21:00 / close × 1.2, net of the free stock projected at arrival and of stock already on order (``gm.incoming``)
  landing by then, raised to the supplier's minimum order. When both need the same packs they merge into one
  to-close candidate;
- **next-day PO**: the cheapest ``next_day`` supplier, restocking to par what is projected to be left at close, net of
  stock on order landing after it;
- **notify**: each on-shift cook at an affected station, plus on-shift FOH when a same-day pickup PO is offered (T1);
- **quote**: ``adjust_quote_time(station, +5)`` for an affected station whose queue is over 12 min (T1);
- **callback**: ``flag_ticket_for_callback`` for each order a shelf count left uncovered (T1);
- **observe re-plans** (P1-2, ``gm.observe``): a notify **re-routing** each unseen notice to a peer at its station,
  else a manager, in place of its own (T1; every later re-plan keeps the move); POs recomputed without a **blocked**
  pickup's supplier; an **un-86** for each dish this incident 86'd once stock was received (T2).

POs are T2 up to ``manager_po_limit_cents`` and T3 above it or from a new supplier; one that would pass the daily cap
is never offered (a forbidden action cannot be expressed). A notify's ``message`` is the action line built from the
``effect_i18n`` of the selected candidates that concern the recipient: candidates carry the playbook's line, and a
plan that selects others re-renders it with ``notify_params``. Supplier notes are never read here.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from zoneinfo import ZoneInfo

from restaurantos.contracts.common import DEFAULT_TZ, Band, Model, SpeakLang, Tier, biz_from_ms
from restaurantos.contracts.gm import (
    CANDIDATE_IDS,
    MAX_CANDIDATES,
    STAFF_MESSAGE_MAX_CHARS,
    ActionType,
    AdjustQuoteTimeParams,
    Candidate,
    CandidateInputs,
    CreatePurchaseOrderParams,
    FlagTicketForCallbackParams,
    NotifyStaffParams,
    PolicyLimits,
    ReportStaff,
    SetItemAvailabilityParams,
    SuggestAlternativeParams,
)
from restaurantos.contracts.inventory import Supplier, SupplierItem
from restaurantos.contracts.kitchen import LocalizedLabel, Staff
from restaurantos.contracts.orders import Order
from restaurantos.domain import availability, detectors
from restaurantos.domain.i18n_kitchen import item_name, label, station_name
from restaurantos.gm import incoming as on_order
from restaurantos.gm import observe as obs
from restaurantos.gm.fmt import hhmm, local_ms, quantity, short_name, speak_lang, usd

LANGS: tuple[SpeakLang, ...] = ("en", "te", "ta", "es")
LANG_NAMES = {"en": "English", "te": "Telugu", "ta": "Tamil", "es": "Spanish"}
BRIDGE_END_LOCAL = "21:00"  # CONTRACTS_SEED §11: bridge = demand from arrival to 21:00 × 1.2
CLOSE_LOCAL = "22:00"  # tenant.yaml close_local; situation passes the tenant's own
SAFETY = Fraction(12, 10)  # PO quantities cover demand × 1.2
QUOTE_QUEUE_OVER_MIN = 12
QUOTE_OFFSET_MIN = 5
QUOTE_DURATION_MIN = 60
GROUP_MAX = 3  # suggest_alternative.for_item_codes holds 1-3 dishes
CALLBACKS_FIRST = 4  # this many callbacks outrank everything when the 16 cap bites; later ones rank last
KIND_ORDER = ("86", "un86", "alt", "po", "notify", "quote", "callback")  # output order: C01 = the first 86
LINE_ORDER = ("set_item_availability", "create_purchase_order", "adjust_quote_time", "flag_ticket_for_callback",
              "suggest_alternative")  # action-line order: the tail is what a line over 280 characters loses
_MIN = 60_000

# The few words the action lines need beyond the kitchen labels. UNREVIEWED drafts, like config/i18n_kitchen.yaml:
# no fluent Telugu, Tamil or Spanish speaker has checked them yet (CONTRACTS_SEED, "Fluency review").
WORDS: dict[str, dict[str, str]] = {
    "low": {"en": "running low", "te": "తక్కువగా ఉంది", "ta": "குறைவாக உள்ளது", "es": "se está acabando"},
    "pickup": {"en": "pickup", "te": "పికప్", "ta": "பிக்கப்", "es": "recoger"},
    "delivery": {"en": "delivery", "te": "డెలివరీ", "ta": "டெலிவரி", "es": "entrega"},
    "tomorrow": {"en": "tomorrow", "te": "రేపు", "ta": "நாளை", "es": "mañana"},
    "callback": {"en": "call the guest back", "te": "అతిథికి మళ్లీ కాల్ చేయండి",
                 "ta": "விருந்தினரை மீண்டும் அழைக்கவும்", "es": "llamar al cliente"},
    "received": {"en": "received", "te": "వచ్చింది", "ta": "வந்துவிட்டது", "es": "recibido"},
    "back_on": {"en": "BACK ON", "te": "మళ్లీ అందుబాటులో ఉంది", "ta": "மீண்டும் கிடைக்கிறது", "es": "DISPONIBLE"},
}


# ----- generation (the formatting lives in gm.fmt: integers in, text out; never a float) -------------------------

@dataclass
class _Ctx:
    inputs: CandidateInputs
    servings_free: dict[str, int]
    tz: str
    close_ms: int
    incoming: tuple[on_order.Incoming, ...] = ()  # open POs of the ingredient: stock already on its way
    station_of: dict[str, str] = field(init=False)  # item code -> station id
    suppliers: dict[str, Supplier] = field(init=False)

    def __post_init__(self) -> None:
        self.station_of = {item.code: item.station_id for item in self.inputs.menu.items}
        self.suppliers = {supplier.id: supplier for supplier in self.inputs.suppliers}

    @property
    def now(self) -> int:
        return self.inputs.now_biz_ms

    def ing_name(self, lang: str) -> str:
        """The ingredient's name for a KDS line, without an explanatory parenthetical ("paneer (queso fresco)")."""
        names = self.inputs.ingredient.names
        return (names.get(lang) or names["en"]).split(" (")[0]  # type: ignore[call-overload]

    def name(self, code: str, lang: str = "en") -> str:
        return item_name(self.inputs.menu, code, lang)

    def affected_stations(self) -> list[str]:
        """Stations of the dishes using the ingredient, in KDS order."""
        used = {self.station_of[d.code] for d in self.inputs.dishes if d.code in self.station_of}
        return [station.id for station in self.inputs.menu.stations if station.id in used]

    def same_day(self, eta: datetime) -> bool:
        return eta.astimezone(ZoneInfo(self.tz)).date() == biz_from_ms(self.now, self.tz).date()


@dataclass(frozen=True)
class _Slot:
    kind: str  # one of KIND_ORDER
    rank: tuple[int, int, int]  # lower survives the 16 cap first
    candidate: Candidate  # a notify's is a draft: its line is rendered once the playbook's choice is known
    person: Staff | None = None  # notify: the recipient
    missed: tuple[obs.Unseen, ...] = ()  # notify: the unseen notices ``person`` carries (re-routed, or their own kept)


def _labels(texts: Mapping[str, str]) -> LocalizedLabel:
    return LocalizedLabel(**{lang: texts[lang] for lang in LANGS})


def _candidate(action_type: ActionType, target: str, params: Model, tier: Tier, effect: str,
               texts: Mapping[str, str], *, cost: int | None = None, deadline: datetime | None = None) -> Candidate:
    # The id is provisional: generate() numbers the survivors C01... in output order.
    return Candidate(id="C01", action_type=action_type, target=target, params=params, tier=tier, cost_cents=cost,
                     effect=effect, effect_i18n=_labels(texts), deadline_biz=deadline)


def generate(inputs: CandidateInputs, *, servings_free: Mapping[str, int] | None = None,
             at_risk: Sequence[Order] = (), new_supplier_ids: Collection[str] = (), po_spent_today_cents: int = 0,
             close_local: str = CLOSE_LOCAL, tz: str = DEFAULT_TZ,
             incoming: Sequence[on_order.Incoming] = (), observed: obs.Observed = obs.NOTHING) -> list[Candidate]:
    """The incident's candidates: at most 16, numbered ``C01``… in output order (86s, un-86s, alternatives, POs,
    notify, quotes, callbacks).

    ``servings_free`` (item code → servings the stock allows) filters and ranks alternatives and gates un-86s;
    ``at_risk`` are the open orders a count left uncovered (callback flags); ``new_supplier_ids`` (``PolicyContext``)
    make their POs T3; ``po_spent_today_cents`` drops a PO that would pass ``po_daily_cap_cents``; ``incoming`` (open
    POs) is stock the PO quantities are net of; ``observed`` holds the incident's observe facts (``gm.observe``).
    When more than 16 qualify, POs, re-routes, un-86s, notices and the first callbacks are kept first, then 86s by
    grams per hour, alternatives, quotes, other callbacks. A notice's line follows the playbook's whole choice."""
    ctx = _Ctx(inputs, dict(servings_free or {}), tz, local_ms(inputs.now_biz_ms, close_local, tz), tuple(incoming))
    back = obs.un86(inputs.observe, observed, inputs.dishes, inputs.forecast.free, ctx.servings_free)
    slots = [*_eighty_six(ctx), *_un86(ctx, back), *_alternatives(ctx),
             *_purchase_orders(ctx, new_supplier_ids, po_spent_today_cents, obs.excluded(inputs.observe)),
             *_quotes(ctx), *_callbacks(ctx, at_risk)]
    pickup = any(s.kind == "po" and _pickup_today(ctx, s.candidate) for s in slots)
    slots += _notify_slots(ctx, pickup, *obs.reroutes(inputs.observe, observed, inputs.staff_on_shift))
    kept = set(sorted(range(len(slots)), key=lambda i: slots[i].rank)[:MAX_CANDIDATES])
    ordered = sorted((slots[i] for i in sorted(kept)), key=lambda s: KIND_ORDER.index(s.kind))
    chosen = [c for c in playbook([s.candidate for s in ordered], band=inputs.forecast.band,
                                  now_biz=biz_from_ms(inputs.now_biz_ms, tz), staff=inputs.staff_on_shift,
                                  max_steps=inputs.policy.max_steps_per_plan) if c.action_type != "notify_staff"]
    return [(s.candidate if s.person is None else _notify(ctx, s.person, chosen, s.missed)).model_copy(
        update={"id": CANDIDATE_IDS[idx]}) for idx, s in enumerate(ordered)]


def _eighty_six(ctx: _Ctx) -> list[_Slot]:
    menu, unit, fc = ctx.inputs.menu, ctx.inputs.ingredient.unit, ctx.inputs.forecast
    out = []
    for idx, dish in enumerate(ctx.inputs.dishes):
        if not dish.available:
            continue
        texts = {lang: f"{ctx.name(dish.code, lang)}: {label(menu, 'eighty_six', lang)}" for lang in LANGS}
        effect = f"Stops new orders of {ctx.name(dish.code)} ({quantity(dish.qty_per_serving, unit)}/serving)"
        candidate = _candidate("set_item_availability", dish.code,
                               SetItemAvailabilityParams(item_code=dish.code, available=False), "T2", effect, texts,
                               deadline=fc.eta_biz)
        out.append(_Slot("86", (1, -dish.forecast_next_60m * dish.qty_per_serving, idx), candidate))
    return out


def _un86(ctx: _Ctx, codes: Sequence[str]) -> list[_Slot]:
    """Observe ``po_received``: each dish this incident 86'd back on sale (``obs.un86`` picks them; T2)."""
    unit, free, dishes = ctx.inputs.ingredient.unit, ctx.inputs.forecast.free, ctx.inputs.dishes
    largest = max((dish.qty_per_serving for dish in dishes), default=0)
    serving = {dish.code: dish.qty_per_serving for dish in dishes}
    out = []
    for idx, code in enumerate(codes):
        texts = {lang: f"{ctx.name(code, lang)}: {WORDS['back_on'][lang]}" for lang in LANGS}
        effect = (f"Restarts orders of {ctx.name(code)} ({quantity(serving[code], unit)}/serving): stock was received "
                  f"and {quantity(free, unit)} is free, at least {detectors.DEFAULT_POLICY.min_servings} × the largest "
                  f"serving ({quantity(largest, unit)})")
        params = SetItemAvailabilityParams(item_code=code, available=True)
        out.append(_Slot("un86", (0, 0, idx), _candidate("set_item_availability", code, params, "T2", effect, texts)))
    return out


def _alternatives(ctx: _Ctx) -> list[_Slot]:
    items = {item.code: item for item in ctx.inputs.menu.items}
    using = [d.code for d in ctx.inputs.dishes]
    exclude = {*using, *ctx.inputs.availability.unavailable}
    groups: dict[tuple[str, bool], list[str]] = {}
    for code in using:
        if code in items:
            groups.setdefault((items[code].category, items[code].is_veg), []).append(code)
    out: list[_Slot] = []
    for codes in groups.values():
        for start in range(0, len(codes), GROUP_MAX):
            group = codes[start:start + GROUP_MAX]
            alts = availability.group_alternatives(group, ctx.inputs.menu, ctx.servings_free, exclude)
            if not alts:
                continue
            texts = {lang: " / ".join(ctx.name(c, lang) for c in group) + " → "
                     + " / ".join(ctx.name(c, lang) for c in alts) for lang in LANGS}
            effect = (f"Offers {' or '.join(ctx.name(c) for c in alts)} when guests ask for "
                      f"{' or '.join(ctx.name(c) for c in group)}")
            params = SuggestAlternativeParams(for_item_codes=group, alternative_codes=alts)
            out.append(_Slot("alt", (3, 0, len(out)),
                             _candidate("suggest_alternative", ",".join(group), params, "T1", effect, texts)))
    return out


def _catalog_item(supplier: Supplier, ingredient_id: str) -> SupplierItem | None:
    return next((item for item in supplier.catalog if item.ingredient_id == ingredient_id), None)


def _packs(ctx: _Ctx, need: Fraction, supplier: Supplier, item: SupplierItem) -> tuple[int, bool] | None:
    """Whole packs covering ``need`` and whether they were raised to the supplier's minimum order. None when that
    minimum would buy more than ``po_qty_max_multiple_of_gap`` × the need: no sensible PO, so none is offered."""
    packs = max(1, math.ceil(need / item.pack_qty))
    floor = math.ceil(Fraction(supplier.min_order_cents, item.pack_price_cents)) if item.pack_price_cents else 0
    if floor <= packs:
        return packs, False
    return (floor, True) if floor * item.pack_qty <= ctx.inputs.policy.po_qty_max_multiple_of_gap * need else None


def _purchase_orders(ctx: _Ctx, new_supplier_ids: Collection[str], spent: int,
                     blocked: Collection[str] = ()) -> list[_Slot]:
    """Same-day and next-day POs from every supplier carrying the ingredient but ``blocked`` (a pickup FOH could not
    make: the supplier switch of an observe re-plan)."""
    ing, fc, now, lots = ctx.inputs.ingredient, ctx.inputs.forecast, ctx.now, ctx.incoming
    free, burn = Fraction(fc.free), Fraction(max(0, fc.burn_per_h))
    ends = {"bridge": min(local_ms(now, BRIDGE_END_LOCAL, ctx.tz), ctx.close_ms), "close": ctx.close_ms}
    suppliers = [supplier for supplier in ctx.inputs.suppliers if supplier.id not in blocked]
    out: list[_Slot] = []
    for supplier in suppliers:
        item = _catalog_item(supplier, ing.id)
        option = detectors.viable(supplier, ing.id, now, tz=ctx.tz) if item is not None else None
        if item is None or option is None:
            continue
        arrives = option.arrives_biz_ms
        projected = on_order.free_at(lots, free, burn, now, arrives)  # stock on order landing first counts
        packs: dict[str, tuple[int, bool]] = {}
        for kind, end_ms in ends.items():
            span_min = Fraction(end_ms - arrives, _MIN)
            need = burn * span_min / 60 * SAFETY - projected - on_order.landing(lots, arrives, end_ms)
            if span_min > 0 and need > 0 and (found := _packs(ctx, need, supplier, item)) is not None:
                packs[kind] = found
        if "bridge" in packs and packs["bridge"][0] == packs.get("close", (0, False))[0]:
            del packs["bridge"]  # the same order covers both: offer it once, as the to-close quantity
        for kind, (n, raised) in packs.items():
            cover = f"to {BRIDGE_END_LOCAL}" if kind == "bridge" else f"to close ({hhmm(ctx.close_ms, ctx.tz)})"
            why = (f"covers demand {cover} × 1.2" + (f", net of {quantity(math.floor(projected), ing.unit)} "
                                                     "projected free at arrival" if projected > 0 else "")
                   + _held(ctx, on_order.landing(lots, None, ends[kind])))
            out += _po_slot(ctx, supplier, item, n, raised, arrives, option.cutoff_biz_ms, why, new_supplier_ids,
                            spent, len(out))
    options = [(Fraction(item.pack_price_cents, item.pack_qty), supplier.lead_min, supplier.id)
               for supplier in suppliers
               if supplier.next_day_local and (item := _catalog_item(supplier, ing.id)) is not None]
    if options:
        supplier = ctx.suppliers[min(options)[2]]
        item = _catalog_item(supplier, ing.id)
        left = on_order.free_at(lots, free, burn, now, ctx.close_ms)
        gap = ing.par - left - on_order.landing(lots, ctx.close_ms, None)  # what lands after close counts too
        found = _packs(ctx, gap, supplier, item) if item is not None and gap > 0 else None
        if item is not None and supplier.next_day_local and found is not None:
            eta = local_ms(now, supplier.next_day_local, ctx.tz, days=1)
            why = (f"restocks to par ({quantity(ing.par, ing.unit)}) from "
                   f"{quantity(math.floor(left), ing.unit)} left at close" + _held(ctx, sum(lot.qty for lot in lots)))
            out += _po_slot(ctx, supplier, item, *found, eta, None, why, new_supplier_ids, spent, len(out))
    return out


def _held(ctx: _Ctx, qty: int) -> str:
    return f", counting {quantity(qty, ctx.inputs.ingredient.unit)} already on order" if qty > 0 else ""


def _po_slot(ctx: _Ctx, supplier: Supplier, item: SupplierItem, packs: int, raised: bool, eta_ms: int,
             deadline_ms: int | None, why: str, new_supplier_ids: Collection[str], spent: int,
             idx: int) -> list[_Slot]:
    ing, policy, tz = ctx.inputs.ingredient, ctx.inputs.policy, ctx.tz
    cost, qty = packs * item.pack_price_cents, packs * item.pack_qty
    if spent + cost > policy.po_daily_cap_cents:
        return []  # FORBIDDEN past the daily cap: no candidate, so it cannot be expressed
    tier: Tier = "T3" if cost > policy.manager_po_limit_cents or supplier.id in new_supplier_ids else "T2"
    eta = biz_from_ms(eta_ms, tz)
    amount, short, at = quantity(qty, ing.unit), short_name(supplier), hhmm(eta_ms, tz)
    if ctx.same_day(eta):
        how = "staff pickup lands" if supplier.kind == "pickup" else "delivery lands"
        texts = {lang: f"+{amount} {ctx.ing_name(lang)} ≈{at} · {short} ({WORDS[supplier.kind][lang]})"
                 for lang in LANGS}
        when = f"{how} ≈{at}"
    else:
        texts = {lang: f"+{amount} {ctx.ing_name(lang)} · {short} · {WORDS['tomorrow'][lang]} {at}" for lang in LANGS}
        when = f"delivered tomorrow {at}"
    effect = (f"Orders {amount} {ctx.ing_name('en')} ({packs} × {quantity(item.pack_qty, ing.unit)}) from {short} "
              f"for {usd(cost)}: {when}; {why}")
    if raised:
        effect += f"; raised to the {usd(supplier.min_order_cents)} minimum order"
    params = CreatePurchaseOrderParams(supplier_id=supplier.id, ingredient_id=ing.id, packs=packs, qty=qty,
                                       cost_cents=cost, eta_biz=eta)
    deadline = None if deadline_ms is None else biz_from_ms(deadline_ms, tz)
    return [_Slot("po", (0, 0, idx), _candidate("create_purchase_order", supplier.id, params, tier, effect, texts,
                                                cost=cost, deadline=deadline))]


def po_params(candidate: Candidate) -> CreatePurchaseOrderParams:
    """The typed params of a ``create_purchase_order`` candidate or step."""
    params = candidate.params_model()
    if not isinstance(params, CreatePurchaseOrderParams):
        raise ValueError(f"{candidate.id} is {candidate.action_type}, not create_purchase_order")
    return params


def _pickup_today(ctx: _Ctx, candidate: Candidate) -> bool:
    params = po_params(candidate)
    supplier = ctx.suppliers.get(params.supplier_id)
    return supplier is not None and supplier.kind == "pickup" and ctx.same_day(params.eta_biz)


def _quotes(ctx: _Ctx) -> list[_Slot]:
    menu, policy = ctx.inputs.menu, ctx.inputs.policy
    loads = {load.station_id: load for load in ctx.inputs.kitchen}
    out: list[_Slot] = []
    for station_id in ctx.affected_stations():
        load = loads.get(station_id)
        if load is None or load.queue_min <= QUOTE_QUEUE_OVER_MIN:
            continue
        params = AdjustQuoteTimeParams(station_id=station_id, minutes=QUOTE_OFFSET_MIN, duration_min=QUOTE_DURATION_MIN)
        tier: Tier = "T1" if QUOTE_OFFSET_MIN <= policy.quote_offset_auto_max_min else "T2"
        texts = {lang: f"{label(menu, 'quote', lang)} +{QUOTE_OFFSET_MIN} {label(menu, 'minutes', lang)} · "
                       f"{station_name(menu, station_id, lang)}" for lang in LANGS}
        effect = (f"Adds {QUOTE_OFFSET_MIN} min to new {station_name(menu, station_id, 'en')} pickup quotes for "
                  f"{QUOTE_DURATION_MIN} min (queue {load.queue_min} min)")
        out.append(_Slot("quote", (4, 0, len(out)), _candidate("adjust_quote_time", station_id, params, tier,
                                                                effect, texts)))
    return out


def _callbacks(ctx: _Ctx, at_risk: Sequence[Order]) -> list[_Slot]:
    ing = ctx.ing_name("en")
    out: list[_Slot] = []
    for idx, order in enumerate(at_risk):
        reason = f"{ing} short after a count: order #{order.order_no} may not be made"[:120]
        texts = {lang: f"#{order.order_no}: {WORDS['callback'][lang]}" for lang in LANGS}
        effect = (f"Asks staff to call back the guest of order #{order.order_no} ({order.channel}): its {ing} "
                  "may run short")
        rank = (0, 1, idx) if idx < CALLBACKS_FIRST else (5, 0, idx)
        out.append(_Slot("callback", rank, _candidate("flag_ticket_for_callback", order.id,
                                                      FlagTicketForCallbackParams(order_id=order.id, reason=reason),
                                                      "T1", effect, texts)))
    return out


def _notify_slots(ctx: _Ctx, pickup: bool, routes: Mapping[str, tuple[obs.Unseen, ...]],
                  away: Collection[str]) -> list[_Slot]:
    """Cooks at an affected station, FOH when a same-day pickup PO is offered, then whoever carries an unseen notice
    (``routes``, kept in the 16); none for whoever missed a notice that moved (``away``): the re-plan retracts it."""
    affected = set(ctx.affected_stations())
    staff = [p for p in ctx.inputs.staff_on_shift if p.on_shift and p.id not in away]
    people = [p for p in staff if p.role == "cook" and affected & set(p.station_ids)]
    people += [p for p in staff if p.role == "foh"] if pickup else []
    listed = {p.id for p in people}
    people += [p for p in staff if p.id in routes and p.id not in listed]
    return [_Slot("notify", (0, 0 if p.id in routes else 2, idx), _notify(ctx, p, (), routes.get(p.id, ())), p,
                  routes.get(p.id, ())) for idx, p in enumerate(people)]


# ----- the playbook's choice and the action lines -----------------------------------------------------------------

def playbook(candidates: Sequence[Candidate], *, band: Band, now_biz: datetime,
             staff: Iterable[Staff | ReportStaff] = (), max_steps: int = PolicyLimits().max_steps_per_plan,
             ) -> list[Candidate]:
    """The playbook (ARCHITECTURE §6.3), in execution order: in ACT every 86, outside it every un-86 (offered only
    once stock was received); every alternatives candidate; the to-close quantity (the largest same-day PO) of the
    fastest same-day supplier, else the next-day PO (a blocked supplier offers none); a notify for each cook in
    ``staff`` and every notify carrying a notice nobody saw, whatever its recipient's role (``observe.carries``:
    dropping one would retract that notice). Past ``max_steps`` the last alternatives go first, then the end, such a
    notice last of all. It sees candidates only, never notes, so in ``dinner_rush`` it orders Patel 4 kg."""
    def of(action: str) -> list[Candidate]:
        return [c for c in candidates if c.action_type == action]

    chosen = [c for c in of("set_item_availability") if c.params["available"] == (band != "act")]
    chosen += of("suggest_alternative")
    orders = [(c, po_params(c)) for c in of("create_purchase_order")]
    today = [(c, p) for c, p in orders if p.eta_biz.astimezone(now_biz.tzinfo).date() == now_biz.date()]
    if today:
        fastest = min(today, key=lambda cp: (cp[1].eta_biz, cp[1].supplier_id))[1].supplier_id
        chosen.append(max((cp for cp in today if cp[1].supplier_id == fastest), key=lambda cp: cp[1].qty)[0])
    elif orders:
        chosen.append(min(orders, key=lambda cp: (cp[1].eta_biz, cp[1].cost_cents))[0])
    cooks = {person.id for person in staff if person.role == "cook"}
    chosen += [c for c in of("notify_staff") if c.target in cooks or obs.carries(c)]
    first_out = sorted(range(len(chosen)), key=lambda i: (obs.carries(chosen[i]),
                                                          chosen[i].action_type != "suggest_alternative", -i))
    gone = set(first_out[:max(0, len(chosen) - max_steps)])
    return [c for i, c in enumerate(chosen) if i not in gone]


def _stations(ctx: _Ctx, candidate: Candidate) -> set[str] | None:
    """The stations a dish or quote candidate concerns; None means every station."""
    params = candidate.params
    if candidate.action_type == "set_item_availability":
        return {ctx.station_of.get(params["item_code"], "")}
    if candidate.action_type == "suggest_alternative":
        return {ctx.station_of.get(code, "") for code in params["for_item_codes"]}
    station = params.get("station_id")
    return None if station is None else {station}


def _concerns(ctx: _Ctx, candidate: Candidate, person: Staff) -> bool:
    """Whether an action belongs on this person's action line: cooks hear about their stations and stock landing
    today; FOH about every dish change, today's pickups and callbacks; managers and owners about everything."""
    if candidate.action_type == "notify_staff":
        return False
    if person.role in ("manager", "owner"):
        return True
    if candidate.action_type == "create_purchase_order":
        return ctx.same_day(po_params(candidate).eta_biz) and (person.role != "foh" or _pickup_today(ctx, candidate))
    if candidate.action_type == "flag_ticket_for_callback":
        return person.role == "foh"
    stations = _stations(ctx, candidate)
    return person.role == "foh" or stations is None or bool(stations & set(person.station_ids))


def _fit(parts: Sequence[str]) -> str:
    """Parts joined by ". ", dropping trailing actions until the line fits ``STAFF_MESSAGE_MAX_CHARS``."""
    for end in range(len(parts), 0, -1):
        line = ". ".join(parts[:end])
        if len(line) <= STAFF_MESSAGE_MAX_CHARS:
            return line
    return parts[0][:STAFF_MESSAGE_MAX_CHARS - 1] + "…"


def _lines(ctx: _Ctx, person: Staff, selected: Iterable[Candidate]) -> dict[str, str]:
    """``notify_action``: "{stations}: {ingredient} low (≈out). {actions}." per language, most useful actions first
    (86s and un-86s, stock landing, quotes, callbacks, alternatives), so a line over 280 characters loses the least.
    In a re-plan after a delivery the head says the ingredient was received."""
    menu, eta = ctx.inputs.menu, ctx.inputs.forecast.eta_biz
    received = ctx.inputs.observe is not None and ctx.inputs.observe.trigger == "po_received"
    relevant = sorted((c for c in selected if _concerns(ctx, c, person)), key=lambda c: LINE_ORDER.index(c.action_type))
    affected = set(ctx.affected_stations())
    mine = [s for s in person.station_ids if s in affected] if person.role == "cook" else []
    lines = {}
    for lang in LANGS:
        where = " / ".join(station_name(menu, s, lang) for s in mine) or person.name
        state = WORDS["received"][lang] if received else WORDS["low"][lang] + (f" (≈{eta:%H:%M})" if eta else "")
        head = f"{where}: {ctx.ing_name(lang)} {state}"
        lines[lang] = _fit([head, *(getattr(c.effect_i18n, lang) for c in relevant)])
    return lines


def _notify(ctx: _Ctx, person: Staff, selected: Sequence[Candidate], missed: Sequence[obs.Unseen] = ()) -> Candidate:
    lang = speak_lang(person)
    lines = _lines(ctx, person, selected)
    where = ", ".join(station_name(ctx.inputs.menu, s, "en") for s in person.station_ids)
    sends = (f"{person.name} ({person.role}{': ' + where if where else ''}) the code-rendered action line on the KDS "
             f"in {LANG_NAMES[lang]}")
    effect = f"{obs.describe(missed, person.id, ctx.inputs)}: sends {sends}" if missed else f"Sends {sends}"
    params = NotifyStaffParams(staff_id=person.id, language=lang, message=lines[lang], message_en=lines["en"])
    return _candidate("notify_staff", person.id, params, "T1", effect, lines)


def notify_params(inputs: CandidateInputs, notify: Candidate, selected: Iterable[Candidate], *,
                  tz: str = DEFAULT_TZ) -> NotifyStaffParams:
    """The delivered action line of a ``notify_staff`` candidate in a plan that selected ``selected`` (CONTRACTS §6):
    the recipient's header plus the ``effect_i18n`` of each selected candidate that concerns them, in their language
    and in English. A model draft never enters it. Raises LookupError when the recipient is no longer on shift."""
    if notify.action_type != "notify_staff":
        raise ValueError(f"{notify.id} is {notify.action_type}, not notify_staff")
    person = next((p for p in inputs.staff_on_shift if p.id == notify.target and p.on_shift), None)
    if person is None:
        raise LookupError(f"staff {notify.target} is not on shift")
    params = _notify(_Ctx(inputs, {}, tz, local_ms(inputs.now_biz_ms, CLOSE_LOCAL, tz)), person, list(selected))
    return NotifyStaffParams.model_validate(params.params)
