"""Staff identity routes (P41): the front door's people layer.

| Route | Auth | Does |
|---|---|---|
| GET /api/staff | S | the tenant's staff list, each person with their usual job (pass 5) |
| POST /api/staff/signup | S | add a person (name, language, 4-digit PIN) and sign them in; the body may |
|  |  | carry their usual job — a predefined slot, or a custom job to define (manager's OK pending) |
| POST /api/staff/login | S | PIN check → a signed staff token (12 h) |
| GET /api/staff/roles | S (+ optional staff token) | every role slot for the shift, holder or open |
| POST /api/staff/roles/claim | S + staff token | take an open slot (409 ``slot_taken`` / ``already_claimed``) |
| POST /api/staff/roles/release | S + staff token | end your own shift (drop your slot) |
| POST /api/staff/roles/release-other | S + staff token | the Manager slot's holder releases someone else's slot |
| GET /api/staff/role-requests | S + staff token | the usual-job asks waiting on the manager (pass 5) |
| POST /api/staff/role-requests/decide | S + staff token | the manager approves or declines one ask |
| POST /api/staff/default | S + staff token | the manager sets (or clears) a person's usual job |

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
    RoleRequest,
    RoleRequestDecision,
    RoleSlot,
    SetDefaultRequest,
    StaffLoginRequest,
    StaffLoginResult,
    StaffPerson,
    StaffSignupRequest,
)
from restaurantos.contracts.common import utc_from_ms
from restaurantos.contracts.errors import BadRequest, Conflict, Forbidden, NotFound
from restaurantos.contracts.kitchen import Station
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


async def _base(c: Container, store: StorePort, tenant_id: str) -> tuple[list[si.SlotDef], list[Station]]:
    """Seed the tenant's people (once, with the usual jobs their seed roles imply) and derive its predefined
    role slots from its stations."""
    async with store.read() as conn:
        personas = await repo.staff(conn, tenant_id)
        snapshot = await repo.menu_snapshot(conn, tenant_id)
    staff_store(c).ensure_seeded(
        tenant_id,
        [(p.id, p.name, p.languages[0] if p.languages else "en") for p in personas],
        {p.id: si.seed_default(p.role, list(p.station_ids)) for p in personas})
    stations = list(snapshot.stations)
    return si.derive_slots(stations), stations


def _tenant_defs(c: Container, tenant_id: str, predefined: list[si.SlotDef]) -> list[si.SlotDef]:
    """The claimable set: predefined slots plus the tenant's approved custom jobs."""
    approved = [r.slot_def() for r in staff_store(c).custom_roles(tenant_id) if r.status == "approved"]
    return [*predefined, *approved]


def _view_defs(c: Container, tenant_id: str, predefined: list[si.SlotDef],
               caller_id: str | None) -> list[si.SlotDef]:
    """The claimable set plus the caller's own pending custom jobs — shown to them as waiting, and claimable
    by nobody (the claim answers ``role_pending`` rather than pretending the job does not exist)."""
    defs = _tenant_defs(c, tenant_id, predefined)
    if caller_id:
        defs += [r.slot_def() for r in staff_store(c).custom_roles(tenant_id)
                 if r.status == "pending" and r.created_by == caller_id]
    return defs


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


def _manager_on_shift(request: Request, c: Container, tenant_id: str) -> StaffPerson:
    """The acting person, who must currently hold the Manager slot: 403 ``manager_required`` otherwise."""
    caller = _caller(request, c, tenant_id)
    if staff_store(c).claim_of(tenant_id, caller.id) != "manager":
        raise Forbidden("Only the manager on shift can decide that", code="manager_required")
    return caller


def _login(c: Container, person: StaffPerson, tenant_id: str) -> StaffLoginResult:
    token, exp = si.mint_staff_token(c.settings, person, tenant_id, c.clock.now_real_ms())
    return StaffLoginResult(token=token, staff=person, expires_at=utc_from_ms(exp))


