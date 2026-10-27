"""Tests for store.tables against the CONTRACTS §3 schema, reflected from a real in-memory SQLite database."""

from __future__ import annotations

import pytest
from sqlalchemy import JSON, Boolean, Float, Integer, Numeric, String, create_engine, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from restaurantos.store import tables
from restaurantos.store.tables import metadata

# Transcribed from CONTRACTS §3. Column notation: name[:T][?] with T = s (TEXT, default), i (INT), j (JSON) or
# b (boolean 0/1), and ? = nullable. tenant_id TEXT NOT NULL is implied for every table except tenants and
# feedback (feedback lists its nullable tenant_id). Index keys are (columns, unique); the partial unique index
# on quote_offsets is checked separately for its WHERE clause.
SPEC: dict[str, dict] = {
    "tenants": dict(
        cols="id name template kind spend_pool created_ms:i last_active_ms:i expires_ms:i? caps_level dtmf_code? "
             "dtmf_armed_until_ms:i? clock:j tax_bp:i menu_version:i last_seq:i ledger_seq:i ledger_head seed_hash? "
             "flags:j",
        pk=("id",), uq={("dtmf_code",)}, ix={(("kind",), False), (("expires_ms",), False)}),
    "menu_items": dict(
        cols="code category station_id price_cents:i is_veg:b spice_level:i names:j aliases:j allowed_modifiers:j "
             "prep_min:i sort:i available:b unavailable_reason? unavailable_source? alternatives:j",
        pk=("tenant_id", "code")),
    "modifiers": dict(cols="code labels:j aliases:j", pk=("tenant_id", "code")),
    "ingredients": dict(
        cols="id names:j unit allergens:j on_hand:i reserved:i par:i reorder_point:i pack_size:i",
        pk=("tenant_id", "id")),
    "recipe_lines": dict(cols="item_code ingredient_id qty:i", pk=("tenant_id", "item_code", "ingredient_id")),
    "suppliers": dict(
        cols="id name kind lead_min:i cutoff_local next_day_local? min_order_cents:i catalog:j notes:j",
        pk=("tenant_id", "id")),
    "staff": dict(cols="id name role languages:j station_ids:j on_shift:b", pk=("tenant_id", "id")),
    "stations": dict(cols="id names:j parallel_slots:i default_language sort:i", pk=("tenant_id", "id")),
    "orders": dict(
        cols="id order_no:i channel status language session_id cart_id call_id? caller_hash? caller_last4? "
             "subtotal_cents:i tax_cents:i total_cents:i pickup_quote_min:i? ready_at_biz_ms:i? ai_cost_micros:i "
             "flags:j idempotency_key created_ms:i placed_biz_ms:i",
        pk=("id",), uq={("tenant_id", "order_no"), ("tenant_id", "idempotency_key")},
        ix={(("tenant_id", "status"), False), (("tenant_id", "placed_biz_ms"), False)}),
    "order_lines": dict(
        cols="order_id line_no:i item_code qty:i modifiers:j unit_price_cents:i status", pk=("order_id", "line_no")),
    "tickets": dict(
        cols="id order_id order_no:i station_id status lines:j channel created_biz_ms:i started_biz_ms:i? "
             "bumped_biz_ms:i? changed:b undo_until_ms:i?",
        pk=("id",), ix={(("tenant_id", "station_id", "status"), False)}),
    "purchase_orders": dict(
        cols="id po_no:i supplier_id ingredient_id packs:i qty:i cost_cents:i status plan_id? step_idx:i? "
             "created_ms:i eta_biz_ms:i",
        pk=("id",), uq={("tenant_id", "po_no"), ("tenant_id", "plan_id", "step_idx")}),
    "staff_notices": dict(
        cols="id staff_id station_id? message message_en language ai_draft? ai_draft_en? ai_draft_status plan_id? "
             "step_idx:i? status created_ms:i delivered_ms:i? seen_ms:i?",
        pk=("id",), ix={(("tenant_id", "status"), False)}),
    "staff_tasks": dict(
        cols="id kind order_id? po_id? note status blocked_reason? plan_id? created_ms:i", pk=("id",)),
    "quote_offsets": dict(
        cols="id incident_id? station_id? minutes:i expires_biz_ms:i plan_id? active:b",
        pk=("id",), ix={(("tenant_id", "incident_id", "station_id"), True)}),
    "incidents": dict(
        cols="id kind ingredient_id? band status plan_epoch:i ai_runs:i opened_biz_ms:i resolved_biz_ms:i? "
             "report:j? report_digest? decision_digest?",
        pk=("id",), ix={(("tenant_id", "status"), False)}),
    "plans": dict(
        cols="id incident_id plan_no:i epoch:i trigger source role model? status body:j state_version:i "
             "decision_digest digest? short_code? created_ms:i expires_ms:i ai_cost_micros:i latency_ms:i?",
        pk=("id",), ix={(("tenant_id", "incident_id"), False)}),
    "approvals": dict(
        cols="id plan_id decision selected_idxs:j digest approver_id approver_role method created_ms:i", pk=("id",)),
    "ledger": dict(
        cols="seq:i plan_id? step_idx:i? kind actor authorizer? payload:j digest? policy_hash idempotency_key? "
             "prev_hash hash ts_ms:i",
        pk=("tenant_id", "seq"), uq={("tenant_id", "kind", "idempotency_key")},
        ix={(("tenant_id", "plan_id"), False)}),
    "events": dict(
        cols="seq:i type actor payload:j causation_id? correlation_id? ts_ms:i biz_ms:i",
        pk=("tenant_id", "seq"), ix={(("tenant_id", "type"), False)}),
    "handler_cursors": dict(cols="handler last_seq:i", pk=("tenant_id", "handler")),
    "calls": dict(
        cols="id call_sid lane caller_hash caller_last4 started_ms:i ended_ms:i? duration_s:i? order_id? status "
             "meta_cost_micros:i twilio_seconds:i",
        pk=("id",), uq={("call_sid",)}, ix={(("caller_hash", "started_ms"), False)}),
    "turns": dict(
        cols="id session_id call_id? channel turn_no:i transcript utterance_en language intent path reply_text "
             "reply_en spans:j ai_cost_micros:i trace_id created_ms:i",
        pk=("id",), ix={(("tenant_id", "session_id"), False)}),
    "corrections": dict(cols="id order_id line_no:i kind by_role created_ms:i", pk=("id",)),
    "tokens": dict(cols="nonce kind subject exp_ms:i used_ms:i?", pk=("nonce",)),
    "consents": dict(cols="id subject_alias kind text_version granted_ms:i", pk=("id",)),
    "feedback": dict(cols="id tenant_id? session_id? trace_id? page note created_ms:i", pk=("id",)),
}
NO_TENANT_COLUMN = {"tenants", "feedback"}


