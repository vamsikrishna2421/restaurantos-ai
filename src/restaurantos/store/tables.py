"""Database schema (CONTRACTS §3) as SQLAlchemy Core tables. Delivered by P01; used only inside ``store``.

Statements against these tables run only inside ``store.read()`` / ``store.write()`` (CONTRACTS_RUNTIME §10.2);
packages use ``repo`` where a function exists.

- TEXT → ``String``, INT → ``Integer`` (SQLite INTEGER is 64-bit), JSON → ``JSON``, booleans → ``Boolean`` stored
  as 0/1. There are no REAL columns: money is cents, AI cost micros.
- A column marked ``?`` in the contract is nullable; every other column is NOT NULL. JSON columns store Python
  ``None`` as SQL NULL, so a missing value in a NOT NULL JSON column fails loudly.
- Every table except ``tenants`` and ``feedback`` has ``tenant_id`` NOT NULL (``feedback.tenant_id`` is nullable).
- Time columns: ``*_ms`` is real epoch ms, ``*_biz_ms`` business epoch ms (CONTRACTS §1.3).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)

metadata = MetaData()

_JSON = JSON(none_as_null=True)


def _s(name: str, *, null: bool = False) -> Column:
    return Column(name, String, nullable=null)


def _i(name: str, *, null: bool = False) -> Column:
    return Column(name, Integer, nullable=null, autoincrement=False)


def _j(name: str, *, null: bool = False) -> Column:
    return Column(name, _JSON, nullable=null)


def _b(name: str) -> Column:
    return Column(name, Boolean(create_constraint=False), nullable=False)


def _id() -> Column:
    return Column("id", String, primary_key=True)


def _tenant() -> Column:
    return Column("tenant_id", String, nullable=False)


tenants = Table(
    "tenants", metadata,
    _id(), _s("name"), _s("template"), _s("kind"), _s("spend_pool"),
    _i("created_ms"), _i("last_active_ms"), _i("expires_ms", null=True),
    _s("caps_level"), _s("dtmf_code", null=True), _i("dtmf_armed_until_ms", null=True),
    _j("clock"), _i("tax_bp"), _i("menu_version"), _i("last_seq"), _i("ledger_seq"), _s("ledger_head"),
    _s("seed_hash", null=True), _j("flags"),
    UniqueConstraint("dtmf_code", name="uq_tenants_dtmf_code"),
    Index("ix_tenants_kind", "kind"),
    Index("ix_tenants_expires_ms", "expires_ms"),
)

menu_items = Table(
    "menu_items", metadata,
    _tenant(), _s("code"), _s("category"), _s("station_id"), _i("price_cents"), _b("is_veg"), _i("spice_level"),
    _j("names"), _j("aliases"), _j("allowed_modifiers"), _i("prep_min"), _i("sort"), _b("available"),
    _s("photo", null=True),
    _s("unavailable_reason", null=True), _s("unavailable_source", null=True), _j("alternatives"),
    PrimaryKeyConstraint("tenant_id", "code", name="pk_menu_items"),
)

modifiers = Table(
    "modifiers", metadata,
    _tenant(), _s("code"), _j("labels"), _j("aliases"),
    PrimaryKeyConstraint("tenant_id", "code", name="pk_modifiers"),
)

ingredients = Table(
    "ingredients", metadata,
    _tenant(), _s("id"), _j("names"), _s("unit"), _j("allergens"), _i("on_hand"), _i("reserved"), _i("par"),
    _i("reorder_point"), _i("pack_size"),
    PrimaryKeyConstraint("tenant_id", "id", name="pk_ingredients"),
)

recipe_lines = Table(
    "recipe_lines", metadata,
    _tenant(), _s("item_code"), _s("ingredient_id"), _i("qty"),
    PrimaryKeyConstraint("tenant_id", "item_code", "ingredient_id", name="pk_recipe_lines"),
)

suppliers = Table(
    "suppliers", metadata,
    _tenant(), _s("id"), _s("name"), _s("kind"), _i("lead_min"), _s("cutoff_local"), _s("next_day_local", null=True),
    _i("min_order_cents"), _j("catalog"), _j("notes"),
    PrimaryKeyConstraint("tenant_id", "id", name="pk_suppliers"),
)

staff = Table(
    "staff", metadata,
    _tenant(), _s("id"), _s("name"), _s("role"), _j("languages"), _j("station_ids"), _b("on_shift"),
    PrimaryKeyConstraint("tenant_id", "id", name="pk_staff"),
)

stations = Table(
    "stations", metadata,
    _tenant(), _s("id"), _j("names"), _i("parallel_slots"), _s("default_language"), _i("sort"),
    PrimaryKeyConstraint("tenant_id", "id", name="pk_stations"),
)

orders = Table(
    "orders", metadata,
    _tenant(), _id(), _i("order_no"), _s("channel"), _s("status"), _s("language"), _s("session_id"), _s("cart_id"),
    _s("call_id", null=True), _s("caller_hash", null=True), _s("caller_last4", null=True),
    _i("subtotal_cents"), _i("tax_cents"), _i("total_cents"), _i("pickup_quote_min", null=True),
    _i("ready_at_biz_ms", null=True), _i("ai_cost_micros"), _j("flags"), _s("idempotency_key"),
    _i("created_ms"), _i("placed_biz_ms"),
    UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_order_no"),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_orders_tenant_idempotency_key"),
    Index("ix_orders_tenant_status", "tenant_id", "status"),
    Index("ix_orders_tenant_placed_biz_ms", "tenant_id", "placed_biz_ms"),
)

order_lines = Table(
    "order_lines", metadata,
    _tenant(), _s("order_id"), _i("line_no"), _s("item_code"), _i("qty"), _j("modifiers"), _i("unit_price_cents"),
    _s("status"),
    PrimaryKeyConstraint("order_id", "line_no", name="pk_order_lines"),
)

tickets = Table(
    "tickets", metadata,
    _tenant(), _id(), _s("order_id"), _i("order_no"), _s("station_id"), _s("status"), _j("lines"), _s("channel"),
    _i("created_biz_ms"), _i("started_biz_ms", null=True), _i("bumped_biz_ms", null=True), _b("changed"),
    _i("undo_until_ms", null=True),  # real time
    Index("ix_tickets_tenant_station_status", "tenant_id", "station_id", "status"),
)

purchase_orders = Table(
    "purchase_orders", metadata,
    _tenant(), _id(), _i("po_no"), _s("supplier_id"), _s("ingredient_id"), _i("packs"), _i("qty"), _i("cost_cents"),
    _s("status"), _s("plan_id", null=True), _i("step_idx", null=True), _i("created_ms"), _i("eta_biz_ms"),
    UniqueConstraint("tenant_id", "po_no", name="uq_purchase_orders_tenant_po_no"),
    UniqueConstraint("tenant_id", "plan_id", "step_idx", name="uq_purchase_orders_tenant_plan_step"),
)

staff_notices = Table(
    "staff_notices", metadata,
    _tenant(), _id(), _s("staff_id"), _s("station_id", null=True), _s("message"), _s("message_en"), _s("language"),
    _s("ai_draft", null=True), _s("ai_draft_en", null=True), _s("ai_draft_status"), _s("plan_id", null=True),
    _i("step_idx", null=True), _s("status"), _i("created_ms"), _i("delivered_ms", null=True), _i("seen_ms", null=True),
    Index("ix_staff_notices_tenant_status", "tenant_id", "status"),
)

staff_tasks = Table(
    "staff_tasks", metadata,
    _tenant(), _id(), _s("kind"), _s("order_id", null=True), _s("po_id", null=True), _s("note"), _s("status"),
    _s("blocked_reason", null=True), _s("plan_id", null=True), _i("created_ms"),
)

quote_offsets = Table(
    "quote_offsets", metadata,
    _tenant(), _id(), _s("incident_id", null=True), _s("station_id", null=True), _i("minutes"), _i("expires_biz_ms"),
    _s("plan_id", null=True), _b("active"),
)
# Offsets never stack: one active row per (tenant, incident, station), where a NULL station means every station.
# A unique index treats NULLs as distinct, so the key coalesces them to ''. Writers still deactivate the previous
# row (matched with IS) in the same transaction before inserting the new one.
Index("uq_quote_offsets_active", quote_offsets.c.tenant_id, func.coalesce(quote_offsets.c.incident_id, ""),
      func.coalesce(quote_offsets.c.station_id, ""), unique=True, sqlite_where=text("active = 1"))

incidents = Table(
    "incidents", metadata,
    _tenant(), _id(), _s("kind"), _s("ingredient_id", null=True), _s("band"), _s("status"), _i("plan_epoch"),
    _i("ai_runs"), _i("opened_biz_ms"), _i("resolved_biz_ms", null=True), _j("report", null=True),
    _s("report_digest", null=True), _s("decision_digest", null=True),
    Index("ix_incidents_tenant_status", "tenant_id", "status"),
)

plans = Table(
    "plans", metadata,
    _tenant(), _id(), _s("incident_id"), _i("plan_no"), _i("epoch"), _s("trigger"), _s("source"), _s("role"),
    _s("model", null=True), _s("status"), _j("body"), _i("state_version"), _s("decision_digest"),
    _s("digest", null=True), _s("short_code", null=True), _i("created_ms"), _i("expires_ms"), _i("ai_cost_micros"),
    _i("latency_ms", null=True),
    Index("ix_plans_tenant_incident", "tenant_id", "incident_id"),
)

approvals = Table(
    "approvals", metadata,
    _tenant(), _id(), _s("plan_id"), _s("decision"), _j("selected_idxs"), _s("digest"), _s("approver_id"),
    _s("approver_role"), _s("method"), _i("created_ms"),
)

ledger = Table(
    "ledger", metadata,
    _tenant(), _i("seq"), _s("plan_id", null=True), _i("step_idx", null=True), _s("kind"), _s("actor"),
    _s("authorizer", null=True), _j("payload"), _s("digest", null=True), _s("policy_hash"),
    _s("idempotency_key", null=True), _s("prev_hash"), _s("hash"), _i("ts_ms"),
    PrimaryKeyConstraint("tenant_id", "seq", name="pk_ledger"),
    UniqueConstraint("tenant_id", "kind", "idempotency_key", name="uq_ledger_tenant_kind_idempotency_key"),
    Index("ix_ledger_tenant_plan", "tenant_id", "plan_id"),
)

events = Table(
    "events", metadata,
    _tenant(), _i("seq"), _s("type"), _s("actor"), _j("payload"), _s("causation_id", null=True),
    _s("correlation_id", null=True), _i("ts_ms"), _i("biz_ms"),
    PrimaryKeyConstraint("tenant_id", "seq", name="pk_events"),
    Index("ix_events_tenant_type", "tenant_id", "type"),
)

handler_cursors = Table(
    "handler_cursors", metadata,
    _tenant(), _s("handler"), _i("last_seq"),
    PrimaryKeyConstraint("tenant_id", "handler", name="pk_handler_cursors"),
)

calls = Table(
    "calls", metadata,
    # caller_last4 is null for a withheld caller ID, as in call.started and call.bound (CONTRACTS §5)
    _tenant(), _id(), _s("call_sid"), _s("lane"), _s("caller_hash"), _s("caller_last4", null=True), _i("started_ms"),
    _i("ended_ms", null=True), _i("duration_s", null=True), _s("order_id", null=True), _s("status"),
    _i("meta_cost_micros"), _i("twilio_seconds"),
    UniqueConstraint("call_sid", name="uq_calls_call_sid"),
    Index("ix_calls_caller_hash_started_ms", "caller_hash", "started_ms"),
)

turns = Table(
    "turns", metadata,
    _tenant(), _id(), _s("session_id"), _s("call_id", null=True), _s("channel"), _i("turn_no"), _s("transcript"),
    _s("utterance_en"), _s("language"), _s("intent"), _s("path"), _s("reply_text"), _s("reply_en"), _j("spans"),
    _i("ai_cost_micros"), _s("trace_id"), _i("created_ms"),
    Index("ix_turns_tenant_session", "tenant_id", "session_id"),
)

corrections = Table(
    "corrections", metadata,
    _tenant(), _id(), _s("order_id"), _i("line_no"), _s("kind"), _s("by_role"), _i("created_ms"),
)

tokens = Table(
    "tokens", metadata,
    _tenant(), Column("nonce", String, primary_key=True), _s("kind"), _s("subject"), _i("exp_ms"),
    _i("used_ms", null=True),
)

consents = Table(
    "consents", metadata,
    _tenant(), _id(), _s("subject_alias"), _s("kind"), _s("text_version"), _i("granted_ms"),
)

feedback = Table(
    "feedback", metadata,
    _id(), _s("tenant_id", null=True), _s("session_id", null=True), _s("trace_id", null=True), _s("page"), _s("note"),
    _i("created_ms"),
)

# Pass 12 (P44): a restaurant's support queue — a problem report or feature request filed by the
# manager on shift, worked by the platform operators. Like feedback it is kept across a sandbox
# reset (it is a message to the operators, not shift state); the sweep still deletes a sandbox's
# rows with the tenant. Status walks new -> in_progress -> done.
support_items = Table(
    "support_items", metadata,
    _tenant(), _id(), _s("kind"), _s("title"), _s("body"), _s("reporter_id"), _s("reporter_name"),
    _s("reporter_role"), _s("status"), _i("created_ms"), _i("updated_ms"),
    Index("ix_support_items_tenant_status", "tenant_id", "status"),
    Index("ix_support_items_created_ms", "created_ms"),
)