@router.get("/api/staff", response_model=list[StaffPerson])
async def list_staff(who: SessionAuth, c: ContainerDep, store: StoreDep) -> list[StaffPerson]:
    """The pick-your-name list, each person with their usual job. Reading it seeds the tenant's personas."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    return staff_store(c).people(who.tenant_id, _tenant_defs(c, who.tenant_id, predefined))


@router.post("/api/staff/signup", response_model=StaffLoginResult)
async def signup(body: StaffSignupRequest, who: SessionAuth, c: ContainerDep, store: StoreDep) -> StaffLoginResult:
    """Add a person and sign them straight in. 409 ``name_taken`` when the name is already on the list.

    The usual job rides along (pass 5): a predefined slot is active at once — except the Manager job, which
    waits for the manager's OK — and a custom job is created pending that same OK. An unknown slot is a 404,
    an unknown station in a custom job a 400, a duplicate custom-job name a 409 ``role_name_taken``."""
    predefined, stations = await _base(c, store, who.tenant_id)
    tenant_store = staff_store(c)
    defs = _tenant_defs(c, who.tenant_id, predefined)
    if body.custom_role is not None:
        known = {s.id for s in stations}
        unknown = [s for s in body.custom_role.station_ids if s not in known]
        if unknown:
            raise BadRequest(f"unknown kitchen station(s): {', '.join(unknown)}", code="unknown_station")
        clash = any(r.name.strip().lower() == body.custom_role.name.strip().lower() and r.status != "declined"
                    for r in tenant_store.custom_roles(who.tenant_id))
        if clash:
            raise Conflict(f"A job called {body.custom_role.name} already exists here",
                           code="role_name_taken")
    elif body.default_slot_id is not None and all(d.slot_id != body.default_slot_id for d in defs):
        raise si.slot_not_found(body.default_slot_id)
    person = tenant_store.signup(who.tenant_id, body.name.strip(), body.language, body.pin)
    if body.custom_role is not None:
        role = tenant_store.create_custom_role(who.tenant_id, body.custom_role.name.strip(),
                                               body.custom_role.station_ids, person.id,
                                               c.clock.now_real_ms())
        tenant_store.set_default(who.tenant_id, person.id, role.slot_id)
    elif body.default_slot_id is not None:
        tenant_store.set_default(who.tenant_id, person.id, body.default_slot_id,
                                 pending=body.default_slot_id == "manager")
    return _login(c, tenant_store.person(who.tenant_id, person.id, defs) or person, who.tenant_id)


@router.post("/api/staff/login", response_model=StaffLoginResult)
async def login(body: StaffLoginRequest, who: SessionAuth, c: ContainerDep, store: StoreDep) -> StaffLoginResult:
    """PIN check. 404 for an unknown staff id, 403 ``bad_pin`` for a wrong PIN."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    tenant_store = staff_store(c)
    record = tenant_store.record(who.tenant_id, body.staff_id)
    if record is None:
        raise NotFound(f"unknown staff member {body.staff_id!r}")
    if not si.pin_matches(body.pin, record.pin_salt, record.pin_hash):
        raise Forbidden("Wrong PIN", code="bad_pin")
    person = tenant_store.person(who.tenant_id, body.staff_id, _tenant_defs(c, who.tenant_id, predefined))
    return _login(c, person or record.person, who.tenant_id)


@router.get("/api/staff/roles", response_model=list[RoleSlot])
async def list_roles(request: Request, who: SessionAuth, c: ContainerDep, store: StoreDep) -> list[RoleSlot]:
    """Every slot for the shift: open, or with its holder's name. A stale token just loses ``mine``."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    caller = _caller_lenient(request, c, who.tenant_id)
    defs = _view_defs(c, who.tenant_id, predefined, caller.id if caller else None)
    return staff_store(c).slots_view(who.tenant_id, defs, caller.id if caller else None)


@router.post("/api/staff/roles/claim", response_model=RoleSlot)
async def claim_role(body: RoleClaimRequest, request: Request, who: SessionAuth, c: ContainerDep,
                     store: StoreDep) -> RoleSlot:
    """Take an open slot. 404 for a slot this tenant does not have; 409s from the store for the two races;
    403 ``role_pending`` / ``role_not_approved`` for a job the caller may not take yet (pass 5 gates).

    Picking a predefined job while you have no usual job makes it your usual job — the first pick sticks."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    tenant_store = staff_store(c)
    caller = _caller(request, c, who.tenant_id)
    defs = _view_defs(c, who.tenant_id, predefined, caller.id)
    mine = next((d for d in defs if d.slot_id == body.slot_id), None)
    if mine is None:
        raise si.slot_not_found(body.slot_id)
    default = tenant_store.default_of(who.tenant_id, caller.id, _tenant_defs(c, who.tenant_id, predefined))
    active_default = default.slot_id if default and not default.pending else None
    tenant_store.claim(who.tenant_id, mine, caller.id, c.clock.now_real_ms(), caller_default_slot=active_default)
    if default is None and not mine.custom and mine.slot_id != "manager":
        tenant_store.set_default(who.tenant_id, caller.id, mine.slot_id)
    view = tenant_store.slots_view(who.tenant_id, defs, caller.id)
    return next(s for s in view if s.slot_id == body.slot_id)


@router.post("/api/staff/roles/release", response_model=OkResult)
async def release_role(request: Request, who: SessionAuth, c: ContainerDep, store: StoreDep) -> OkResult:
    """End your shift: drop your slot (a no-op when you hold none)."""
    await _base(c, store, who.tenant_id)
    caller = _caller(request, c, who.tenant_id)
    staff_store(c).release(who.tenant_id, caller.id)
    return OkResult()


