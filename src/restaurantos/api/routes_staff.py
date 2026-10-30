"""Staff identity routes (P41): the front door's people layer.

| Route | Auth | Does |
|---|---|---|
| GET /api/staff | S | the tenant's staff list (names + languages only — never PIN material) |
| POST /api/staff/signup | S | add a person (name, language, 4-digit PIN) and sign them in |
| POST /api/staff/login | S | PIN check → a signed staff token (12 h) |
| GET /api/staff/roles | S (+ optional staff token) | every role slot for the shift, holder or open |
| POST /api/staff/roles/claim | S + staff token | take an open slot (409 ``slot_taken`` / ``already_claimed``) |
| POST /api/staff/roles/release | S + staff token | end your own shift (drop your slot) |
| POST /api/staff/roles/release-other | S + staff token | the Manager slot's holder releases someone else's slot |

The tenant always comes from ``ros_sess`` (``security.auth``), never from the request or the staff token — the
token only says *who* is acting inside that tenant. People and claims live in ``services.staff_identity``'s own
SQLite file; the seed personas and the station list are read from the tenant's rows in the main store on every
call, so a tenant created or reset moments ago is served correctly with no extra wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from restaurantos.api.deps import ContainerDep, get_store
from restaurantos.contracts.api import (
    OkResult,
    RoleClaimRequest,
    RoleReleaseRequest,
    RoleSlot,
    StaffLoginRequest,
    StaffLoginResult,
    StaffPerson,
    StaffSignupRequest,
)
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.errors import Forbidden, NotFound
from restaurantos.contracts.ports import StorePort
from restaurantos.security.auth import SessionAuth
from restaurantos.services import staff_identity as si
from restaurantos.services.container import Container
from restaurantos.store import repo

router = APIRouter(tags=["staff"])

StoreDep = Annotated[StorePort, Depends(get_store)]

_stores: dict[Path, si.StaffStore] = {}


def staff_store(c: Container) -> si.StaffStore:
    """The one StaffStore for the app's data dir (created on first use; tests get one per temp data dir)."""
    path = c.settings.data_dir / "staff.db"
    store = _stores.get(path)
    if store is None:
        store = _stores[path] = si.StaffStore(path)
    return store


async def _defs(c: Container, store: StorePort, tenant_id: str) -> list[si.SlotDef]:
    """Seed the tenant's people (once) and derive its role slots from its stations."""
    async with store.read() as conn:
        personas = await repo.staff(conn, tenant_id)
        snapshot = await repo.menu_snapshot(conn, tenant_id)
    staff_store(c).ensure_seeded(
        tenant_id, [(p.id, p.name, p.languages[0] if p.languages else "en") for p in personas])
    return si.derive_slots(list(snapshot.stations))


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    return header[7:] if header.lower().startswith("bearer ") else None


def _caller(request: Request, c: Container, tenant_id: str) -> StaffPerson:
    """The acting person from the staff token (strict: mutations need a valid one)."""
    return si.read_staff_token(c.settings, _bearer(request), tenant_id, c.clock.now_real_ms())


def _caller_lenient(request: Request, c: Container, tenant_id: str) -> StaffPerson | None:
    try:
        return _caller(request, c, tenant_id)
    except Forbidden:
        return None


def _login(c: Container, person: StaffPerson, tenant_id: str) -> StaffLoginResult:
    token, exp = si.mint_staff_token(c.settings, person, tenant_id, c.clock.now_real_ms())
    return StaffLoginResult(token=token, staff=person, expires_at=utc_from_ms(exp))


@router.get("/api/staff", response_model=list[StaffPerson])
async def list_staff(who: SessionAuth, c: ContainerDep, store: StoreDep) -> list[StaffPerson]:
    """The pick-your-name list. Reading it seeds the tenant's personas on first use."""
    await _defs(c, store, who.tenant_id)
    return staff_store(c).list_staff(who.tenant_id)


@router.post("/api/staff/signup", response_model=StaffLoginResult)
async def signup(body: StaffSignupRequest, who: SessionAuth, c: ContainerDep, store: StoreDep) -> StaffLoginResult:
    """Add a person and sign them straight in. 409 ``name_taken`` when the name is already on the list."""
    await _defs(c, store, who.tenant_id)
    person = staff_store(c).signup(who.tenant_id, body.name.strip(), body.language, body.pin)
    return _login(c, person, who.tenant_id)


@router.post("/api/staff/login", response_model=StaffLoginResult)
async def login(body: StaffLoginRequest, who: SessionAuth, c: ContainerDep, store: StoreDep) -> StaffLoginResult:
    """PIN check. 404 for an unknown staff id, 403 ``bad_pin`` for a wrong PIN."""
    await _defs(c, store, who.tenant_id)
    record = staff_store(c).record(who.tenant_id, body.staff_id)
    if record is None:
        raise NotFound(f"unknown staff member {body.staff_id!r}")
    if not si.pin_matches(body.pin, record.pin_salt, record.pin_hash):
        raise Forbidden("Wrong PIN", code="bad_pin")
    return _login(c, record.person, who.tenant_id)


@router.get("/api/staff/roles", response_model=list[RoleSlot])
async def list_roles(request: Request, who: SessionAuth, c: ContainerDep, store: StoreDep) -> list[RoleSlot]:
    """Every slot for the shift: open, or with its holder's name. A stale token just loses ``mine``."""
    defs = await _defs(c, store, who.tenant_id)
    caller = _caller_lenient(request, c, who.tenant_id)
    return staff_store(c).slots_view(who.tenant_id, defs, caller.id if caller else None)


@router.post("/api/staff/roles/claim", response_model=RoleSlot)
async def claim_role(body: RoleClaimRequest, request: Request, who: SessionAuth, c: ContainerDep,
                     store: StoreDep) -> RoleSlot:
    """Take an open slot. 404 for a slot this tenant does not have; 409s from the store for the two races."""
    defs = await _defs(c, store, who.tenant_id)
    caller = _caller(request, c, who.tenant_id)
    mine = next((d for d in defs if d.slot_id == body.slot_id), None)
    if mine is None:
        raise si.slot_not_found(body.slot_id)
    staff_store(c).claim(who.tenant_id, mine, caller.id, c.clock.now_real_ms())
    view = staff_store(c).slots_view(who.tenant_id, defs, caller.id)
    return next(s for s in view if s.slot_id == body.slot_id)


@router.post("/api/staff/roles/release", response_model=OkResult)
async def release_role(request: Request, who: SessionAuth, c: ContainerDep, store: StoreDep) -> OkResult:
    """End your shift: drop your slot (a no-op when you hold none)."""
    await _defs(c, store, who.tenant_id)
    caller = _caller(request, c, who.tenant_id)
    staff_store(c).release(who.tenant_id, caller.id)
    return OkResult()


@router.post("/api/staff/roles/release-other", response_model=OkResult)
async def release_other(body: RoleReleaseRequest, request: Request, who: SessionAuth, c: ContainerDep,
                        store: StoreDep) -> OkResult:
    """Release someone else's slot. Only the person currently holding the Manager slot may: 403
    ``manager_required`` otherwise. 404 for a slot this tenant does not have."""
    defs = await _defs(c, store, who.tenant_id)
    caller = _caller(request, c, who.tenant_id)
    if staff_store(c).claim_of(who.tenant_id, caller.id) != "manager":
        raise Forbidden("Only the manager on shift can release someone else's job", code="manager_required")
    if all(d.slot_id != body.slot_id for d in defs):
        raise si.slot_not_found(body.slot_id)
    staff_store(c).release_slot(who.tenant_id, body.slot_id)
    return OkResult()
