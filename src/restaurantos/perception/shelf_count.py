"""The shelf-count service (P38; ARCHITECTURE_VOICE_MEDIA §10, ARCHITECTURE "Shelf count (P1-7)", CONTRACTS_API #43):
photo → Muse Spark count/point grounding → SAM 3.1 per concept → consensus → a pending count → a person confirms →
``inventory.adjusted`` → the GM (``gm.trigger`` opens a ``count_drift`` incident or re-plans; nothing here plans).

- **count(auth, photo)**: the kitchen's countable ingredients (``spark.countable``), the chaos drill, the photo limit,
  then the photo's turn (``Turns``), one Spark call, then one SAM call per concept concurrently (none for a phrase two
  items still share, ``spark.shared_concepts``), then ``consensus.item`` per concept. The result is kept as a pending
  count and returned as ``ShelfCount``; nothing is written yet.
- **Turns** (``Turns``): one photo per session and at most ``COUNTS_AT_ONCE`` across the app are with the models at
  a time; the others wait their turn. A photo is up to 6 MB, and ``MetaClient`` serialises each call's body on the
  event loop (about 0.13 s per call for 6 MB), so this bounds the memory and the loop time that uploads sent straight
  to the API can take. For the same reason the SAM calls start ``SAM_STAGGER_S`` apart: the loop serves other
  connections between them instead of after all four.
- **Failures**: the chaos drill, a used-up budget, an open breaker, a timeout, a Meta error or an unreadable Spark
  answer mean no concepts at all: 503 ``ai_unavailable`` with ``details.fallback = "manual"`` (enter the count by hand).
  Meta's 400 on the photo itself is 400. A SAM call that fails or cannot be read costs only that concept its SAM
  count: Spark's stands, never as agreement.
- **Pending counts** (``PendingCounts``): in memory per tenant under a new ``shc_`` id, kept ``PENDING_TTL_MS`` (15
  min) and swept on every access; each holds the photo's hash and, per ingredient, both counts, the agreement and the
  unit snapshot. Never the photo. A restart forgets them: take the photo again.
- **confirm(auth, id, counts, inventory)**: the id must be one of this kitchen's pending counts (404 otherwise, which
  says nothing about other kitchens), every ingredient must be in that count and every count 0–999 (400). The count is
  then used up, and each ingredient goes through ``InventoryService.adjust(..., "count", AdjustEvidence(photo_sha256,
  spark_count, sam_count, agreed), actor=auth.role)`` with ``on_hand`` = count × 1 (``ea``) or × ``pack_size``.
- **New-ingredient proposals** (pass 13): the Spark call also names countable stock the kitchen does not list
  (``spark.read_proposals``); the pending count keeps them, and ``confirm``'s ``new_ingredients`` accepts any of
  them by name (a name the photo never proposed is 400). Accepting creates the ingredient through
  ``InventoryService.add_ingredient`` with the counted quantity as its opening stock, in the same confirm.
  Proposals get no SAM pass: there is no catalog entry yet for a second count to agree with.
- **The photo limit** (``PhotoLimit``): at most ``PHOTO_LIMIT`` photos per ``PHOTO_WINDOW_MS`` per session (an admin
  by name), a sliding window on the container's real clock; it is checked after the photo passed intake and the drill,
  just before the first model call, so a refused upload costs nothing. 429 ``rate_limited`` with ``Retry-After``.
- **Spend** (``call_ctx``): a sandbox pays from its own scope and cap (by its caps level) as ``judge`` data; the live
  and demo kitchens, where only an admin is a manager, from the admin pool as ``staff`` data. About $0.015 a photo.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from restaurantos.contracts.api import ShelfCount, ShelfNewIngredient, ShelfProposal
from restaurantos.contracts.common import CallCtx, is_sandbox
from restaurantos.contracts.errors import BadRequest, ChaosDown, NotFound, RateLimited
from restaurantos.contracts.events import AdjustEvidence
from restaurantos.contracts.ids import new_id
from restaurantos.meta.errors import CircuitOpen, MetaAPIError, MetaError
from restaurantos.perception import consensus, sam, spark
from restaurantos.perception.responses import MANUAL_FALLBACK, failure_reason, unavailable
from restaurantos.security.ratelimit import session_key_for
from restaurantos.services.container import require
from restaurantos.store import repo as store_repo

if TYPE_CHECKING:
    from restaurantos.config import Settings
    from restaurantos.contracts.common import TenantInfo
    from restaurantos.contracts.inventory import Ingredient
    from restaurantos.contracts.ws import InventoryItemView
    from restaurantos.perception.image import Photo
    from restaurantos.security.auth import Auth
    from restaurantos.services.container import Container
    from restaurantos.services.inventory import InventoryService

log = logging.getLogger(__name__)

ID_PREFIX = "shc"
PENDING_TTL_MS = 15 * 60_000
MAX_PENDING_PER_TENANT = 24  # the oldest goes first past this
PHOTO_LIMIT = 6
PHOTO_WINDOW_MS = 10 * 60_000
COUNTS_AT_ONCE = 2
SAM_STAGGER_S = 0.001


# ----- pending counts --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingItem:
    ingredient_id: str
    unit: str
    pack_size: int
    spark_count: int | None
    sam_count: int | None
    agreed: bool


@dataclass(frozen=True)
class PendingCount:
    id: str
    tenant_id: str
    created_ms: int
    photo_sha256: str
    items: Mapping[str, PendingItem]  # by ingredient id, in the count's order
    proposals: tuple[ShelfProposal, ...] = ()  # countable stock this kitchen does not list yet (pass 13)


class PendingCounts:
    """Counts waiting for a person, per tenant, for ``ttl_ms`` (real time); every access sweeps the expired ones."""

    def __init__(self, now_ms: Callable[[], int], ttl_ms: int = PENDING_TTL_MS) -> None:
        self._now_ms = now_ms
        self.ttl_ms = ttl_ms
        self._by_tenant: dict[str, dict[str, PendingCount]] = {}

    def __len__(self) -> int:
        return sum(len(counts) for counts in self._by_tenant.values())

    def sweep(self) -> int:
        """Drop every count older than ``ttl_ms``; returns how many went."""
        now, dropped = self._now_ms(), 0
        for tenant_id in list(self._by_tenant):
            counts = self._by_tenant[tenant_id]
            for count_id in [k for k, count in counts.items() if now - count.created_ms > self.ttl_ms]:
                del counts[count_id]
                dropped += 1
            if not counts:
                del self._by_tenant[tenant_id]
        return dropped

    def put(self, count: PendingCount) -> None:
        self.sweep()
        counts = self._by_tenant.setdefault(count.tenant_id, {})
        counts[count.id] = count
        while len(counts) > MAX_PENDING_PER_TENANT:
            del counts[next(iter(counts))]

    def get(self, tenant_id: str, count_id: str) -> PendingCount:
        """This kitchen's pending count; 404 when unknown here or expired."""
        self.sweep()
        found = self._by_tenant.get(tenant_id, {}).get(count_id)
        if found is None:
            raise NotFound("No such shelf count in this kitchen, or it expired (counts wait 15 minutes): take the "
                           "photo again", details={"count_id": count_id[:40]})
        return found

    def pop(self, tenant_id: str, count_id: str) -> PendingCount:
        found = self.get(tenant_id, count_id)
        counts = self._by_tenant[tenant_id]
        del counts[count_id]
        if not counts:
            del self._by_tenant[tenant_id]
        return found