def _expected_columns(table: str) -> dict[str, tuple[str, bool]]:
    cols: dict[str, tuple[str, bool]] = {} if table in NO_TENANT_COLUMN else {"tenant_id": ("s", False)}
    for token in SPEC[table]["cols"].split():
        nullable = token.endswith("?")
        name, _, kind = token.rstrip("?").partition(":")
        cols[name] = (kind or "s", nullable)
    return cols


def _kind(sql_type) -> str:
    for cls, kind in ((JSON, "j"), (Boolean, "b"), (Integer, "i"), (String, "s")):
        if isinstance(sql_type, cls):
            return kind
    raise AssertionError(f"unexpected column type {sql_type!r}")


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


def test_tables_match_contract(engine):
    insp = inspect(engine)
    assert set(insp.get_table_names()) == set(SPEC) == set(metadata.tables)
    for table, spec in SPEC.items():
        reflected = {c["name"]: (_kind(c["type"]), c["nullable"]) for c in insp.get_columns(table)}
        assert reflected == _expected_columns(table), table
        assert tuple(insp.get_pk_constraint(table)["constrained_columns"]) == spec["pk"], table
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}
        assert uniques == spec.get("uq", set()), table
        indexes = {(tuple(ix["column_names"]), bool(ix["unique"])) for ix in insp.get_indexes(table)}
        assert indexes == spec.get("ix", set()), table

    with engine.connect() as conn:  # autoindexes behind PK/UNIQUE constraints have sql = NULL
        index_sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'index' "
                                      "AND tbl_name = 'quote_offsets' AND sql IS NOT NULL")).scalars().all()
    assert len(index_sql) == 1 and index_sql[0].endswith("WHERE active = 1")


def test_no_real_columns_and_tenant_id_rule():
    for table in metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, (Float, Numeric)), f"{table.name}.{column.name}"
        if table.name in NO_TENANT_COLUMN:
            continue
        assert table.c.tenant_id.nullable is False, table.name
    assert "tenant_id" not in tables.tenants.c and tables.feedback.c.tenant_id.nullable is True


def _offset(**overrides) -> dict:
    row = dict(tenant_id="sbx_1", id="qo_1", incident_id="inc_1", station_id="STN_CURRY", minutes=5,
               expires_biz_ms=1_794_098_520_000, plan_id="pln_1", active=True)
    return {**row, **overrides}


def test_quote_offsets_never_stack_while_active(engine):
    with engine.begin() as conn:
        conn.execute(insert(tables.quote_offsets), [_offset(), _offset(id="qo_2", active=False),
                                                    _offset(id="qo_3", active=False),
                                                    _offset(id="qo_4", station_id="STN_TANDOOR")])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(tables.quote_offsets), [_offset(id="qo_5")])
    with engine.begin() as conn:  # a new active row is fine once the old one is deactivated
        conn.execute(tables.quote_offsets.update().where(tables.quote_offsets.c.id == "qo_1").values(active=False))
        conn.execute(insert(tables.quote_offsets), [_offset(id="qo_5")])
        raw = conn.execute(text("SELECT id, active FROM quote_offsets ORDER BY id")).all()
    assert [tuple(r) for r in raw] == [("qo_1", 0), ("qo_2", 0), ("qo_3", 0), ("qo_4", 1), ("qo_5", 1)]


def test_json_none_is_sql_null_and_not_null_json_fails_loudly(engine):
    incident = dict(tenant_id="sbx_1", id="inc_1", kind="shortage", ingredient_id="ING_PANEER", band="act",
                    status="open", plan_epoch=0, ai_runs=0, opened_biz_ms=1_794_095_000_000, report=None)
    with engine.begin() as conn:
        conn.execute(insert(tables.incidents), [incident])
        assert conn.execute(text("SELECT report IS NULL FROM incidents")).scalar() == 1
        conn.execute(insert(tables.events), [dict(tenant_id="sbx_1", seq=1, type="order.placed", actor="system",
                                                  payload={"order_no": 1042, "te": "పనీర్"}, ts_ms=1, biz_ms=2)])
        assert conn.execute(select(tables.events.c.payload)).scalar() == {"order_no": 1042, "te": "పనీర్"}
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(tables.events), [dict(tenant_id="sbx_1", seq=2, type="x", actor="system", payload=None,
                                                  ts_ms=1, biz_ms=2)])
