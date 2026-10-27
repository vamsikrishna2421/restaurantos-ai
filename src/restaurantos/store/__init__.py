"""SQLite WAL store: single-writer transactions with an event outbox, reads, repo and sandboxes (P04).

- ``store.db``: ``Store`` (CONTRACTS_RUNTIME §10.2): ``write()`` / ``read()``, the commit hook, ``events_since``,
  handler cursors and ``backup()``; ``store.sandbox`` clones, resets and sweeps judge sandboxes.
- ``store.events``: ``Tx`` and its ``append_event`` (per-tenant ``seq``, real and business time).
- ``store.repo``: the fixed read API (``RepoPort``); the module itself is ``c.repo``.
- ``store.tables``: the schema (P01).

``provide(c)`` is the PROVIDERS entry (CONTRACTS_API §7). It sets ``c.store`` and ``c.repo`` only; the lifespan then
installs the commit hook (``services.wiring.commit_fanout``). Imports stay lazy so ``import restaurantos.store`` (for
``store.tables``) stays cheap and cycle-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from restaurantos.services.container import Container


def provide(c: Container) -> None:
    """Open the store on ``settings.db_path`` (WAL, schema created if missing) and wire the repo module."""
    from restaurantos.store import repo
    from restaurantos.store.db import Store

    settings = c.settings
    repo.set_timezone(settings.tenant_timezone)
    c.store = Store(settings.db_path or settings.data_dir / "ros.db", c.clock, tz=settings.tenant_timezone)
    c.repo = repo  # type: ignore[assignment] - the module satisfies RepoPort