# ----- the photo limit -------------------------------------------------------------------------------------------


class PhotoLimit:
    """At most ``limit`` photos per ``window_ms`` per key: a sliding window of the times each key was let through."""

    def __init__(self, now_ms: Callable[[], int], limit: int = PHOTO_LIMIT, window_ms: int = PHOTO_WINDOW_MS) -> None:
        self._now_ms = now_ms
        self.limit = limit
        self.window_ms = window_ms
        self._seen: dict[str, deque[int]] = {}

    def take(self, key: str) -> None:
        """Let one photo through for ``key``, or 429 ``rate_limited`` with the seconds until the oldest one ages out."""
        now = self._now_ms()
        for other in [k for k, times in self._seen.items() if not times or now - times[-1] >= self.window_ms]:
            del self._seen[other]  # whole windows that aged out
        times = self._seen.setdefault(key, deque())
        while times and now - times[0] >= self.window_ms:
            times.popleft()
        if len(times) >= self.limit:
            wait_s = max(1, math.ceil((times[0] + self.window_ms - now) / 1000))
            minutes = self.window_ms // 60_000
            raise RateLimited(f"You can count up to {self.limit} photos every {minutes} minutes. Try again in about "
                              f"{math.ceil(wait_s / 60)} min, or enter the count by hand.",
                              details={"limit": "shelf_photo", "max": self.limit, "window_s": self.window_ms // 1000,
                                       "fallback": MANUAL_FALLBACK}, retry_after_s=wait_s)
        times.append(now)


