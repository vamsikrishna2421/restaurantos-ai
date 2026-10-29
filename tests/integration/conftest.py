"""Integration-test options. ``--bless`` (lead only) regenerates the golden canonical replay's model fixtures
(``tests/fixtures/ai_cache/golden/``, $0, no network) and rewrites ``tests/fixtures/golden/head.txt`` from this run
(P41a; ARCHITECTURE §6.9); it needs a BUILD_LOG entry saying why the head moved::

    .venv/bin/python -m pytest tests/integration/test_golden_canonical.py --bless

pytest registers an option only from a conftest it loads at startup, so name this folder or a file in it.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--bless", action="store_true", default=False,
                     help="lead only: regenerate tests/fixtures/ai_cache/golden/ and rewrite tests/fixtures/golden/"
                          "head.txt from this run (add a BUILD_LOG entry)")