@router.post("/api/staff/roles/release-other", response_model=OkResult)
async def release_other(body: RoleReleaseRequest, request: Request, who: SessionAuth, c: ContainerDep,
                        store: StoreDep) -> OkResult:
    """Release someone else's slot. Only the person currently holding the Manager slot may: 403
    ``manager_required`` otherwise. 404 for a slot this tenant does not have."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    caller = _caller(request, c, who.tenant_id)
    if staff_store(c).claim_of(who.tenant_id, caller.id) != "manager":
        raise Forbidden("Only the manager on shift can release someone else's job", code="manager_required")
    if all(d.slot_id != body.slot_id for d in _tenant_defs(c, who.tenant_id, predefined)):
        raise si.slot_not_found(body.slot_id)
    staff_store(c).release_slot(who.tenant_id, body.slot_id)
    return OkResult()


def _binding_detail(role: si.CustomRole, stations: list[Station]) -> str:
    """A custom job's coverage in words, for the manager's decision list."""
    if not role.station_ids:
        return "No station — helps everywhere on the floor"
    names = {s.id: (s.names.en or s.id) for s in stations}
    return "Covers " + ", ".join(names.get(s, s) for s in role.station_ids)


@router.get("/api/staff/role-requests", response_model=list[RoleRequest])
async def list_role_requests(request: Request, who: SessionAuth, c: ContainerDep,
                             store: StoreDep) -> list[RoleRequest]:
    """The usual-job asks waiting on the manager on shift: Manager-job picks and custom jobs, oldest first."""
    predefined, stations = await _base(c, store, who.tenant_id)
    _manager_on_shift(request, c, who.tenant_id)
    tenant_store = staff_store(c)
    defs = _tenant_defs(c, who.tenant_id, predefined)
    requests: list[RoleRequest] = []
    for person in tenant_store.people(who.tenant_id, defs):
        default = person.default_role
        if default and default.pending and default.slot_id == "manager":
            requests.append(RoleRequest(request_id=f"manager:{person.id}", staff_id=person.id,
                                        staff_name=person.name, label="Manager", detail="The Manager job"))
    people = {p.id: p for p in tenant_store.people(who.tenant_id, defs)}
    for role in tenant_store.custom_roles(who.tenant_id):
        if role.status != "pending":
            continue
        creator = people.get(role.created_by)
        requests.append(RoleRequest(request_id=role.slot_id, staff_id=role.created_by,
                                    staff_name=creator.name if creator else role.created_by,
                                    label=role.name, detail=_binding_detail(role, stations)))
    return requests


@router.post("/api/staff/role-requests/decide", response_model=OkResult)
async def decide_role_request(body: RoleRequestDecision, request: Request, who: SessionAuth,
                              c: ContainerDep, store: StoreDep) -> OkResult:
    """The manager's answer. Approving a Manager ask activates that person's usual job; declining clears it.
    Approving a custom job makes its slot claimable; declining retires it and clears its owner's default."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    caller = _manager_on_shift(request, c, who.tenant_id)
    tenant_store = staff_store(c)
    if body.request_id.startswith("manager:"):
        staff_id = body.request_id[len("manager:"):]
        default = tenant_store.default_of(who.tenant_id, staff_id, _tenant_defs(c, who.tenant_id, predefined))
        if default is None or default.slot_id != "manager" or not default.pending:
            raise NotFound(f"unknown role request {body.request_id!r}")
        tenant_store.set_default(who.tenant_id, staff_id, "manager" if body.approve else None)
        return OkResult()
    if body.request_id.startswith(si.CUSTOM_PREFIX):
        role = tenant_store.decide_custom_role(who.tenant_id, body.request_id[len(si.CUSTOM_PREFIX):],
                                               body.approve, caller.id, c.clock.now_real_ms())
        if role is None:
            raise NotFound(f"unknown role request {body.request_id!r}")
        return OkResult()
    raise NotFound(f"unknown role request {body.request_id!r}")


@router.post("/api/staff/default", response_model=StaffPerson)
async def set_default(body: SetDefaultRequest, request: Request, who: SessionAuth, c: ContainerDep,
                      store: StoreDep) -> StaffPerson:
    """Set a person's usual job (the manager on shift only). A manager-set job is active at once — setting
    it is the approval. ``slot_id: null`` clears the usual job. 404 for an unknown person or slot."""
    predefined, _stations = await _base(c, store, who.tenant_id)
    _manager_on_shift(request, c, who.tenant_id)
    tenant_store = staff_store(c)
    defs = _tenant_defs(c, who.tenant_id, predefined)
    if tenant_store.person(who.tenant_id, body.staff_id, defs) is None:
        raise NotFound(f"unknown staff member {body.staff_id!r}")
    if body.slot_id is not None and all(d.slot_id != body.slot_id for d in defs):
        raise si.slot_not_found(body.slot_id)
    tenant_store.set_default(who.tenant_id, body.staff_id, body.slot_id)
    return tenant_store.person(who.tenant_id, body.staff_id, defs)  # type: ignore[return-value]