class Turns:
    """One photo per session and at most ``slots`` across the app with the models at a time; the rest wait."""

    def __init__(self, slots: int = COUNTS_AT_ONCE) -> None:
        self._slots = asyncio.Semaphore(slots)
        self._sessions: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, int] = {}  # photos per session holding or waiting for its lock

    def __len__(self) -> int:
        return len(self._sessions)

    @asynccontextmanager
    async def turn(self, key: str) -> AsyncIterator[None]:
        lock = self._sessions.setdefault(key, asyncio.Lock())
        self._holders[key] = self._holders.get(key, 0) + 1
        try:
            async with lock, self._slots:
                yield
        finally:
            self._holders[key] -= 1
            if not self._holders[key]:
                del self._holders[key], self._sessions[key]


# ----- the service -----------------------------------------------------------------------------------------------


def call_ctx(tenant: TenantInfo, auth: Auth, settings: Settings) -> CallCtx:
    """Whose budget a photo spends (see the module docstring)."""
    session_id = auth.session.sid if auth.session is not None else None
    if is_sandbox(tenant.id):
        cap = settings.sandbox_usd_cap_passcode if tenant.caps_level == "passcode" else settings.sandbox_usd_cap
        return CallCtx(tenant_id=tenant.id, session_id=session_id, trace_id=new_id("tr"), data_class="judge",
                       spend_scope=tenant.id, spend_pool=tenant.spend_pool, scope_cap_usd=cap)
    return CallCtx(tenant_id=tenant.id, session_id=session_id, trace_id=new_id("tr"), data_class="staff",
                   spend_scope="pool:admin", spend_pool="admin")


