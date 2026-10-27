"""Tests for contracts.hashing: canonical JSON, digests and short codes (CONTRACTS §1.5)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

import pytest

from restaurantos.contracts import gm
from restaurantos.contracts.common import Model, TenantClockSpec, utc_iso
from restaurantos.contracts.hashing import canonical, digest, sha256_hex, short_code
from restaurantos.contracts.kitchen import QuoteOffset, StaffTask
from restaurantos.contracts.ledger import GENESIS_HASH, LedgerRow

NY = ZoneInfo("America/New_York")
BIZ = datetime(2026, 11, 7, 18, 42, tzinfo=NY)
REAL = datetime(2026, 11, 7, 23, 44, 5, tzinfo=UTC)  # a whole second: pydantic alone would drop the milliseconds


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


def test_utc_iso_is_the_real_time_wire_and_hash_format():
    assert utc_iso(BIZ) == "2026-11-07T23:42:00.000Z" == canonical(BIZ).decode().strip('"')
    assert utc_iso(datetime(2026, 11, 8, 0, 2, 30, 125000, tzinfo=UTC)) == "2026-11-08T00:02:30.125Z"
    with pytest.raises(ValueError, match="naive"):
        utc_iso(datetime(2026, 11, 7, 18, 42))


def _plan() -> gm.Plan:
    po = gm.CreatePurchaseOrderParams(supplier_id="SUP_PATEL", ingredient_id="ING_PANEER", packs=3, qty=3000,
                                      cost_cents=3300, eta_biz=BIZ + timedelta(minutes=40))
    step = gm.PlanStep(idx=0, candidate_id="C07", action_type="create_purchase_order", target="SUP_PATEL", params=po,
                       tier="T2", decision="pending")
    return gm.Plan(id="pln_1", plan_no=31, incident_id="inc_1", epoch=1, trigger="act", source="ai", role="active",
                   status="proposed", summary="Bridge with 3 kg.", severity="high", confidence="medium", steps=[step],
                   report_digest="ab" * 32, decision_digest="cd" * 32, state_version=88, created_at=REAL,
                   expires_at=REAL + timedelta(minutes=15))


def test_models_hash_like_their_json_form():
    """A model, its JSON dump and its round trip through a JSON column share one digest (§8.1 report, §9 approval
    digest and ledger chain), whatever the zone or precision of its datetimes."""
    row = LedgerRow(tenant_id="sbx_1", seq=1, kind="PROPOSE", actor="gm", policy_hash="p" * 64,
                    prev_hash=GENESIS_HASH, hash="h", ts=REAL, payload={"plan_id": "pln_1", "at": BIZ, "idxs": (0, 2)})
    models = [_plan(), row, StaffTask(id="tsk_1", kind="pickup", note="Patel 3 kg", status="open", created_at=REAL),
              QuoteOffset(id="qo_1", minutes=5, expires_biz=BIZ + timedelta(minutes=60), active=True)]
    for model in models:
        stored = json.loads(model.model_dump_json())
        again = type(model).model_validate(stored)
        assert digest(model) == digest(stored) == digest(model.model_dump(mode="json")) == digest(again), model
    plan = models[0]
    assert digest(plan.steps[0].params_model()) == digest(plan.steps[0].params)
    # the ledger chain hashes the same body whichever dump mode the appender or the verifier uses
    assert canonical(row.model_dump(exclude={"hash"})) == canonical(row.model_dump(mode="json", exclude={"hash"}))
    assert row.payload == {"at": "2026-11-07T23:42:00.000Z", "idxs": [0, 2], "plan_id": "pln_1"}
    assert b'"created_at":"2026-11-07T23:44:05.000Z"' in canonical(models[2])  # real time: utc_iso
    assert b'"expires_biz":"2026-11-07T19:42:00-05:00"' in canonical(models[3])  # business time keeps its offset


def test_canonical_models_keep_the_type_rules():
    class Priced(Model):
        usd: float

    class Tagged(Model):
        tags: set[str]

    with pytest.raises(TypeError, match="float"):
        canonical(Priced(usd=0.05))
    with pytest.raises(TypeError, match="float"):
        canonical({"nested": [Priced(usd=float("nan"))]})
    with pytest.raises(TypeError, match="set"):
        canonical(Tagged(tags={"PNR_BM", "CHK_BIRY"}))  # JSON mode alone would hash an unordered list
