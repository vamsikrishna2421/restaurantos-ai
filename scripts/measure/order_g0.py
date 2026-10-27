"""Gate G0: the real order prompt on 20 warm turns (P21-G0; ARCHITECTURE §5.3, DECISIONS D4).

P13's production ``OrderAgent`` (prompt ``o1``, ``build_order_turn_schema``, the ORDER route: muse-spark-1.3, effort
minimal, strict schema, ``max_retries=0``) parses 20 order turns, five each in te-en and ta-en (romanized), es and
en, written like the canonical order ("naaku rendu chicken biryani kavali, okati without onions, inka oka paneer
curry, anthe"). A warm-up parse caches the prefix first and is reported apart (cold unless an earlier run left the
prefix cached); then the turns run one at a time with failover off. Per turn: the parse latency (wall clock around
``parse``, and the HTTP ``latency_ms``), input, cached, output and reasoning tokens, the cost, the raw model output,
and the cart it yields beside the expected cart (``cart_ok``: the same dishes, quantities and modifiers however the
lines are split; ``closing_ok``). The summary holds p50/p95, token stats, cost per turn, correctness per language, the
decision (``full`` while the warm p50 is at most 1.8 s, else measure ``lite`` with ``schema_variant.py``, which makes
the call) and MASTER_PLAN §9 projections from the measured cost.

Live only, tagged and capped: spend scope ``dev:P21-G0``, data class ``synthetic``, and a scope cap of the $0.40 G0
budget minus what earlier G0 runs spent (read back from the ledger). A tiny sample first::

    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21-G0 .venv/bin/python scripts/measure/order_g0.py --turns 4
    RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21-G0 .venv/bin/python scripts/measure/order_g0.py

``--dry-run`` spends nothing: it checks every cart against the menu and prints prompt sizes and the worst case.
Writes ``data/measure/g0_order_<variant>_<stamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file: make `scripts.measure` importable from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.measure.common import patch_pricing, summarize, write_result  # noqa: E402

from restaurantos.agents import order_prompt  # noqa: E402
from restaurantos.agents.order_agent import (  # noqa: E402
    PARSE_TIMEOUT_S,
    WARM_UTTERANCE,
    OrderAgent,
    OrderParseUnavailable,
)
from restaurantos.config import AIMode, ModelRoute, Purpose, Settings, get_settings  # noqa: E402
from restaurantos.contracts.common import CallCtx, Channel  # noqa: E402
from restaurantos.contracts.ids import new_id  # noqa: E402
from restaurantos.contracts.menu import Availability, MenuSnapshot  # noqa: E402
from restaurantos.contracts.orders import Cart, CartOp  # noqa: E402
from restaurantos.contracts.schemas import OrderSchemaVariant  # noqa: E402
from restaurantos.domain.cart import apply_ops  # noqa: E402
from restaurantos.meta import MetaClient, SpendGuard, pricing  # noqa: E402
from restaurantos.meta.spend import CallRecord  # noqa: E402
from restaurantos.seed.loader import load_seed  # noqa: E402

SCOPE = "dev:P21-G0"
TRACE_PREFIX = "p21g0"
BUDGET_USD = 0.40
GATE_P50_S = 1.8  # DECISIONS D4: a slower warm p50 ships the lite variant
MEASURE_TIMEOUT_S = 15.0  # above production's 5 s, so the tail is measured instead of cut off
ASR_USD_PER_MIN = pricing.ASR_USD_PER_HOUR / 60
TOKEN_FIELDS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
AE = "anything_else"
LineSpec = tuple[str, int, tuple[str, ...]]  # (item_code, qty, modifiers): one cart line


@dataclass(frozen=True)
class Turn:
    """One utterance, the cart it starts from (L1..Ln) and the whole cart a correct parse leaves."""

    id: str
    lang: str
    utterance: str
    expected: tuple[LineSpec, ...]
    closing: bool
    channel: Channel = "phone"
    cart: tuple[LineSpec, ...] = ()
    last_question: str | None = None


def _l(code: str, qty: int = 1, *mods: str) -> LineSpec:
    return (code, qty, mods)


CANON = (_l("CHK_BIRY"), _l("CHK_BIRY", 1, "NO_ONION"), _l("PNR_BM"))  # CONTRACTS_SEED §11
_TE = (
    Turn("te1", "te-en", "naaku rendu chicken biryani kavali, okati without onions, inka oka paneer curry, anthe",
         CANON, True),
    Turn("te2", "te-en", "moodu garlic naan, rendu mamidi lassi, inka oka gongura mutton karam ekkuva kavali",
         (_l("GARLIC_NAAN", 3), _l("MANGO_LASSI", 2), _l("GONGURA_MUTTON", 1, "EXTRA_SPICY")), False),
    Turn("te3", "te-en", "naaku oka mutton biryani, okati veg biryani onion vaddu, rendu chai, anthe",
         (_l("GOAT_BIRY"), _l("VEG_BIRY", 1, "NO_ONION"), _l("MASALA_CHAI", 2)), True, "kiosk"),
    Turn("te4", "te-en", "rendu masala dosa kavali, okati karam takkuva, inka oka filter coffee",
         (_l("MASALA_DOSA"), _l("MASALA_DOSA", 1, "MILD"), _l("FILTER_COFFEE")), False),
    Turn("te5", "te-en", "inka rendu butter naan, oka pappu, anthe",
         (_l("CHK_BIRY"), _l("BUTTER_NAAN", 2), _l("DAL_TADKA")), True, cart=(_l("CHK_BIRY"),), last_question=AE),
)
_TA = (
    Turn("ta1", "ta-en", "enakku rendu chicken biryani venum, onnu vengayam illama, appuram oru paneer curry, podhum",
         CANON, True),
    Turn("ta2", "ta-en", "moonu idli, rendu medu vada, oru filter kaapi kudunga",
         (_l("IDLI", 3), _l("SAMBAR_VADA", 2), _l("FILTER_COFFEE")), False, "kiosk"),
    Turn("ta3", "ta-en", "oru chettinad chicken kaaram kammi, rendu poondu naan, avlo dhaan",
         (_l("CHK_CHETTINAD", 1, "MILD"), _l("GARLIC_NAAN", 2)), True),
    Turn("ta4", "ta-en", "rendu masala dosai venum, onnu ennai kammi, oru maambazha lassi",
         (_l("MASALA_DOSA"), _l("MASALA_DOSA", 1, "LESS_OIL"), _l("MANGO_LASSI")), False, "kiosk"),
    Turn("ta5", "ta-en", "enakku oru kozhi biryani, oru thayir sadam, rendu masala tea, podhum",
         (_l("CHK_BIRY"), _l("CURD_RICE"), _l("MASALA_CHAI", 2)), True),
)
_ES = (
    Turn("es1", "es", "quiero dos biryanis de pollo, uno sin cebolla, y un curry de paneer, es todo", CANON, True),
    Turn("es2", "es", "me da tres naan de ajo y dos lassi de mango, por favor",
         (_l("GARLIC_NAAN", 3), _l("MANGO_LASSI", 2)), False, "kiosk"),
    Turn("es3", "es", "quiero un pollo tikka masala muy picante y un biryani de verduras sin cebolla",
         (_l("CHK_TIKKA_MASALA", 1, "EXTRA_SPICY"), _l("VEG_BIRY", 1, "NO_ONION")), False),
    Turn("es4", "es", "dos dosas masala, una sin picante, y un café de filtro, eso es todo",
         (_l("MASALA_DOSA"), _l("MASALA_DOSA", 1, "MILD"), _l("FILTER_COFFEE")), True),
    Turn("es5", "es", "también dos samosas y un gulab jamun, es todo",
         (_l("PNR_BM"), _l("SAMOSA", 2), _l("GULAB_JAMUN")), True, cart=(_l("PNR_BM"),), last_question=AE),
)
_EN = (
    Turn("en1", "en", "two chicken biryanis, one without onions, and one paneer curry", CANON, False, "kiosk"),
    Turn("en2", "en", "can I get three garlic naans, two mango lassis and one chicken tikka masala extra spicy",
         (_l("GARLIC_NAAN", 3), _l("MANGO_LASSI", 2), _l("CHK_TIKKA_MASALA", 1, "EXTRA_SPICY")), False),
    Turn("en3", "en", "I'd like a goat biryani with no garlic, two samosas and a masala chai, that's all",
         (_l("GOAT_BIRY", 1, "NO_GARLIC"), _l("SAMOSA", 2), _l("MASALA_CHAI")), True),
    Turn("en4", "en", "two veg biryanis, one mild and one extra spicy, plus a dal tadka",
         (_l("VEG_BIRY", 1, "MILD"), _l("VEG_BIRY", 1, "EXTRA_SPICY"), _l("DAL_TADKA")), False, "kiosk"),
    Turn("en5", "en", "actually make the biryani just one, and add a gobi manchurian, that's it",
         (_l("CHK_BIRY"), _l("MANGO_LASSI"), _l("GOBI_MAN")), True, cart=(_l("CHK_BIRY", 2), _l("MANGO_LASSI")),
         last_question=AE),
)
TURNS: tuple[Turn, ...] = tuple(t for four in zip(_TE, _TA, _ES, _EN, strict=True) for t in four)  # `--turns 4`: 1 each
WARMUP = Turn("warmup", "en", WARM_UTTERANCE, (), False, "kiosk")  # the shape of OrderAgent.warm()


# ----- carts and scoring --------------------------------------------------------------------------------------------
def cart_counts(lines: Iterable[LineSpec]) -> Counter[tuple[str, tuple[str, ...]]]:
    """Units per (dish, modifier set): equal for the same order however its lines are split or ordered."""
    out: Counter[tuple[str, tuple[str, ...]]] = Counter()
    for code, qty, mods in lines:
        out[(code, tuple(sorted(mods)))] += qty
    return out


def lines_of(cart: Cart) -> list[LineSpec]:
    return [(ln.item_code, ln.qty, tuple(ln.modifiers)) for ln in cart.lines]


def fmt(lines: Iterable[LineSpec]) -> str:
    text = " + ".join(f"{code}x{qty}" + (f"[{','.join(mods)}]" if mods else "") for code, qty, mods in lines)
    return text or "(empty)"


def make_cart(lines: Iterable[LineSpec], menu: MenuSnapshot, channel: Channel = "phone") -> Cart:
    """A priced cart holding ``lines`` as L1..Ln (the real ``cart.apply_ops``); ValueError if one does not apply."""
    specs = list(lines)
    empty = Cart(session_id="vs_p21g0", cart_id="cart_p21g0", language="en", channel=channel)
    ops = [CartOp(op="add", item_code=code, qty=qty, modifiers=list(mods)) for code, qty, mods in specs]
    result = apply_ops(empty, ops, menu, Availability())
    if result.rejected or len(result.cart.lines) != len(specs):
        raise ValueError(f"{fmt(specs)} does not apply cleanly: {[r.reason for r in result.rejected]}")
    return result.cart


def check_turns(turns: Sequence[Turn], menu: MenuSnapshot) -> list[str]:
    """Problems with the turn set itself: a start or expected cart the menu cannot hold."""
    problems = []
    for turn in turns:
        for label, lines in (("cart", turn.cart), ("expected", turn.expected)):
            try:
                make_cart(lines, menu, turn.channel)
            except ValueError as exc:
                problems.append(f"{turn.id} {label}: {exc}")
    return problems


# ----- the live harness ---------------------------------------------------------------------------------------------
class Tap:
    """A ``MetaPort`` proxy for ``OrderAgent``: forwards ``chat_json`` and keeps each call's raw output by trace id
    (also when it then fails validation), so every turn can be read by eye."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.raw: dict[str, Any] = {}

    async def chat_json(self, **kwargs: Any) -> tuple[Any, CallRecord]:
        raw, record = await self.inner.chat_json(**kwargs)
        self.raw[kwargs["ctx"].trace_id] = raw
        return raw, record