class ShelfCountService:
    """One per app (``api.routes_perception``): the pending counts, the photo limit and the turns live here."""

    def __init__(self, c: Container) -> None:
        self.c = c
        self.pending = PendingCounts(c.clock.now_real_ms)
        self.limit = PhotoLimit(c.clock.now_real_ms)
        self.turns = Turns()

    async def _kitchen(self, tenant_id: str) -> tuple[TenantInfo, list[Ingredient]]:
        repo = self.c.repo if self.c.repo is not None else store_repo
        async with require(self.c, "store").read() as conn:
            return await repo.tenant(conn, tenant_id), await repo.ingredients(conn, tenant_id)

    def _unavailable(self, exc: BaseException) -> Exception:
        retry = math.ceil(self.c.settings.breaker_cooldown_s) if isinstance(exc, CircuitOpen) else None
        return unavailable(failure_reason(exc), retry_after_s=retry)

    async def count(self, auth: Auth, photo: Photo) -> ShelfCount:
        """REST #43 (upload): both models on one photo, kept as a pending count (see the module docstring)."""
        tenant, stock = await self._kitchen(auth.tenant_id)
        allowed = spark.countable(stock)
        if not allowed:
            raise BadRequest("Nothing this kitchen stocks is counted one by one or in packs, so a photo cannot count "
                             "it", details={"reason": "nothing_countable"})
        try:
            self.c.chaos.check(tenant.id, "meta")
        except ChaosDown as exc:
            raise self._unavailable(exc) from None
        key = session_key_for(auth)
        self.limit.take(key)
        async with self.turns.turn(key):
            found, sams, proposals = await self._models(call_ctx(tenant, auth, self.c.settings), photo, allowed)
        items = [consensus.item(f, s) for f, s in zip(found, sams, strict=True)]
        kept = {f.ingredient.id: PendingItem(f.ingredient.id, f.ingredient.unit, f.ingredient.pack_size,
                                             row.spark_count, row.sam_count, row.agreed)
                for f, row in zip(found, items, strict=True)}
        pending = PendingCount(id=new_id(ID_PREFIX), tenant_id=tenant.id, created_ms=self.c.clock.now_real_ms(),
                               photo_sha256=photo.sha256, items=kept, proposals=tuple(proposals))
        self.pending.put(pending)
        log.info("shelf count %s for %s: %d item(s), %d agreed, %d without SAM, %d new proposal(s)",
                 pending.id, tenant.id, len(items), sum(row.agreed for row in items),
                 sum(row.sam_count is None for row in items), len(proposals))
        return ShelfCount(id=pending.id, items=items, proposals=proposals)

    async def _models(self, ctx: CallCtx, photo: Photo, allowed: list[Ingredient],
                      ) -> tuple[list[spark.SparkItem], list[sam.SamCount | None], list[ShelfProposal]]:
        """Spark's items and new-ingredient proposals, then SAM's count per known item (see the module
        docstring for what fails how). Proposals get no SAM pass: there is no catalog entry yet for a
        second count to agree with."""
        try:
            found, proposals = await spark.count_photo_full(self.c.meta, ctx, photo, allowed)
        except MetaAPIError as exc:
            if exc.status == 400:  # Meta could not decode the image
                raise BadRequest("Muse Spark could not read this photo; take it again",
                                 details={"field": "image", "reason": "bad_image"}) from None
            raise self._unavailable(exc) from None
        except (MetaError, spark.SparkUnreadable) as exc:
            raise self._unavailable(exc) from None
        shared = spark.shared_concepts(found)
        async with asyncio.TaskGroup() as group:  # ``_sam`` never raises, so one concept never cancels the others
            calls = []
            for item in found:
                calls.append(group.create_task(self._sam(ctx, item.concept, photo, shared=item.concept in shared)))
                await asyncio.sleep(SAM_STAGGER_S)
        return found, [call.result() for call in calls], proposals

    async def _sam(self, ctx: CallCtx, concept: str, photo: Photo, *, shared: bool = False) -> sam.SamCount | None:
        """SAM's count for one concept, or None when the call failed, its answer could not be trusted, or another
        item shares the phrase (no call: one count cannot be split between two ingredients)."""
        if shared:
            return None
        try:
            return await sam.count_concept(self.c.meta, ctx, concept, photo.data_url)
        except MetaError as exc:
            log.warning("sam: no count for one concept (%s)", failure_reason(exc))
        except sam.SamUnreadable as exc:
            log.warning("sam: no count for one concept: %s", exc)
        except Exception:  # a parser failure must not cost the other concepts their counts
            log.exception("sam: reading one concept's answer failed")
        return None

    async def confirm(self, auth: Auth, count_id: str, counts: Mapping[str, int],
                      inventory: InventoryService, *,
                      new_ingredients: Sequence[ShelfNewIngredient] = (),
                      actor: str | None = None) -> list[InventoryItemView]:
        """REST #43 (confirm): the person's counts become ``inventory.adjusted(count)`` (see the module
        docstring). Accepted new-ingredient proposals (pass 13) join the catalog in the same confirm,
        their opening stock the counted quantity; their views ride along in the returned list (an id
        the caller did not know before is a creation)."""
        pending = self.pending.get(auth.tenant_id, count_id)
        if not counts and not new_ingredients:
            raise BadRequest("Confirm at least one count", details={"field": "counts"})
        if unknown := sorted(set(counts) - set(pending.items)):
            raise BadRequest("Those ingredients are not in this shelf count",
                             details={"field": "counts", "unknown": [u[:40] for u in unknown[:10]]})
        if invalid := sorted(k for k, n in counts.items() if isinstance(n, bool) or not 0 <= n <= spark.MAX_COUNT):
            raise BadRequest(f"A count is a whole number from 0 to {spark.MAX_COUNT}",
                             details={"field": "counts", "invalid": invalid})
        proposed = {" ".join(p.name.lower().split()): p for p in pending.proposals}
        for accepted in new_ingredients:
            if " ".join(accepted.spec.name.lower().split()) not in proposed:
                raise BadRequest("That new ingredient was not proposed by this shelf count",
                                 details={"field": "new_ingredients", "reason": "unknown_proposal"})
            if not 0 <= accepted.count <= spark.MAX_COUNT:
                raise BadRequest(f"A count is a whole number from 0 to {spark.MAX_COUNT}",
                                 details={"field": "new_ingredients"})
        self.pending.pop(auth.tenant_id, count_id)  # used once: a second tap finds nothing
        stamp = actor if actor is not None else auth.role
        views = []
        for ingredient_id, item in pending.items.items():
            if ingredient_id not in counts:
                continue
            evidence = AdjustEvidence(photo_sha256=pending.photo_sha256, spark_count=item.spark_count,
                                      sam_count=item.sam_count, agreed=item.agreed)
            qty = counts[ingredient_id] * consensus.per_count(item.unit, item.pack_size)
            views.append(await inventory.adjust(auth.tenant_id, ingredient_id, qty, "count", evidence,
                                                actor=stamp))
        for accepted in new_ingredients:
            spec = accepted.spec
            opening = accepted.count * consensus.per_count(spec.unit, spec.pack_size)
            views.append(await inventory.add_ingredient(
                auth.tenant_id, name=spec.name, unit=spec.unit, pack_size=spec.pack_size,
                par=spec.par, reorder_point=spec.reorder_point, opening_qty=opening, actor=stamp))
        return views
