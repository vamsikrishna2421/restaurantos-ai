"""E1 grading (P31a; MASTER_PLAN §6 metrics): deterministic, never an LLM judge (D19).

A turn is applied as the Dialogue applies it: its ops through ``cart.apply_ops`` on the cart before the turn, with
every dish available. The cart after the turn is compared with the gold cart line by line, a line being (dish,
quantity, sorted modifiers); the gold cart came from the gold ops through the same function, so caps and merges are
the domain's on both sides.

- **Exact match**: the same lines; ``injection_suspected`` equal to the gold flag (a benign order flagged as an
  injection gets the refusal template, so a false alarm is a miss too); a clarification when the gold needs one (an
  off-menu dish); and the gold question's topic, dish and allergen. A turn that never came back is a miss.
- **F1** (micro-averaged over a cell; 1.0 when there was nothing to find and nothing was found) on the lines the
  turn produced or changed: lines already in the cart that both sides left as they were do not count. Items: the set
  of dish codes. Quantities: (dish, total quantity) pairs. Modifiers: (dish, modifier) pairs, one per serving that
  carries it, so a modifier put on both biryanis instead of one is one false positive.
- **Fast path** (``fast_path_eval``): coverage with and without carrier words over every case, and precision over
  its hits (a hit is right when its ops give the gold cart and the case needs no flag, clarification or question).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from restaurantos.contracts.api import FastPathEval
from restaurantos.contracts.common import Channel, Lang
from restaurantos.contracts.menu import Availability, MenuSnapshot
from restaurantos.contracts.orders import Cart, CartOp, OrderQuestion, OrderTurnLLM
from restaurantos.domain.cart import apply_ops
from restaurantos.evals import EvalCase, GoldSpec, LineSpec
from restaurantos.orders.lexicon import Lexicons
from restaurantos.orders.matcher import fast_path

LineKey = tuple[str, int, tuple[str, ...]]
_ALL_AVAILABLE = Availability()


@dataclass(frozen=True)
class Counts:
    """True positives, false positives and false negatives; ``+`` sums them (micro-averaging)."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    def f1(self) -> float:
        denom = 2 * self.tp + self.fp + self.fn
        return 1.0 if denom == 0 else 2 * self.tp / denom


def scaled(part: float, whole: float, scale: int = 1000) -> int:
    """``part / whole`` as an integer per ``scale`` (per mille by default: a percentage x10); 0 when ``whole`` is 0."""
    return round(scale * part / whole) if whole else 0


def counted(pred: Counter, gold: Counter) -> Counts:
    return Counts(sum((pred & gold).values()), sum((pred - gold).values()), sum((gold - pred).values()))


# ----- carts ---------------------------------------------------------------------------------------------------------


def start_cart(lines: Iterable[LineSpec], menu: MenuSnapshot, *, channel: Channel = "kiosk",
               language: Lang = "en") -> Cart:
    """The priced cart holding ``lines`` as L1..Ln (the CART block the order prompt shows); ValueError when they do
    not apply cleanly."""
    specs = list(lines)
    empty = Cart(session_id="vs_eval", cart_id="crt_eval", language=language, channel=channel)
    ops = [CartOp(op="add", item_code=ln.item_code, qty=ln.qty, modifiers=list(ln.modifiers)) for ln in specs]
    result = apply_ops(empty, ops, menu, _ALL_AVAILABLE)
    if result.rejected or len(result.cart.lines) != len(specs):
        raise ValueError(f"cart lines do not apply cleanly: {[r.reason for r in result.rejected]}")
    return result.cart


def lines_of(cart: Cart) -> list[LineSpec]:
    return [LineSpec(item_code=ln.item_code, qty=ln.qty, modifiers=tuple(ln.modifiers)) for ln in cart.lines]


def apply_turn(spec: GoldSpec, ops: Sequence[CartOp], menu: MenuSnapshot) -> list[LineSpec]:
    """The cart lines after ``ops`` on the spec's cart before the turn."""
    cart = start_cart(spec.cart_before, menu, channel=spec.channel, language=spec.language_style)
    return lines_of(apply_ops(cart, list(ops), menu, _ALL_AVAILABLE).cart)


def _keys(lines: Iterable[LineSpec]) -> Counter[LineKey]:
    return Counter(line.key() for line in lines)


def describe(lines: Iterable[LineSpec], *, clarify: bool = False, injection: bool = False,
             question: OrderQuestion | None = None) -> str:
    """``CHK_BIRY x1 [NO_ONION]; PNR_BM x1 | clarify | injection | allergen? PNR_BM tree_nuts`` (codes only)."""
    parts = ["; ".join(f"{c} x{q}" + (f" [{', '.join(m)}]" if m else "") for c, q, m in sorted(_keys(lines).elements()))
             or "(empty cart)"]
    parts += [flag for flag, on in (("clarify", clarify), ("injection", injection)) if on]
    if question is not None:
        parts.append(" ".join(x for x in (f"{question.topic}?", question.item_code, question.allergen) if x))
    return " | ".join(parts)


