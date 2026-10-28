"""Judge controls (CONTRACTS_API §3 #35-#37; P30b): load a scenario, start or stop the rush, switch a chaos drill.

The tenant comes from P11's session (``SessionAuth``, or an admin's ``?tenant=``), never from the body. Inside a
sandbox any session drives these controls (a judge is every role there); on ``live`` and ``demo`` only an admin does
(CONTRACTS_API §1: "on live/demo only admins ... toggle chaos"; a scenario load or a rush there would reset or fill a
shared tenant), else 403 ``forbidden_role``. Events carry the caller's role as their actor.
"""

from __future__ import annotations

from fastapi import APIRouter

from restaurantos.api.deps import ContainerDep
from restaurantos.contracts.api import ChaosRequest, RushRequest, RushStatus, ScenarioRequest, ScenarioResult
from restaurantos.contracts.common import ChaosFlags, is_sandbox
from restaurantos.contracts.errors import Forbidden, NotWired
from restaurantos.judge import rush, scenario_control
from restaurantos.judge.chaos import TenantChaos
from restaurantos.security.auth import Auth, SessionAuth

router = APIRouter(prefix="/api/judge", tags=["judge"])


def require_judge_powers(auth: Auth) -> None:
    """Any session in its own sandbox; an admin anywhere (the tenant is already scoped by ``auth``)."""
    if not (is_sandbox(auth.tenant_id) or auth.is_admin):
        raise Forbidden("Outside a sandbox only an admin runs judge controls", details={"needs": ["admin"]})


@router.post("/scenario", response_model=ScenarioResult)
async def load_scenario(body: ScenarioRequest, auth: SessionAuth, c: ContainerDep) -> ScenarioResult:
    """#35: reset the tenant into ``dinner_rush`` or ``quiet`` (every surface resyncs on ``sandbox.reset``); a
    running rush stops first. 400 for a rush on the public live board."""
    require_judge_powers(auth)
    rush.cancel(auth.tenant_id)
    return await scenario_control.load(c, auth.tenant_id, body.name)


@router.post("/rush", response_model=RushStatus)
async def rush_control(body: RushRequest, auth: SessionAuth, c: ContainerDep) -> RushStatus:
    """#36: ``start`` runs the scripted rush (pre_call at ``speed``, 10 when 0; 409 ``no_rush`` or ``rush_over``);
    ``stop`` ends it and leaves the clock at ``speed``."""
    require_judge_powers(auth)
    if body.action == "start":
        return await rush.start(c, auth.tenant_id, speed=body.speed, auto_cook=body.auto_cook, actor=auth.role)
    return await rush.stop(c, auth.tenant_id, speed=body.speed, actor=auth.role)


@router.post("/chaos", response_model=ChaosFlags)
async def chaos_drill(body: ChaosRequest, auth: SessionAuth, c: ContainerDep) -> ChaosFlags:
    """#37: switch the drill for one component of this tenant (admin on live and demo); returns all three flags."""
    require_judge_powers(auth)
    port = c.chaos
    if not isinstance(port, TenantChaos):
        raise NotWired("chaos")
    return await port.set(auth.tenant_id, body.target, body.on, actor=auth.role)
