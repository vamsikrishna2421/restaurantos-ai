"""Eval providers (P31a): who parses one E1 case.

``meta`` (``providers.meta.MetaOrderProvider``: the production ``OrderAgent`` on Muse Spark) is the only provider in
E1; P40 adds ``openweight``. ``model`` names the model that answers (the runner's contributor check reads it before
any call). ``parse`` returns the turn and its ``CallRecord``, or raises what the agent raises: ``OrderParseUnavailable``
(a miss with its reason, or a budget stop), ``PolicyViolation`` (the run aborts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from restaurantos.contracts.common import CallCtx
    from restaurantos.contracts.orders import OrderTurnLLM
    from restaurantos.evals import EvalCase
    from restaurantos.meta.spend import CallRecord


class EvalProvider(Protocol):
    name: str

    @property
    def model(self) -> str: ...

    async def parse(self, case: EvalCase, ctx: CallCtx) -> tuple[OrderTurnLLM, CallRecord]: ...
