# External orders — getting orders from another system into RestaurantOS (pass 18)

RestaurantOS runs one ledger: every order the kitchen cooks, the stock watch deducts, and the
manager reports on lives in the same place, no matter where it was placed. Pass 18 adds the
generic pipe that lets an outside system — a point-of-sale, an online ordering page, a delivery
marketplace — put its orders into that ledger as first-class orders.

The pipe is deliberately generic. There is no vendor client in this pass: no Toast API, no
Square API, no write-back of order status to any outside system. A partner (or an adapter the
restaurant controls) sends the documented shape below; anything vendor-specific plugs in later
as a thin adapter that translates into this shape.

## Authenticating

Each restaurant has **one ingestion token**, created or rotated by the manager in
**Manager → Settings → Outside orders**. The token is shown once, at creation; the server keeps
only its SHA-256 hash. Requests carry it in either header:

```
X-Ingestion-Token: ros_ingest_…
Authorization: Bearer ros_ingest_…
```

The token identifies the restaurant: an order sent with restaurant A's token can only ever land
in restaurant A. Rotating the token (same Settings card) replaces it immediately — the old one
stops working. A request with a missing token is `401 ingestion_token_required`; an unknown one
is `401 bad_ingestion_token`.

## Sending one order — `POST /api/ingest/orders`

```json
{
  "source": "toast",
  "external_order_id": "T-1001",
  "channel": "online",
  "placed_at": "2026-11-02T22:55:00Z",
  "customer_name": "Asha Rao",
  "customer_phone": "+1 774 555 0142",
  "items": [
    { "item_code": "CHK_BIRY", "qty": 2 },
    { "item_name": "Mango Lassi", "qty": 1, "unit_price_cents": 499 }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `source` | yes | The sending system's name (`toast`, `square`, `web-store`, …). Part of the dedupe key and shown to staff. |
| `external_order_id` | yes | The order's id in the sending system. Part of the dedupe key. |
| `channel` | no | A free label for where it was taken (`online`, `counter`, `delivery`). Shown next to the source. |
| `placed_at` | no | When the order was placed there (ISO 8601). Kept on the record; RestaurantOS books the order at the moment it arrives, in the restaurant's own business time. |
| `customer_name`, `customer_phone` | no | Stored on the ingestion record only — never in the audit trail, never in the AI layer. The phone is only ever shown masked (last four digits). |
| `items[].item_code` | one of the two | The menu item's code is the reliable key. |
| `items[].item_name` | one of the two | Or the item's exact name, in any language the menu carries it (the seeded menu's romanizations count too). |
| `items[].qty` | yes | Quantity. |
| `items[].unit_price_cents` | no | What the guest paid there, for reference. RestaurantOS always prices the order from its own menu — the ledger never takes a sender's totals. |

### The answer

`201` with the outcome: `status` is `placed` (or `duplicate`, see below), the RestaurantOS
`order_id` and `order_no`, each line resolved to its menu item, the `unmapped` lines with their
reason, and the totals computed from the menu. A re-delivery returns `200` with
`status: "duplicate"` and the original order.

### Idempotency — send it twice, it lands once

Deliveries are deduplicated on **(restaurant, source, external_order_id)**. A retry, a webhook
redelivery, or a re-sent CSV row returns the existing order and changes nothing: no second
order, no second stock deduction. Callers should retry freely on network errors.

### Item mapping and unmapped lines

Each line is matched by **code first, then exact name** (case-insensitive, across all the
menu's languages). A line that matches nothing — or matches an item the kitchen had 86'd that
day — does **not** fail the order and never touches stock: it is recorded on the order as
unmapped with a reason (`not_on_menu` or `unavailable`), flagged on the order
(`unmapped_items`), listed in Settings → Outside orders, and raised on the manager's
Attention feed so a human can reconcile it. Mapped lines cook, deduct and report exactly like
any other order's.

### Audit

Every delivery appends an `order.ingested` event to the restaurant's audit trail with the
source, the external id, the outcome (`received`, `partial` when some lines were unmapped, or
`deduped`) and the unmapped item labels — never a customer name or phone number. Placed orders
also carry the usual `order.placed` event, actor `ingestion`.

## CSV import — `POST /api/ingest/orders/csv`

For systems that export rather than push: post the CSV as the raw request body with the same
ingestion token. One row per order **line**; rows sharing `(source, external_order_id)` are one
order. Columns:

```
external_order_id,source,channel,placed_at,customer_name,customer_phone,item_code,item_name,qty,unit_price_cents
T-1001,toast,online,2026-11-02T22:55:00Z,Asha Rao,+1 774 555 0142,CHK_BIRY,,2,1699
T-1001,toast,online,2026-11-02T22:55:00Z,Asha Rao,+1 774 555 0142,,Mango Lassi,1,499
```

The answer reports each distinct order's outcome (`placed` / `duplicate` / `error` with a
reason) and counts them, so a bad row never sinks the rest of the file. Re-importing the same
file is safe — every order dedupes.

## Reading what arrived — `GET /api/ingest/orders`

A manager session reads the recent arrivals (newest first, phone numbers masked, unmapped
lines included). `GET /api/ingest/token` reports whether a token is configured;
`POST /api/ingest/token/rotate` creates or replaces it and returns the new value once.

## Explicitly out of scope

- **No status write-back.** RestaurantOS never tells the outside system an order was fired,
  ready or picked up. The flow is one-way by design: ingestion alone already gives complete
  stock, forecast and analytics truth.
- **No vendor integration.** No Toast/Square/etc. client ships in this pass; partner access
  (e.g. Toast's) plugs in later as an adapter emitting the shape above.
- **No storage migration.** Orders land in the same single-file store as everything else.
