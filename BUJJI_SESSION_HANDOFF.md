# Bujji session handoff — front door, pass 1 + onboarding, pass 2

> **Update 2026-10-30 (Vamsi's correction, post-pass-2):** the splash no longer cycles greetings
> (Hello / Bonjour / …). Vamsi's "Bonjour" was about the typography, not the word. The front door is now
> just the designed brand: the hat logo (`/favicon.png`, via `BrandHat`) large, with "RestaurantOS AI" as
> the big display wordmark in that same heavy type. No greeting text anywhere on the splash.

Branch: `bujji/front-door` (off `main`). Written 2026-10-30 by Bujji (the assistant builder) so any later
session — Claude Code contributor session included — can pick this work up cold: what shipped, the decisions
behind it, how to run it, and what is deliberately left for later. Pass 2 (restaurant onboarding) is the
first section; pass 1 follows below it.

## Pass 2 — Restaurant onboarding (P39): a restaurant drafts itself from its menu

Built 2026-10-30, same branch, commits `50ed235`…`3d9740a` on top of pass 1. `config/features.yaml`:
**P39 flipped `pending` → `go`** (the routes module now exists and imports cleanly).

### The flow (spec: onboarding is "Step zero", the demo's opening act)

`/onboard` is no longer a placeholder. **Step 1 asks for almost nothing**: the restaurant's name and its
public **Toast ordering-page link** — or, behind a "No Toast page?" toggle, the menu **pasted as text**.
One button: **Build my restaurant draft**. **Step 2 is one review screen**: the whole proposal — menu
grouped by station, station sizes, the shift's job list — with light inline editing only (rename the
restaurant, remove a dish, move a dish to another station via dropdown, step a station's cook count up or
down, give the manager's name). **Go live** posts exactly what the screen shows; the success state names
the restaurant and its tenant id.

### Backend (`services/onboarding.py`, `api/routes_onboarding.py`, contracts in `contracts/api.py`)

- `POST /api/onboarding/draft` (session auth, like every mutating route) → `OnboardingDraft`
  `{restaurant_name, source, items[{name, price_cents, category, station_id}], stations[{id, label,
  parallel_slots}], roles[RoleSlot]}`. Exactly one source or 400. The roles are P41's
  `staff_identity.derive_slots` over the draft stations — the same derivation the live tenant uses.
- `POST /api/onboarding/go-live` → writes the tenant config (below). 409 `tenant_exists`, never overwrites.
- **Toast parsing** (`parse_toast_html`): Toast public pages are Next.js apps, so it reads the embedded
  `__NEXT_DATA__` JSON and walks it — a named dict with a price and no item list is a dish (dollar floats
  and integer cents both handled); a named dict with an item list is a group and becomes the category; an
  item's own subtree is never descended into, so modifier add-on prices are not collected as dishes. If the
  JSON is absent or yields nothing, a best-effort rendered-text scan (headings = categories, `$` prices =
  items) runs. If that also finds nothing → **422 `menu_unparseable`** with a plain-language message. Fetch
  failures (transport error, non-200, >8 MB) → **502 `menu_fetch_failed`**. Nothing is ever fabricated:
  no source items, no draft.
- **Paste parsing** (`parse_menu_text`): headings (`## X`, `X:`, ALL-CAPS) set the category; an item line
  ends in a price (`$12.99`, or a bare trailing `12.99`); description lines are skipped, not guessed.
- **Station inference** (`infer_station`): keyword rules, category hits weighing double name hits, word
  boundaries throughout. biryani/pulao/fried rice → `STN_BIRYANI`; dosa/idli/vada/tiffin/breakfast →
  `STN_TIFFIN`; naan/roti/kebab/tikka/breads → `STN_TANDOOR`; curry/masala/gravy/entrees/mains →
  `STN_CURRY`; 65/manchurian/pakora/samosa/appetizers/starters → `STN_FRY`; drinks/desserts/lassi/chai/
  coffee → `STN_BAR`; no signal → `STN_CURRY`. Tie-breaks favour the more specific station (a masala main
  is Curry, not Tandoor; fried rice is Biryani station, not Fry). It reuses the template's station ids,
  names (all four languages), default languages and modifier sets verbatim (`STATION_DEFS`), so the KDS,
  role slots and `config/i18n_kitchen.yaml` labels all stay consistent.
- **Station sizing**: the template's parallel_slots, +1 (cap 4) for a section with ≥12 dishes, 1 for a
  section of ≤2 dishes. The review screen's steppers override it before go-live.
- Draft dish defaults for the seed menu (drafts, all reviewable later): `is_veg` from a non-veg keyword
  list (default veg), `spice_level` 2 for spicy/masala-style names else 1, `prep_min` per station,
  item codes generated UPPER_SNAKE ≤16 chars.

### What go-live actually does (read this before assuming a tenant is "live")

`publish_tenant` renders **the nine files `seed.loader` reads** — `tenant.yaml`, `stations.yaml`,
`staff.yaml`, `modifiers.yaml` (the template's, verbatim), `menu.yaml`, `ingredients.yaml`,
`recipes.yaml`, `suppliers.yaml`, `demand.yaml` — under `config/tenants/<slug>/` (`<slug>` = slugified
restaurant name; `RESTOS_TENANTS_DIR` overrides the root, as tests use). `staff.yaml` seeds exactly one
person: the manager named on the review screen (`STF_MANAGER`, role `manager`, `en`). Placeholders a
manager replaces in Settings: hours 11:00–22:00, timezone America/New_York, `tax_bp: 0`, one generic
stock ingredient (`ING_MAIN`, every dish's starter recipe = 1 portion) and one placeholder supplier.
Files are rendered into a staging dir, **validated, and only then moved into place**: every file against
the loader's own schemas (`seed.loader.parse_yaml` + the `seed.validate` models) plus the referential
checks of `check_seed` that apply to a generated tenant (known stations/sets/ingredients everywhere,
a recipe per item, demand inside hours and `item_mix` summing to exactly 1 in Decimal, station names equal
to the kitchen labels). A validation failure writes nothing and answers 400 `draft_invalid` with the
problem list.

**The honest limitation:** the runtime seed loader still materializes **only the template tenant**
(`spice_route`) into the served `live`/`demo`/sandbox tenants — `seed.loader.TENANT_DIR` is a constant.
So a newly onboarded restaurant is fully set up on disk in the exact seed format, but the running app does
**not** serve it yet; no fake live tenant is minted and the UI never claims otherwise. Making onboarded
tenants servable = generalizing `load_seed`/`materialize_template` beyond the template (and deciding how
sessions pick a real tenant) — that is the next backend step, deliberately separate from this pass.

**Second limitation:** draft dish names are English-only. The generated `menu.yaml` carries the English
name into the te/ta/es fields (romanizations = lowercased English) and flags the file
`review_status: unreviewed` — the same flag the sample tenant carries for its unchecked translations.
The loader's native-script fluency checks are therefore the one `check_seed` family the go-live validation
deliberately does not run; translating dish names is a review task, not something to fabricate.

### Checks run for pass 2

- Python: `tests/onboarding/test_onboarding_service.py` (parsing against a saved Toast-like
  `__NEXT_DATA__` fixture — no live network — inference table, draft assembly, publish/validate/409)
  and `tests/api/test_onboarding_routes.py` (session gate, paste draft, Toast draft with a monkeypatched
  fetch, honest 502/422, go-live writes + repeat 409) — **30 tests, all passing**; ruff clean.
  `tests/api tests/contracts tests/security tests/meta tests/onboarding tests/seed
  tests/integration/test_registry.py` pass in their own suites. (One combined mega-run showed flaky
  sandbox failures in seed/registry that do not reproduce per-suite — the same proxy-environment flakiness
  pass 1 recorded.)
- Web: full suite **35 files, 242 tests, all passing** (239 from pass 1 + 3 new onboard tests: paste →
  review, review edits are what goes live, honest 422). `npm run build` passes.
- Also fixed a pass-1 leftover: `tests/integration/test_registry.py`'s `EXPECTED_MODULES` now includes
  `api.routes_staff` (pass 1 added the router but not the expectation, so
  `test_registry_tables_match_contract` had failed on this branch ever since).

### Still open after pass 2

1. **Serve onboarded tenants** (loader generalization, above) — the big one; until then onboarding output
   is a validated config directory, not a running restaurant.
2. **Settings surface**: the spec wants everything onboarding created editable later in a Settings area
   with the same structure. Not built; the files are the settings until then.
3. **Menu photos / PDF fallback** (the spec's other sources) — same parse → draft pipeline, needs the
   perception stack; the paste fallback covers the no-Toast case meanwhile.
4. **Modifier extraction** from Toast pages: the draft carries no per-item modifier sets beyond the
   station default. Toast modifier groups are parsed past today, not into the draft.
5. Onboarding endpoints ride the device **session cookie** like the rest of the app (the surface starts
   a session on entry, same as `/staff`). Before a public multi-tenant launch, go-live wants a stronger
   gate than a sandbox session.

---

## Product direction (read this first)

RestaurantOS is a **commercial product**, not a hackathon demo. There is no judge surface, judge language, or
demo framing anywhere in the product UI, and there must never be one: anyone evaluating the product
experiences it the way a restaurant would — open the app, see the brand, choose Customer or Staff, and work.
The old web "Action page" (the judge surface) is deleted in this pass; the backend `src/restaurantos/judge/`
harness package still exists but is invisible from the product and was not touched.

The model the front door encodes:

- **A person is not a role.** Name, language and PIN belong to the person and persist. Jobs belong to the
  shift: a person claims exactly one job per shift, and the same person can be a cook on Tuesday and front
  counter on Friday.
- **One holder per job, per shift — hard rule.** A job slot (e.g. "Biryani Chef 1") has at most one holder at
  a time, enforced in the database, not just greyed out in the UI. If a station needs two people, it defines
  two slots ("Biryani Chef 1", "Biryani Chef 2").
- **Claiming is attendance.** The set of claimed slots is the manager's "who is here now" list.
- **The software drives the human.** The manager's home is an attention view — what needs you, in what
  order — not a passive dashboard of charts.

## What shipped in pass 1

### 1. Splash + mode choice at `/` (`src/web/src/surfaces/welcome/`)

- `/` is now a brand splash: RestaurantOS banner, a greeting that cycles through the languages the product
  speaks (Hello, Bonjour, నమస్కారం, வணக்கம், Hola), one **Enter** button.
- After Enter: **Customer** → `/kiosk`, **Staff** → `/staff`, **Set up a new restaurant** → `/onboard`
  (still the existing placeholder; the automation is pass 2 — see below).
- The router no longer maps `/` to the judge surface; `src/web/src/surfaces/judge/` is deleted. Its shared
  test helpers moved to `src/web/src/test/story-kit.ts` (the recorded-run slot tests import them).
  `src/App.test.tsx`'s two "coming soon" expectations now say `Welcome: coming soon`.

### 2. Staff identity backend — package P41 (`src/restaurantos/services/staff_identity.py`,
`src/restaurantos/api/routes_staff.py`, contracts in `src/restaurantos/contracts/api.py`)

- A **dedicated store**, `<data_dir>/staff.db` (stdlib SQLite), deliberately separate from the core event
  store — no event-store tables were touched. Tables: `staff`, `role_claims`.
- A staff record is: id, tenant, name (unique per tenant, case-insensitive), language (`en`/`te`/`ta`/`es`),
  PIN stored as a **PBKDF2-HMAC-SHA256 hash with a random salt (120k rounds) — never plaintext**.
- On first staff-API access for a tenant, the store seeds itself from that tenant's existing
  `config/tenants/<tenant>/staff.yaml` personas (first listed language wins).
- The tenant always comes from the existing `ros_sess` session cookie — never from a request body. Login
  mints a signed staff token (`security.tokens.sign`, purpose `staff`, 12 h TTL) carrying staff id, name and
  language.
- Endpoints:
  - `GET /api/staff` — the staff list (names + languages only)
  - `POST /api/staff/signup` — `{name, language, pin}` → staff token (`409 name_taken` on a duplicate name)
  - `POST /api/staff/login` — `{staff_id, pin}` → staff token (`403 bad_pin` on a wrong PIN)
  - `GET /api/staff/roles` — every role slot with its holder (optional staff token marks `mine`)
  - `POST /api/staff/roles/claim` — `{slot_id}` (`409 slot_taken`, `409 already_claimed`)
  - `POST /api/staff/roles/release` — end your own shift
  - `POST /api/staff/roles/release-other` — `{slot_id}`; only the current Manager-slot holder may call it
    (`403 manager_required` otherwise)
- **Role slots are derived, not configured**: from the tenant's stations and their `parallel_slots`
  ("<Station> Chef N"), plus one **Manager** slot and one **Front Counter** slot. For Spice Route that is
  14 slots (Manager, Front Counter, Biryani ×2, Curry ×3, Tandoor ×2, Tiffin ×2, Fry ×2, Drinks & desserts ×1).
- Exclusivity is enforced by SQLite unique constraints — one holder per `(tenant, slot)`, one slot per
  `(tenant, person)` — so a double-tap or two devices racing cannot both win a slot.
- Registry: `api.routes_staff`, package `P41`, due `CP4`. TS contracts regenerated
  (`scripts/gen_ts_contracts.py`); only `src/web/src/contracts/generated/api.ts` changed.
- Tests: `tests/api/test_staff_routes.py` (9 tests: seeding, PIN check, slot exclusivity, one-slot-per-person,
  manager release, token checks).

### Seed staff and their PINs (Spice Route sample tenant)

Seed PINs are `1111 × (seed position)`, from `seed_pin()` in `staff_identity.py`:

| Person | Language | PIN |
| --- | --- | --- |
| Lakshmi | Telugu | `1111` |
| Carlos | Spanish | `2222` |
| Meena | Tamil | `3333` |
| Arjun | Telugu | `4444` |
| Maria | Spanish | `5555` |

Anyone else uses **I am new here** on `/staff` and sets their own PIN. These defaults exist so the founders
can sign in on day one; a production pass should force a PIN change on first login (noted under "Left for
later").

### 3. Staff sign-in flow (`src/web/src/surfaces/staff/`, device state in `src/web/src/lib/staff.ts`)

Pick your name (or **I am new here** → name + language + set a 4-digit PIN, entered twice) → PIN pad (touch
and hardware keyboard, auto-submits on the 4th digit) → job picker: open jobs tappable, taken jobs disabled
and labelled "Taken by <name>" → claim and land on the job's home. A device that already holds a shift opens
on a "You are on shift" panel (go to my screen / end shift). The device session (`ros_staff` in localStorage)
holds the staff token + current claim; if the device has no tenant session yet, entering staff mode starts
one.

### 4. Role routing

- **Manager** → `/manager`
- **Chef** → `/kds?station=<their station>`, and the KDS **locks to that station while a chef claim is on
  the device**: station tabs are hidden, `?station=` in the URL is ignored, the header shows
  "Name · Job" and an **End shift** button (releases the slot, signs the device out). No claim on the device
  → the KDS behaves exactly as before (shared expo screens are unaffected).
- **Front Counter** → `/kiosk`

### 5. Manager attention view, v1 (`src/web/src/surfaces/manager/`)

Composed **only** from APIs that exist today — nothing is faked:

- **Shift readiness** (`GET /api/staff/roles`): open jobs first ("Still open — needs someone"), then who
  holds what; the on-shift manager gets a **Release** button per filled slot.
- **Waiting for your approval** (tenant feed): GM plans in `proposed` state, with a link to the GM console.
- **Running low** (tenant feed inventory): items closest to running out, runway-ordered.
- **In the kitchen now** (tenant feed): open-order count and their total value.

**Known API gap:** the orders API exposes *open* orders only, and the ledger endpoint is the GM action
ledger, not sales — so there is **no honest "orders today / revenue today" number** to show yet. The card
says what the data actually is ("In the kitchen now"). If a day-totals endpoint lands, rename the card and
show both.

## How to run

```bash
scripts/dev.sh            # build if needed, serve http://127.0.0.1:8000
scripts/dev.sh --tunnel   # same, plus a Cloudflare quick tunnel — open the printed
                          # https://<name>.trycloudflare.com URL (phone review)
```

Then: splash → Enter → Staff → pick **Lakshmi**, PIN `1111` → claim **Manager** → you are on `/manager`.
Or pick **Carlos**, PIN `2222` → claim **Curry Chef 1** → you land on the locked Curry KDS.

Checks run for this pass:

- Web: `npm --prefix src/web test` — full suite green (34 files, 239 tests), including new suites for
  welcome, staff, manager and the KDS lock, and the updated `App.test.tsx`.
- Web build: `npm run build` (tsc + vite) passes.
- Python: `tests/api/test_staff_routes.py` — 9 passed; ruff clean on all touched Python files.
  Blast-radius comparison, branch vs pristine `main` worktree, same sandbox
  (`tests/api tests/contracts tests/security tests/meta tests/governance`): **identical results** — one
  failure on both, `tests/governance/test_resume.py::test_registry_wiring_boots`, an `httpx.InvalidURL`
  caused by this sandbox's proxy environment, present on `main` before this work. (The full Python suite
  has further pre-existing sandbox failures on `main` in voice/replay areas this branch does not touch;
  the full-suite run is also extremely slow in this sandbox and was not used as the gate.)

## Design decisions worth knowing before you change things

- **Why a separate staff.db?** People and shift claims are operational state, not business events; bolting
  tables onto the event store would have meant touching the store's invariants for a greenfield feature.
  If claims later need to appear in the event feed (e.g. a "shift started" notice), emit events *from* the
  staff service — don't move the store.
- **Why derive slots from stations?** The role map a restaurant needs falls out of its menu/stations; the
  manager should never have to design it from scratch. Onboarding (pass 2) proposes stations from the menu,
  and the slots follow automatically.
- **Claims have no clock.** A claim ends on end-shift or a manager release — there is no business-day
  rollover yet, because the sandbox clock and real shifts don't agree on "today". Decide deliberately.
- **The staff token is a bearer token in localStorage** — the same trust model as the rest of this app
  (tenant from the session cookie). Fine for the current deployment shape; revisit before any public
  multi-tenant launch.

## Left for later (deliberately, in order)

1. **Onboarding automation — pass 2. DONE (see the pass-2 section at the top of this file).** Toast link
   or pasted menu → draft → one review screen → validated tenant config written. What remains of the
   original item: serving onboarded tenants (loader generalization), the Settings surface, and the
   menu-photo/PDF source fallback.
2. **Kiosk voice panel docked right.** Voice ordering exists on the kiosk; the permanent right-side panel
   arrangement from the product sketch is a layout pass on `src/web/src/surfaces/kiosk/`.
3. **Manager attention v2.** Rank the feed harder: fold in open GM incidents and live station load
   (service pressure), so the top card is always the single most urgent thing; add the day-totals card when
   the API can answer honestly (see the gap above).
4. **Person language → surface language.** A staff record's language is stored and returned, but the KDS
   language still follows the station toggle. Wiring the signed-in person's language as the default is a
   small follow-up.
5. **Force PIN change on first login** for seeded accounts before any real deployment, and consider a
   manager-set PIN reset.
