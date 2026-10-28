"""``MetaOrderProvider``: the production ``OrderAgent`` (P13) on one E1 case.

The case's cart before the turn becomes the priced cart the CART block shows (``grade.start_cart``), and the parse
runs exactly as a live turn does (prompt ``o1``, the strict order schema, the ORDER route), so the request hashes of a
recorded run are the ones a replay sends. Two settings differ from a live turn, both so that every answer comes from
the model being measured: failover is off, and the parse timeout is 30 s instead of 5 s (the report counts the
answers that took longer than the production timeout instead of turning them into misses with no recording).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from restaurantos.agents.order_agent import OrderAgent
from restaurantos.evals.grade import start_cart

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.common import CallCtx
    from restaurantos.contracts.menu import MenuSnapshot
    from restaurantos.contracts.orders import OrderTurnLLM
    from restaurantos.contracts.ports import MetaPort
    from restaurantos.evals import EvalCase
    from restaurantos.meta.spend import CallRecord

EVAL_TIMEOUT_S = 30.0


class MetaOrderProvider:
    name = "meta"

    def __init__(self, agent: OrderAgent, menu: MenuSnapshot, *, model: str | None = None) -> None:
        self.agent = agent
        self.menu = menu
        self._model = model

    @property
    def model(self) -> str:
        return self._model or self.agent.primary_model

    async def parse(self, case: EvalCase, ctx: CallCtx) -> tuple[OrderTurnLLM, CallRecord]:
        cart = start_cart(case.cart_before, self.menu, channel=case.channel, language=case.language_style)
        return await self.agent.parse(case.utterance, cart, case.last_question, case.channel, ctx, menu=self.menu,
                                      model=self._model)


def build(meta: MetaPort, settings: Settings, *, menu: MenuSnapshot | None = None, model: str | None = None,
          timeout_s: float = EVAL_TIMEOUT_S) -> MetaOrderProvider:
    """The provider on a fresh ``OrderAgent`` (no failover) over ``meta``; the seed menu unless ``menu`` is given."""
    if menu is None:
        from restaurantos.seed.loader import load_seed

        menu = load_seed().menu
    return MetaOrderProvider(OrderAgent(meta, settings, failover_model=None, timeout_s=timeout_s), menu, model=model)
