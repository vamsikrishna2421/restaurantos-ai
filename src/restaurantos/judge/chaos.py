"""The judge's chaos drill (CONTRACTS_API #37, CONTRACTS_RUNTIME §10.9, ARCHITECTURE §5 V9; P30b).

``TenantChaos`` is the real ``ChaosPort`` (``c.chaos``, provided by ``judge.chaos``): ``check(tenant_id, component)``
raises ``ChaosDown`` while that tenant's drill has the component switched off. It is synchronous (it runs before every
parse, ASR session and TTS stream), so it reads an in-memory mirror of ``tenants.flags.chaos``:

- ``set`` writes the flag and appends ``chaos.state`` in one transaction; after the commit it updates the mirror and
  publishes the tenant-scoped banner (ephemeral ``system.degraded`` with the tenant id and ``chaos: true``, so a drill
  in one sandbox never degrades another, CONTRACTS §5).
- ``refresh`` re-reads one tenant's flags (``judge.rush`` calls it on ``sandbox.reset`` / ``scenario.loaded``, which
  clear them) and ``load`` reads every tenant at startup, so a drill survives a restart.
- Mirror entries carry the ``seq`` they are as of and an older update never overwrites a newer one, whatever order
  concurrent writers finish in. A call without a tenant (dev scripts) is never drilled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import select, update

from restaurantos.contracts.common import ChaosComponent, ChaosFlags, TenantFlags
from restaurantos.contracts.errors import ChaosDown, NotFound
from restaurantos.contracts.events import SystemDegradedPayload
from restaurantos.services.container import require
from restaurantos.store import tables as t

if TYPE_CHECKING:
    from restaurantos.contracts.ports import StorePort
    from restaurantos.services.container import Container

log = logging.getLogger(__name__)

OFF = ChaosFlags()
DRILL_REASON = {True: "chaos drill: switched off for this sandbox", False: "chaos drill over"}


class TenantChaos:
    """``ChaosPort`` over ``tenants.flags.chaos`` (see the module docstring)."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._flags: dict[str, tuple[int, ChaosFlags]] = {}  # tenant -> (seq the flags are as of, flags)

    # ----- ChaosPort ----------------------------------------------------------------------------------------------

    def check(self, tenant_id: str | None, component: ChaosComponent) -> None:
        entry = self._flags.get(tenant_id) if tenant_id else None
        if entry is not None and getattr(entry[1], component, False):
            raise ChaosDown(component, tenant_id)

    def state(self, tenant_id: str) -> dict[str, bool]:
        entry = self._flags.get(tenant_id)
        return (entry[1] if entry is not None else OFF).model_dump()

    # ----- the mirror ---------------------------------------------------------------------------------------------

    def mirror(self, tenant_id: str, flags: ChaosFlags, seq: int) -> None:
        """Record ``flags`` as of ``seq`` unless the mirror already holds something newer."""
        current = self._flags.get(tenant_id)
        if current is None or current[0] <= seq:
            self._flags[tenant_id] = (seq, flags)

    def forget(self, tenant_id: str) -> None:
        self._flags.pop(tenant_id, None)

    def _store(self) -> StorePort:
        return require(self._c, "store")

    async def refresh(self, tenant_id: str) -> ChaosFlags:
        """The tenant's current flags from the store, mirrored; a deleted tenant is forgotten."""
        tn = t.tenants
        async with self._store().read() as conn:
            row = (await conn.execute(select(tn.c.flags, tn.c.last_seq).where(tn.c.id == tenant_id))).one_or_none()
        if row is None:
            self.forget(tenant_id)
            return OFF
        flags = _chaos(tenant_id, row.flags)
        self.mirror(tenant_id, flags, int(row.last_seq))
        return flags

    async def load(self) -> int:
        """Startup: mirror every tenant whose drill is on; returns how many."""
        tn = t.tenants
        async with self._store().read() as conn:
            rows = (await conn.execute(select(tn.c.id, tn.c.flags, tn.c.last_seq))).all()
        drilled = 0
        for row in rows:
            flags = _chaos(row.id, row.flags)
            if flags != OFF or row.id in self._flags:
                self.mirror(row.id, flags, int(row.last_seq))
                drilled += flags != OFF
        if drilled:
            log.info("chaos: %d tenant(s) have a drill on", drilled)
        return drilled

    # ----- the drill switch (REST #37) ----------------------------------------------------------------------------

    async def set(self, tenant_id: str, component: ChaosComponent, on: bool, *, actor: str) -> ChaosFlags:
        """Switch one component's drill for the tenant: ``chaos.state`` in the flags' transaction, then the mirror and
        the tenant-scoped banner. Repeating the current value writes nothing. 404 for an unknown tenant."""
        tn = t.tenants
        async with self._store().write() as tx:
            row = (await tx.conn.execute(select(tn.c.flags, tn.c.last_seq).where(tn.c.id == tenant_id))
                   ).one_or_none()
            if row is None:
                raise NotFound(f"unknown tenant {tenant_id!r}")
            flags = TenantFlags.model_validate(row.flags)
            chaos = flags.chaos.model_copy(update={component: on})
            seq = int(row.last_seq)
            if chaos != flags.chaos:
                stored = flags.model_copy(update={"chaos": chaos}).model_dump(mode="json")
                await tx.conn.execute(update(tn).where(tn.c.id == tenant_id).values(flags=stored))
                seq = (await tx.append_event(tenant_id, "chaos.state", chaos, actor)).seq or seq
        changed = chaos != flags.chaos
        self.mirror(tenant_id, chaos, seq)
        if changed:
            log.info("chaos: %s %s for %s", component, "down" if on else "back", tenant_id)
            self._banner(tenant_id, component, on)
        return chaos

    def _banner(self, tenant_id: str, component: ChaosComponent, on: bool) -> None:
        bus = self._c.bus
        if bus is None:
            return
        payload = SystemDegradedPayload(component=component, state="down" if on else "ok", reason=DRILL_REASON[on],
                                        chaos=True)
        try:
            bus.publish_ephemeral(tenant_id, "system.degraded", payload.model_dump(mode="json"))
        except Exception:  # the drill is committed: a banner that failed to go out never undoes it
            log.exception("chaos: the system.degraded banner for %s failed", tenant_id)


def _chaos(tenant_id: str, raw: object) -> ChaosFlags:
    try:
        return TenantFlags.model_validate(raw).chaos
    except ValidationError:
        log.error("chaos: tenant %s has unreadable flags; treating its drill as off", tenant_id)
        return OFF


def provide(c: Container) -> None:
    """PROVIDERS ``judge.chaos``: replaces the no-op chaos. ``judge.rush``'s startup task loads the mirror."""
    c.chaos = TenantChaos(c)
