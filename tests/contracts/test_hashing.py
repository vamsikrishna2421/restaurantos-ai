"""Tests for contracts.hashing: canonical JSON, digests and short codes (CONTRACTS §1.5)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts.common import TenantClockSpec
from restaurantos.contracts.hashing import canonical, digest, sha256_hex, short_code


def test_canonical_rejects_floats():
    with pytest.raises(TypeError, match="float"):
        canonical(1.5)
    with pytest.raises(TypeError, match=r"\$\.steps\[1\]\.cost"):
        canonical({"steps": [{"cost": 1}, {"cost": 0.25}]})


def test_short_code_format():
    code = short_code(digest({"plan_id": "pln_1", "steps": [0, 2]}))
    assert re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", code)
    assert short_code("7f3a91c2" + "0" * 56) == "7F3A-91C2"
    with pytest.raises(ValueError):
        short_code("7f3a")


def test_canonical_sorted_compact_utf8():
    assert canonical({"b": 1, "a": [True, None, "x"]}) == b'{"a":[true,null,"x"],"b":1}'
    assert canonical({"te": "పనీర్"}) == '{"te":"పనీర్"}'.encode()  # ensure_ascii=False


def test_canonical_aware_datetimes_become_utc_ms_z():
    biz = datetime(2026, 11, 7, 18, 42, tzinfo=ZoneInfo("America/New_York"))
    assert canonical(biz) == b'"2026-11-07T23:42:00.000Z"'
    real = datetime(2026, 11, 7, 23, 42, 0, 123456, tzinfo=UTC)
    assert canonical(real) == b'"2026-11-07T23:42:00.123Z"'
    # The same instant in different zones hashes the same.
    assert digest({"t": biz}) == digest({"t": biz.astimezone(timezone(timedelta(hours=5, minutes=30)))})
    assert canonical(date(2026, 11, 1)) == b'"2026-11-01"'


def test_canonical_rejects_naive_datetimes():
    with pytest.raises(TypeError, match="naive"):
        canonical({"at": datetime(2026, 11, 7, 18, 42)})


def test_canonical_rejects_sets_and_non_string_keys():
    with pytest.raises(TypeError, match="set"):
        canonical({"codes": {"PNR_BM", "CHK_BIRY"}})
    with pytest.raises(TypeError, match="non-string key"):
        canonical({1: "a"})


def test_canonical_normalizes_enums_tuples_and_models():
    class Color(Enum):
        RED = "red"

    assert canonical({"c": Color.RED, "t": (1, 2)}) == b'{"c":"red","t":[1,2]}'
    spec = TenantClockSpec(mode="sim", anchor_biz_ms=10, anchor_real_ms=20, speed=0)
    assert canonical(spec) == b'{"anchor_biz_ms":10,"anchor_real_ms":20,"mode":"sim","speed":0}'


def test_digest_is_sha256_of_canonical():
    obj = {"plan_id": "pln_1", "state_version": 42}
    assert digest(obj) == hashlib.sha256(canonical(obj)).hexdigest() == sha256_hex(canonical(obj))
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