class LiveG0:
    """One live G0 run: refuses anything but ``RESTOS_AI_MODE=live`` with a live tag, installs F0's pricing workaround
    for the real client, caps scope ``dev:P21-G0`` at the G0 budget left and collects every call record by trace."""

    def __init__(self, *, max_usd: float | None = None, settings: Settings | None = None,
                 guard: SpendGuard | None = None, meta: Any = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.ai_mode is not AIMode.LIVE or not self.settings.live_tag:
            raise SystemExit("G0 spends real money: run it with RESTOS_AI_MODE=live RESTOS_LIVE_TAG=P21-G0")
        self.pricing_workaround = patch_pricing() if meta is None else False
        self.guard = guard or SpendGuard.from_settings(self.settings)
        self.spent_before = round(self.guard.scope_spent(SCOPE), 6)
        left = max(0.0, BUDGET_USD - self.spent_before)
        self.remaining = round(left if max_usd is None else min(left, max(0.0, max_usd)), 6)
        self.scope_cap = round(self.spent_before + self.remaining, 6)
        self.meta = meta if meta is not None else MetaClient(self.settings, self.guard)
        self.records: dict[str, list[CallRecord]] = {}
        self._unsubscribe = self.guard.subscribe(self._on_record)
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")

    def _on_record(self, rec: CallRecord) -> None:
        self.records.setdefault(rec.trace_id or "", []).append(rec)

    def ctx(self, channel: Channel | None = None) -> CallCtx:
        return CallCtx(trace_id=new_id(TRACE_PREFIX), data_class="synthetic", channel=channel, spend_scope=SCOPE,
                       scope_cap_usd=self.scope_cap)

    def spent(self) -> float:
        """USD scope ``dev:P21-G0`` gained since this run started (every process's calls, from the ledger)."""
        return round(self.guard.scope_spent(SCOPE) - self.spent_before, 6)

    def header(self) -> dict[str, Any]:
        return {"gate": "G0", "live_tag": self.settings.live_tag, "spend_scope": SCOPE, "started_at": self.started_at,
                "budget_usd": BUDGET_USD, "spent_before_usd": self.spent_before, "budget_left_usd": self.remaining,
                "scope_cap_usd": self.scope_cap, "pricing_workaround": self.pricing_workaround}

    async def aclose(self) -> None:
        self._unsubscribe()
        if hasattr(self.meta, "aclose"):
            await self.meta.aclose()


def usage(records: Sequence[CallRecord]) -> dict[str, Any]:
    """Status, model, HTTP latency, tokens and cost of one turn's call records (failover is off: one call)."""
    last = records[-1] if records else None
    return {"status": last.status if last else None, "model": last.model if last else None,
            "latency_ms": last.latency_ms if last else None,
            **{name: sum(getattr(r, name) for r in records) for name in TOKEN_FIELDS},
            "cost_usd": round(sum(r.cost_usd for r in records), 7)}


async def run_turn(run: LiveG0, agent: OrderAgent, tap: Tap, turn: Turn, menu: MenuSnapshot, *,
                   clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    """Parse one turn on the primary model and score the cart it yields; a failed parse lands on the row."""
    cart = make_cart(turn.cart, menu, turn.channel)
    ctx = run.ctx(turn.channel)
    row: dict[str, Any] = {"id": turn.id, "lang": turn.lang, "channel": turn.channel, "utterance": turn.utterance,
                           "cart_before": fmt(turn.cart), "expected": fmt(turn.expected),
                           "expected_closing": turn.closing, "trace_id": ctx.trace_id, "error": None}
    t0 = clock()
    try:
        out, _ = await agent.parse(turn.utterance, cart, turn.last_question, turn.channel, ctx, menu=menu,
                                   model=agent.primary_model)
    except OrderParseUnavailable as exc:
        out, cause = None, exc.__cause__
        row["error"] = f"{exc.reason}: {type(cause).__name__}: {cause}"[:300] if cause else exc.reason
    row["wall_ms"] = round((clock() - t0) * 1000)
    row |= usage(run.records.get(ctx.trace_id, []))
    row["raw"] = tap.raw.get(ctx.trace_id)
    if out is None:
        return row | {"cart_ok": False, "closing_ok": False}
    result = apply_ops(cart, out.ops, menu, Availability())
    got = lines_of(result.cart)
    rejected = [f"{r.reason}:{r.op.item_code or r.op.line_ref}" for r in result.rejected]
    return row | {"got": fmt(got), "rejected": rejected,
                  "cart_ok": cart_counts(got) == cart_counts(turn.expected), "closing": out.closing,
                  "closing_ok": out.closing == turn.closing, "language": out.language, "intent": out.intent,
                  "needs_clarification": out.needs_clarification, "injection_suspected": out.injection_suspected}


def prompt_info(menu: MenuSnapshot, variant: OrderSchemaVariant, route: ModelRoute) -> dict[str, Any]:
    """Prompt and schema sizes of ``variant`` and the worst-case pre-call hold of one parse (MetaClient's estimate:
    no cache hit, the whole output budget)."""
    req = order_prompt.build_request(menu, (), None, "phone", TURNS[0].utterance, variant)
    body: dict[str, Any] = {"model": route.model, "messages": req.messages,
                            "max_completion_tokens": route.max_output_tokens, "prompt_cache_key": req.cache_key,
                            "response_format": {"type": "json_schema", "json_schema": {
                                "name": req.schema_name, "schema": req.schema, "strict": True}}}
    if route.effort:
        body["reasoning_effort"] = route.effort
    chars = len(json.dumps(body, ensure_ascii=False))
    return {"version": order_prompt.PROMPT_VERSION, "variant": variant, "cache_key": req.cache_key,
            "system_chars": len(req.messages[0]["content"]), "schema_chars": len(json.dumps(req.schema)),
            "examples": len(order_prompt.examples_for(menu, variant)),
            "hold_usd": round(pricing.estimate_text_call_usd(route.model, chars, route.max_output_tokens), 6)}


# ----- summary, decision, projections -------------------------------------------------------------------------------
def summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Latency and tokens over the turns that parsed; cost and correctness over all; counts per language."""
    ok = [r for r in rows if r["error"] is None]
    fed = sum(r["input_tokens"] for r in ok)
    by_lang = {}
    for lang in dict.fromkeys(r["lang"] for r in rows):
        mine = [r for r in rows if r["lang"] == lang]
        by_lang[lang] = {"n": len(mine), "cart_ok": sum(r["cart_ok"] for r in mine),
                         "closing_ok": sum(r["closing_ok"] for r in mine),
                         "p50_s": summarize(r["wall_ms"] / 1000 for r in mine if r["error"] is None)["p50"]}
    return {
        "n": len(rows), "ok": len(ok), "errors": dict(Counter(r["error"].split(":")[0] for r in rows if r["error"])),
        "latency_wall_s": summarize(r["wall_ms"] / 1000 for r in ok),
        "latency_http_s": summarize(r["latency_ms"] / 1000 for r in ok if r["latency_ms"] is not None),
        **{name: summarize((r[name] for r in ok), 1) for name in TOKEN_FIELDS},
        "cache_hit": round(sum(r["cached_tokens"] for r in ok) / fed, 3) if fed else None,
        "cold_turns": sum(1 for r in ok if r["cached_tokens"] == 0),
        "over_prod_timeout": sum(1 for r in ok if r["wall_ms"] > PARSE_TIMEOUT_S * 1000),
        "cost_usd": summarize((r["cost_usd"] for r in rows), 7),
        "total_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "cart_ok": sum(r["cart_ok"] for r in rows), "closing_ok": sum(r["closing_ok"] for r in rows),
        "both_ok": sum(r["cart_ok"] and r["closing_ok"] for r in rows), "by_lang": by_lang,
    }


def decide(summary: dict[str, Any], variant: OrderSchemaVariant, gate_s: float = GATE_P50_S) -> dict[str, Any]:
    """DECISIONS D4 on a ``full`` run: keep it while the warm p50 is at most ``gate_s``, else measure and ship
    ``lite``. A ``lite`` run only says whether it meets the gate (``schema_variant.py`` compares the two)."""
    p50 = summary["latency_wall_s"]["p50"]
    met = p50 is not None and p50 <= gate_s
    out: dict[str, Any] = {"variant": variant, "gate_s": gate_s, "p50_s": p50, "gate_met": met}
    if variant == "full" and p50 is not None:
        out["recommend"] = "full" if met else "lite"
        out["next"] = None if met else "measure lite: scripts/measure/schema_variant.py"
    return out


def projections(turn_usd: float | None, cold_usd: float | None, hold_usd: float, settings: Settings) -> dict[str, Any]:
    """MASTER_PLAN §9 unit costs from the measured mean cost of a warm parse, and what the call and sandbox caps hold.
    Assumptions: a kiosk voice order is 1–2 parses and 0.5–1 min of ASR; a phone order 3–4 parse turns plus at most
    2 speculative parses and 2 min of ASR; a call scope also pays the ASR of a maximum-length call."""
    if not turn_usd:
        return {"note": "no billed turn: nothing to project"}
    kiosk = (turn_usd + 0.5 * ASR_USD_PER_MIN, 2 * turn_usd + ASR_USD_PER_MIN)
    phone = (3 * turn_usd + 2 * ASR_USD_PER_MIN, 6 * turn_usd + 2 * ASR_USD_PER_MIN)
    asr_call = settings.phone_max_call_s / 60 * ASR_USD_PER_MIN
    call_room = settings.call_usd_cap - asr_call - hold_usd  # the last parse still needs its worst-case hold free
    return {"parse_turn_usd": round(turn_usd, 6), "cold_parse_usd": cold_usd, "hold_usd": hold_usd,
            "kiosk_voice_order_usd": [round(x, 5) for x in kiosk], "phone_order_usd": [round(x, 5) for x in phone],
            "call_cap_usd": settings.call_usd_cap, "asr_max_call_usd": round(asr_call, 5),
            "parses_within_call_cap": max(0, math.floor(call_room / turn_usd) + 1) if call_room >= 0 else 0,
            "sandbox_cap_usd": settings.sandbox_usd_cap,
            "phone_orders_within_sandbox_cap": math.floor(settings.sandbox_usd_cap / phone[1]),
            "kiosk_orders_within_sandbox_cap": math.floor(settings.sandbox_usd_cap / kiosk[1])}


async def measure(run: LiveG0, variant: OrderSchemaVariant, turns: Sequence[Turn], *, menu: MenuSnapshot | None = None,
                  timeout_s: float = MEASURE_TIMEOUT_S, gap_s: float = 0.5, warmup: bool = True,
                  echo: Callable[[str], None] = print) -> dict[str, Any]:
    """A warm-up parse (unless ``warmup`` is off), then ``turns`` one at a time on ``variant``; stops on a budget
    refusal, three failures in a row, or a warm-up that failed for any reason but a timeout. Returns the payload."""
    menu = menu or load_seed().menu
    spent_before = run.spent()
    tap = Tap(run.meta)
    agent = OrderAgent(tap, run.settings, variant=variant, failover_model=None, timeout_s=timeout_s)
    route = run.settings.route(Purpose.ORDER)
    info = prompt_info(menu, variant, route)
    echo(f"G0 {variant}: {len(turns)} turns on {agent.primary_model}; budget left ${run.remaining:.4f}; "
         f"worst case ${info['hold_usd'] * (len(turns) + warmup):.4f}")
    warm = await run_turn(run, agent, tap, WARMUP, menu) if warmup else None
    if warm is not None:
        echo(row_line(warm))
    rows: list[dict[str, Any]] = []
    if warm is not None and warm["error"] and not warm["error"].startswith("timeout"):
        echo(f"warm-up failed, no turns sent: {warm['error']}")
    else:
        streak = 0
        for turn in turns:
            await asyncio.sleep(gap_s)
            rows.append(row := await run_turn(run, agent, tap, turn, menu))
            echo(row_line(row))
            streak = streak + 1 if row["error"] else 0
            if (row["error"] or "").startswith("budget") or streak >= 3:
                echo(f"stopping early: {row['error']}")
                break
    summary = summarize_rows(rows)
    cold = warm is not None and warm["error"] is None and warm["cached_tokens"] == 0  # else an earlier run warmed it
    return {**run.header(), "script": "order_g0", "variant": variant, "model": agent.primary_model,
            "effort": route.effort, "max_completion_tokens": route.max_output_tokens, "timeout_s": timeout_s,
            "data_class": "synthetic", "prompt": info, "warmup": warm, "summary": summary,
            "decision": decide(summary, variant),
            "projections": projections(summary["cost_usd"]["mean"], warm["cost_usd"] if cold and warm else None,
                                       info["hold_usd"], run.settings),
            "spent_usd": round(run.spent() - spent_before, 6), "turns": rows}


# ----- output -------------------------------------------------------------------------------------------------------
def row_line(row: dict[str, Any]) -> str:
    head = (f"{row['id']:6} {row['lang']:5} {row['wall_ms']:5d} ms  in={row['input_tokens']} "
            f"cached={row['cached_tokens']} out={row['output_tokens']} reasoning={row['reasoning_tokens']} "
            f"${row['cost_usd']:.5f}")
    if row["error"]:
        return f"{head}  ERROR {row['error']}"
    flags = f"cart={'ok' if row['cart_ok'] else 'WRONG'} closing={'ok' if row['closing_ok'] else 'WRONG'}"
    return (f"{head}  {flags}\n       said: {row['utterance']}\n       want: {row['expected']}"
            f" closing={row['expected_closing']}\n       got:  {row['got']} closing={row['closing']}"
            + (f" rejected={row['rejected']}" if row["rejected"] else ""))


def report(payload: dict[str, Any]) -> str:
    s, d, p = payload["summary"], payload["decision"], payload["projections"]
    wall, http = s["latency_wall_s"], s["latency_http_s"]
    tokens = " ".join(f"{name.split('_')[0]}={s[name]['mean']}" for name in TOKEN_FIELDS)
    langs = ", ".join(f"{k} {v['cart_ok']}/{v['n']} p50 {v['p50_s']} s" for k, v in s["by_lang"].items())
    return "\n".join([
        f"G0 {payload['variant']}: {s['ok']}/{s['n']} parsed, errors {s['errors'] or 'none'}; "
        f"warm p50 {wall['p50']} s, p95 {wall['p95']} s (HTTP p50 {http['p50']} s, p95 {http['p95']} s); "
        f"over 5 s: {s['over_prod_timeout']}",
        f"tokens (mean) {tokens}; cache hit {s['cache_hit']}; cold turns {s['cold_turns']}",
        f"cost per turn mean ${s['cost_usd']['mean']} p95 ${s['cost_usd']['p95']}; carts {s['cart_ok']}/{s['n']}, "
        f"closing {s['closing_ok']}/{s['n']}; {langs}",
        f"decision: {d}", f"projections: {p}", f"spent ${payload['spent_usd']:.4f}"])


def dry_run(variant: OrderSchemaVariant, n: int, settings: Settings | None = None,
            echo: Callable[[str], None] = print) -> int:
    """Checks the turns against the seed menu and prints both variants' sizes and the worst case. No network."""
    menu = load_seed().menu
    route = (settings or get_settings()).route(Purpose.ORDER)
    for v in ("full", "lite"):
        info = prompt_info(menu, v, route)
        echo(f"{v}: {info}; worst case for {n} turns + warm-up ${info['hold_usd'] * (n + 1):.4f}")
    for turn in TURNS[:n]:
        echo(f"{turn.id:4} {turn.lang:5} {turn.channel:5} {turn.utterance}\n     from {fmt(turn.cart)} want "
             f"{fmt(turn.expected)} closing={turn.closing}")
    problems = check_turns(TURNS, menu)
    echo("\n".join(problems) if problems else f"every cart applies cleanly; running variant {variant}")
    return 1 if problems else 0


async def _live(args: argparse.Namespace) -> int:
    run = LiveG0(max_usd=args.max_usd)
    variant: OrderSchemaVariant = args.variant or run.settings.order_schema_variant
    try:
        payload = await measure(run, variant, TURNS[: args.turns], timeout_s=args.timeout_s, gap_s=args.gap_s,
                                warmup=not args.no_warmup)
    finally:
        await run.aclose()
    path = write_result(run.settings.data_dir, f"g0_order_{variant}", payload)
    print(f"{report(payload)}\n-> {path}")
    return 0 if payload["summary"]["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--variant", choices=("full", "lite"), default=None, help="schema variant (default: settings)")
    ap.add_argument("--turns", type=int, default=len(TURNS), help="the first N turns (interleaved by language)")
    ap.add_argument("--max-usd", type=float, default=None, help="cap this run below the G0 budget left")
    ap.add_argument("--timeout-s", type=float, default=MEASURE_TIMEOUT_S, help="per-parse timeout")
    ap.add_argument("--gap-s", type=float, default=0.5, help="pause before each turn")
    ap.add_argument("--no-warmup", action="store_true", help="skip the warm-up parse")
    ap.add_argument("--dry-run", action="store_true", help="spend nothing: check the turns, print sizes")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not 1 <= args.turns <= len(TURNS):
        ap.error(f"--turns must be 1..{len(TURNS)}")
    if args.dry_run:
        return dry_run(args.variant or "full", args.turns)
    return asyncio.run(_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
