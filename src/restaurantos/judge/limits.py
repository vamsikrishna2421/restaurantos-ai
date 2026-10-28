"""Session grades and the judge-mode limits (CONTRACTS_API §2, ARCHITECTURE_OPS §16, MASTER_PLAN §5; P30a).

**Grades.** A session's caps level and spend pool. Its sandbox row carries the same pair (``tenants.caps_level`` and
``spend_pool``), which is what the spend scope, the cookie and ``SessionInfo`` read:

| Who | Level | Pool | Sandbox cap | Phone number shown |
|---|---|---|---|---|
| Default (no passcode) | ``default`` | ``public`` | ``sandbox_usd_cap`` ($0.40) | the public lane's |
| Judge passcode | ``passcode`` | ``judge`` | ``sandbox_usd_cap_passcode`` ($1.50) | the judge lane's |
| Admin (``ros_admin``) | ``passcode`` | ``admin`` | ``sandbox_usd_cap_passcode`` | the judge lane's |

A grade only ever goes up on a live sandbox (``Grade.outranks``): a passcode or an admin cookie upgrades a default
sandbox in place, nothing downgrades one.

**Limits.** P11's process-wide buckets (``security.ratelimit``): a refusal is 429 ``rate_limited`` with
``Retry-After`` and ``details.limit``, and takes nothing.

| What | Key | Limit |
|---|---|---|
| A new default sandbox | IP + User-Agent hash; IP (/64); the public pool | 5/h; 20/h; 150/day |
| A new passcode sandbox | IP (/64); the judge pool | 20/h; 100/day (no per-IP+UA limit) |
| (both through ``ratelimit.sandbox_creation``, all-or-nothing) | | |
| A new admin sandbox | IP (/64) | the passcode bucket's 20/h; no pool count |
| A passcode checked without a new sandbox (a wrong one, or an upgrade) | IP (/64) | the passcode bucket's 20/h |
| A sandbox reset (a re-clone under the store's single write lock) | sandbox | 12 per 10 min; admins exempt |
| A call arm | sandbox | 10/h |

Charging every wrong passcode to the passcode bucket bounds guessing at 20 tries an hour per address (/64), and
once that bucket is empty the right passcode waits too, so a guess can never be confirmed early.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from restaurantos.contracts.common import CapsLevel, PhoneLane, SpendPool, TenantInfo
from restaurantos.security import ratelimit
from restaurantos.security.ratelimit import Limit

if TYPE_CHECKING:
    from restaurantos.config import Settings

SANDBOX_RESET = Limit("sandbox_reset", 12, 600)
CALL_ARM = Limit("call_arm", 10, 3600)
_POOL_RANK: dict[str, int] = {"public": 0, "judge": 1, "admin": 2}


@dataclass(frozen=True, slots=True)
class Grade:
    """A session's caps level and spend pool."""

    level: CapsLevel
    pool: SpendPool

    def __post_init__(self) -> None:
        if self.level not in get_args(CapsLevel) or self.pool not in _POOL_RANK:
            raise ValueError(f"unknown grade {self.level!r}/{self.pool!r}")

    def outranks(self, other: Grade) -> bool:
        """True when this grade spends from a bigger pool (or at a higher level) than ``other``."""
        return (_POOL_RANK[self.pool], self.level == "passcode") > (_POOL_RANK[other.pool], other.level == "passcode")

    @property
    def lane(self) -> PhoneLane:
        """The phone lane whose number this grade is shown: the public one for default sessions only."""
        return "public" if self.pool == "public" else "judge"


PUBLIC = Grade("default", "public")
JUDGE = Grade("passcode", "judge")
ADMIN = Grade("passcode", "admin")


def grade_of(tenant: TenantInfo) -> Grade:
    """The grade a tenant row carries."""
    return Grade(tenant.caps_level, tenant.spend_pool)


def sandbox_cap_usd(settings: Settings, level: CapsLevel) -> float:
    """The sandbox spend cap for a level (CONTRACTS §2.1): a display float, never persisted or hashed."""
    return settings.sandbox_usd_cap_passcode if level == "passcode" else settings.sandbox_usd_cap


def tenant_key(tenant_id: str) -> str:
    return f"tenant:{tenant_id}"


def creation(grade: Grade, ip: str, user_agent: str | None) -> None:
    """One new sandbox of ``grade`` from ``ip`` (POST /api/session; P39's onboarding publish counts as one too)."""
    if grade.pool == "admin":
        ratelimit.limiter().check((ratelimit.SANDBOX_PASSCODE, ratelimit.ip_key(ip)))
    else:
        ratelimit.sandbox_creation(grade.level, ip, user_agent)


def passcode_attempt(ip: str) -> None:
    """A passcode checked without creating a sandbox: a wrong one, or an upgrade of a live sandbox."""
    ratelimit.limiter().check((ratelimit.SANDBOX_PASSCODE, ratelimit.ip_key(ip)))


def reset(tenant_id: str) -> None:
    """One re-clone of the sandbox (POST /api/session/reset by a session; admins are not counted)."""
    ratelimit.limiter().check((SANDBOX_RESET, tenant_key(tenant_id)))


def arm(tenant_id: str) -> None:
    """One "Call now" on the sandbox (POST /api/judge/call/arm)."""
    ratelimit.limiter().check((CALL_ARM, tenant_key(tenant_id)))