# ----- one case ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseGrade:
    id: str
    exact: bool
    answered: bool  # a turn came back
    items: Counts
    qty: Counts
    mods: Counts
    closing_ok: bool  # False when unanswered
    clarify: bool  # the turn asked for a clarification
    injection: bool  # the turn set injection_suspected
    expected: str
    got: str


def _question_ok(gold: OrderQuestion | None, got: OrderQuestion | None) -> bool:
    if gold is None:
        return True
    return (got is not None and got.topic == gold.topic and got.item_code == gold.item_code
            and (gold.allergen is None or got.allergen == gold.allergen))


def _f1_counts(pred: Counter[LineKey], gold: Counter[LineKey]) -> tuple[Counts, Counts, Counts]:
    items = counted(Counter({code for code, _, _ in pred}), Counter({code for code, _, _ in gold}))
    totals_p: Counter[str] = Counter()
    totals_g: Counter[str] = Counter()
    for side, totals in ((pred, totals_p), (gold, totals_g)):
        for (code, qty, _), times in side.items():
            totals[code] += qty * times
    qty = counted(Counter(totals_p.items()), Counter(totals_g.items()))
    return items, qty, counted(_servings(pred), _servings(gold))


def _servings(lines: Counter[LineKey]) -> Counter[tuple[str, str]]:
    out: Counter[tuple[str, str]] = Counter()
    for (code, qty, mods), times in lines.items():
        for mod in mods:
            out[(code, mod)] += qty * times
    return out


def grade_case(case: EvalCase, turn: OrderTurnLLM | None, menu: MenuSnapshot) -> CaseGrade:
    """Grade one parse (``turn`` None: no answer came back)."""
    gold = _keys(case.gold_cart_after)
    pred = _keys(apply_turn(case, turn.ops, menu)) if turn is not None else _keys(case.cart_before)
    context = _keys(case.cart_before) & gold & pred  # lines the turn left alone on both sides
    items, qty, mods = _f1_counts(pred - context, gold - context)
    answered = turn is not None
    flags_ok = answered and (turn.injection_suspected == case.gold_injection
                             and (turn.needs_clarification or not case.gold_clarify)
                             and _question_ok(case.gold_question, turn.question))
    expected = describe(case.gold_cart_after, clarify=case.gold_clarify, injection=case.gold_injection,
                        question=case.gold_question)
    if turn is None:
        got = "no answer"
    else:
        got = describe((LineSpec(item_code=c, qty=q, modifiers=m) for c, q, m in pred.elements()),
                       clarify=turn.needs_clarification, injection=turn.injection_suspected, question=turn.question)
    return CaseGrade(id=case.id, exact=bool(flags_ok and pred == gold), answered=answered, items=items, qty=qty,
                     mods=mods, closing_ok=answered and turn.closing == case.gold_closing,
                     clarify=answered and turn.needs_clarification, injection=answered and turn.injection_suspected,
                     expected=expected, got=got)


def total(grades: Iterable[CaseGrade]) -> tuple[int, Counts, Counts, Counts]:
    """(exact matches, item counts, quantity counts, modifier counts) summed over ``grades``."""
    exact, items, qty, mods = 0, Counts(), Counts(), Counts()
    for g in grades:
        exact, items, qty, mods = exact + g.exact, items + g.items, qty + g.qty, mods + g.mods
    return exact, items, qty, mods


# ----- fast path -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FastPathStats:
    n: int
    hits: int  # the fast path parsed the utterance (carrier words stripped where needed)
    plain_hits: int  # ... without stripping any carrier word
    correct: int

    def model(self) -> FastPathEval:
        return FastPathEval(coverage_x1000=scaled(self.plain_hits, self.n), precision_x1000=scaled(self.correct,
                            self.hits), coverage_with_carriers_x1000=scaled(self.hits, self.n))


def fast_path_eval(cases: Iterable[EvalCase], menu: MenuSnapshot, lexicons: Lexicons) -> FastPathStats:
    n = hits = plain = correct = 0
    for case in cases:
        n += 1
        cart = start_cart(case.cart_before, menu, channel=case.channel, language=case.language_style)
        result = fast_path(case.utterance, menu, lexicons, cart)
        if result is None:
            continue
        hits += 1
        plain += not result.carriers_stripped
        needs_more = case.gold_clarify or case.gold_injection or case.gold_question is not None
        correct += not needs_more and _keys(apply_turn(case, result.ops, menu)) == _keys(case.gold_cart_after)
    return FastPathStats(n, hits, plain, correct)
