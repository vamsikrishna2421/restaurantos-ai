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

## Pass 3 — Visual redesign of the front door (branch `bujji/ui-polish`)

Built 2026-10-30 on top of pass 2 (branch point `b324c3b`, the tree that merged to main). Vamsi's verdict on
passes 1–2 was that the UI "looks like a science project, not a billion-dollar application" — flat bordered
cards, no depth, no craft. Pass 3 is **presentation only**: no route, API call, navigation target, role/slot
rule, onboarding behaviour, or copy meaning changed, and the full web suite (35 files / 242 tests) passes
unmodified — no test needed editing.

### Design approach: deepen the system, don't replace it

The "kitchen at night" theme in `src/web/src/index.css` was already rich (glow shadow family, orange→gold
brand gradient, AI violet) — the surfaces just weren't using it. Pass 3 exploits it and adds only what was
missing:

- **`--shadow-pop` / `--shadow-lift`** — plain-black depth shadows, so elements sit at different *heights*
  (raised keys, tiles, steppers) and cards earn a deeper shadow on hover. Black reads as height; the coloured
  glows stay reserved for meaning (tone, brand, urgency).
- **`--animate-rise` / `--animate-pop`** — transform/opacity-only entrances; every use is behind
  `motion-safe:`, so reduced motion gets the finished state instantly. No blur filters anywhere (the radial
  "light pools" on the welcome and onboard-done screens are `radial-gradient` backgrounds, painted once).
- **A soft vignette** closes the body background at the foot of the page; selection colour is now the accent.
- `cx.ts` learned that `pop`/`lift` are shadow sizes, so a caller's shadow still overrides a component's glow.

### The kit (`src/web/src/components/ui/`)

- **Button** — coloured variants bloom from the small glow to the full glow on hover and press down a pixel
  (`motion-safe:active:translate-y-px`); `secondary` is now a gradient panel with a pop shadow; radii grew one
  step at every size. Padding/sizes unchanged, so kiosk/KDS touch targets are identical.
- **Card** — panels get a top-lit gradient (`panel-2` → `panel`) and hairline-white header/footer dividers
  instead of the flat fill and hard `line` border. The toned left edge and glow contract is untouched.
- **Chip** — a selected toggle wears the primary gradient with its glow (staff language pills reuse this
  treatment locally for their selected state).
- **Tabs** — the selected tab gets a faint sheen and its count badge lights up in accent.
- **BrandMark** — the hat's corner radius is now a share of its size (app-icon proportions at any size) and
  "AI" wears the orange→gold gradient wherever the brand appears, including the router header.
- **Avatar (new)** — initials on a gradient disc; the tint is a hash of the person's name, so someone is the
  same colour on every surface and shift. Decorative; the name beside it stays the accessible text.
- **Badge — deliberately unchanged.** It lives in dense GM/KDS rows where flat and quiet is the job.

### The surfaces

- **Welcome (`/`)** — the cover: the designed hat sits in a gradient app-icon tile with the kit glow, over
  three soft radial pools of the palette's light (orange above, blue left, violet right). The wordmark is
  display type (6xl/7xl) with the gradient on "AI". Enter blooms to the full accent glow. The three ways in
  are raised cards, each with a tinted icon tile (orange Customer, violet Staff, gold Set up), lifting on
  hover and staggered in on arrival. Back is a quiet pill.
- **Staff (`/staff`)** — people are identity cards (Avatar, bold name, language as a designed chip, chevron,
  hover lift). The PIN screen leads with the person's avatar; pad keys are raised gradient discs that press
  in, and the dots fill with the brand gradient and glow. Job slots carry a tinted icon tile per kind (crown
  = manager, chef's hat = chef, bell = front counter); an open slot shows a lit green "Open", a taken one is
  dimmed with the holder's avatar and a lock. Signup inputs get real focus rings (accent border + soft ring);
  errors sit in a proper tinted alert box instead of bare red text.
- **Manager (`/manager`)** — the feed now carries urgency as tone: shift readiness glows **warn** while any
  job is open and **ok** when the shift is full (the filled-count chip matches), open jobs sit on amber tiles,
  filled ones carry holder avatars; approvals wear the **AI violet** with a count chip and plan summaries on
  soft violet tiles; low-stock rows get urgency dots that escalate with the forecast band (`watch` → amber,
  `act` → red) and the card's tone follows the worst item; the open-orders count is a display-size number in
  the brand gradient. Calm states keep the same polish — an all-quiet day still looks like a product.
- **Onboard (`/onboard`)** — a two-step progress rail (Menu → Review: current step lit in the brand gradient,
  finished step green-checked, `aria-current="step"`). Step 1's form sits in a lit card under the hat tile;
  the Toast link is the hero input (link icon in the field, focus ring). The review screen: dish prices in
  gold tabular figures, station selects and remove buttons with real focus/hover states, cook steppers as
  round raised keys with a display-size count, job chips carrying their kind's icon. Going live lands on a
  ceremony — a glowing green check (`animate-pop`) over soft pools of green/warm light, the restaurant's
  facts set like a receipt, tenant ID as a gold mono chip.

### Deliberately left alone

- **Kiosk, KDS, GM and the remaining surfaces** — not redesigned; they inherit the kit upgrades (gradient
  cards, deeper buttons, lit tabs, gradient brand mark), which were checked against them via the full suite.
  KDS paint cost is unchanged: no new filters, shadows are the same box-shadow family.
- **Modal and Toast** — self-styled already; untouched.
- **All copy** — every string is the pass-1/2 wording. The only new words in the product are the step rail's
  "Menu" / "Review" labels.

## Pass 4 — Onboarded restaurants go live, kiosk voice rail, no gate, tap-to-talk (branch `bujji/pass4`)

### The limitation this pass removes

Pass 2's go-live was files-only: it wrote a validated tenant config under `config/tenants/<slug>/`, but the
runtime seed loader was hardwired to the template tenant (`spice_route`) and materialized only
template/live/demo — an onboarded restaurant was never served. Pass 4 generalizes the loader and wires
go-live to activate, plus three kiosk changes from the founder's live review.

### Tenant selection (the mechanism)

- `seed.loader.load_seed(root, tenant_id, tenants_dir=…, validate_scripts=…)` loads ANY tenant: its nine
  files come from the tenants root (`RESTOS_TENANTS_DIR`, the same root onboarding writes to; default the
  repo's `config/tenants/`), labels and the quiet scenario from the config root. `spice_route` remains the
  default everywhere; its seed hash is unchanged (hash inputs are the same logical paths), so Spice Route
  behavior is bit-for-bit what it was.
- Which tenant a deployment serves is `seed.loader.resolve_template_tenant`, in order: the data dir's
  `active_tenant` file (written by go-live via `set_active_tenant`, atomic replace) → `Settings.template_tenant`
  (`RESTOS_TEMPLATE_TENANT`) → the env var when no settings object exists → `spice_route`. A candidate counts
  only while its tenant directory exists; a stale one is skipped with a log line, never an error and never
  served.
- One check family is a switch now (`check_seed(..., scripts=)`): the native-script checks. They are enforced
  on the strict path for every tenant, and on the serving path (`load_tenant_seed`) for the template tenant
  only — an onboarded tenant's dish names are English-only by construction (the generated files flag them
  `review_status: unreviewed`), the exact waiver go-live's own validation applied in pass 2. Every other check
  (schemas, references, demand, labels) runs on both paths; a broken reference still fails loudly.
- `kitchen_labels()` no longer loads a whole tenant to reach the labels: they are one shared config file and
  are parsed from it directly (cached by content hash). Snapshot rendering never depends on a tenant existing.

### What go-live does now

`POST /api/onboarding/go-live` = publish the validated config (unchanged) → `seed.materialize.activate_tenant`
materializes the new seed in the RUNNING app (the tenant becomes the deployment's template row; `live`/`demo`
are rebuilt from its seed, same hash-idempotent write as boot) → the tenant is recorded as the active tenant.
The result contract gained `status: "live"` and the success screen says **"{name} is live"** with "This app
now serves your restaurant — your menu, kitchen stations and shift jobs are what customers and staff see from
here on." No restart is involved. If activation itself fails, the answer is an honest 500 `activation_failed`
(the config stays on disk; the previous restaurant keeps serving; a restart with
`RESTOS_TEMPLATE_TENANT=<slug>` activates it then) — go-live never claims live when it is not.

- **Sessions**: minting clones from the resolved tenant, so a Start after go-live opens on the new
  restaurant. Devices already mid-shift keep the sandbox they cloned — clones are per-device and are never
  yanked. A sandbox cloned from a non-template tenant starts `quiet`: the scripted scenarios
  (`dinner_rush`) are written against the sample menu, so the clone starts as a fresh restaurant at opening
  instead (deterministic, and logged).
- **Staff follows automatically**: the staff routes read personas and stations from the session tenant's own
  rows, and staff.db seeds per tenant from them — the onboarded manager (the go-live `STF_MANAGER` person)
  signs in with the seed PIN for the first persona (**1111**, the usual rule) and the role slots derive from
  the NEW tenant's stations. Proven end-to-end in `tests/api/test_onboarding_routes.py` (draft → go-live →
  roster → PIN login → slots with no Spice Route station).
- **Other consumers**: sandbox clones load their own template's seed; inventory forecasts take the demand
  curve from the tenant's own template (`repo.template_id` is the new read); the order agent's fallback menu
  and the GM situation defaults resolve through the selection (see the limit below). Boot materialization
  uses the same resolution, so a restart serves the recorded tenant too.

Plainly-stated limits:

- GM situation defaults (`gm/situation._seed_defaults`: demand curve, closing time, zone for report math)
  resolve via `RESTOS_TEMPLATE_TENANT`/default only — that pure module has no settings/data-dir access.
  After a UI go-live in a process without the env var set, those report defaults still read the sample
  tenant's until the env is set; the served menu, staff, orders and forecasts are unaffected.
- Re-onboarding the same restaurant name still 409s (unchanged from pass 2); a failed activation's recovery
  is the restart named above, not a re-submit.

### Kiosk layout: voice panel docked right

On `lg` the kiosk grid is two rows: left column = scrolling menu over the cart (cart capped at 45% height,
its own scroll); the TalkPanel spans both rows as a persistent right rail — full height, stretched card, its
own scroll — staying in view while menu and cart scroll. On phones the stack keeps the previous order (talk,
cart, menu) via order classes; nothing covers the menu. TalkPanel, CartPanel and `/ws/voice` unchanged.

### Kiosk: the voice-start gate is removed (founder change)

Entering the kiosk (Customer mode, and Front Counter sign-ins routed to `/kiosk`) used to land on a modal
"Order with your voice / Tap to start" dialog over an inert, blurred menu; the founder hit it himself in
review and killed it. Now:

- `AudioGate` and the controller's gate state are deleted. The surface opens directly on the ordering
  interface: `KioskController.start()` runs on mount (session, then voice socket; concurrent calls share
  one attempt), nothing is `inert`, and the menu's "appears once you start" placeholder is gone.
- The microphone is the only thing that waits: the first talk tap starts the capture (that tap is the
  gesture the browser's permission prompt requires) and listening begins the moment the mic runs
  (`wantTalk`). Denied/unsupported mics show the existing inline explanation, typing is offered, the talk
  button disables, and tap ordering keeps working — it never depended on the mic.
- Playback keeps its gesture unlock without a gate: every guest tap (talk, menu, qty, confirm, send)
  creates/resumes the AudioContext — context only, no microphone — so a tap-ordered reply is heard too.
- Failures are notices, not blockers: a failed open shows a warning banner and the talk tap retries it; a
  gone session (1006 + probe) clears the cart, shows "This kiosk session ended. A fresh one is starting…"
  and restarts fresh by itself, once per episode (the guard resets on `ready`).

### Kiosk: tap-to-talk replaces hold-to-talk (founder change)

- One tap starts listening: the button reads "Tap to stop", gains `aria-pressed`, a pulsing live dot and an
  elapsed timer (`data-testid="talk-elapsed"`, m:ss). A second tap stops and sends — the same stop + tail
  frames the hold release sent. A recording left open auto-stops at the existing cap (the push-to-talk
  timer: 29 s, before the server's 30 s), and a stop tap under 250 ms still cancels instead of billing a
  turn (with the hint notice, reworded). `PushToTalk` is unchanged — the toggle lives in the kiosk
  controller (`toggleTalk`) — and the other surfaces that share it keep their hold controls.
- Copy: the panel caption is now the persistent small line **"Nothing is sent until you tap stop."** The
  greeting, button labels, the cart's empty-state line and the monitor's kiosk reference all speak tap, not
  hold; the global Space-hold listener went away with the hold model (the button stays keyboard-operable
  when focused, as a native toggle button).

### Tests

- New Python: loader selection + waiver (`tests/seed/test_tenant_selection.py`), activation read-backs
  (`tests/seed/test_activate_tenant.py`), a sandbox clone of an onboarded template starting quiet
  (`tests/store/test_sandbox.py`), and go-live → staff sign-in end-to-end (`tests/api/test_onboarding_routes.py`).
- Web: 35 files / **242 tests green**, `npm run build` clean. Kiosk suites were re-pointed, not reduced:
  gate tests became direct-entry tests (opens on ordering, first tap starts the mic, failed open is a
  notice the talk tap retries, gone session restarts itself), and the turn tests drive tap/tap.
- Python: the touched suites pass per-suite under the sandbox's clean-env recipe (seed 46, api 53,
  store 29, services/gm/judge 210, onboarding + staff within api, integration 71 standalone). A full
  in-one-process run reported 4231 passed / 2 skipped / 1 xfailed with 3 fixture-setup errors from the
  sandbox's known combined-run flakiness (every suite passes standalone); the only reproducible failure
  anywhere, `tests/test_conftest_network.py::test_localhost_tcp_needs_opt_in`, fails identically on the
  pristine base branch — environmental, not pass 4. Ruff clean on the tree.

## Pass 5 — Usual jobs: a person's role is their default (branch `bujji/pass5`)

Founder redesign from the 2026-10-30 live review: picking a job on every sign-in was wrong. A job is
chosen ONCE and belongs to the person; a shift claim is just the day's instance of it. Plus two fixes the
founder hit himself (the onboarding dead end, and the staff boot spinner).

### The model

- **Default role lives on the person**: a `default_role` column on the staff row in `staff.db`, holding
  JSON `{"slot_id", "pending"}` — the slot id plus whether it still waits on approval (only the Manager
  job uses the stored flag). Resolved for display against the tenant's own slots, so a default whose
  station no longer exists resolves to nothing rather than dangling. `StaffPerson.default_role` carries it
  (`StaffDefaultRole {slot_id, label, pending}`).
- **Predefined jobs** are unchanged: `derive_slots` over the SESSION tenant's stations (pass-4 behavior),
  so defaults and customs are per-tenant like everything else. Seed personas start with the usual job
  their seed role implies: manager/owner → Manager, foh → Front Counter, cook → their FIRST station's #1
  slot (Lakshmi → Manager; Carlos → STN_CURRY#1; Meena → STN_TIFFIN#1; Arjun → STN_TANDOOR#1;
  Maria → Front Counter).
- **Custom jobs** live in a new `custom_roles` table (per tenant): a name (case-insensitive unique among
  live roles — a declined name may be asked for again) and a station binding (a list of station ids,
  possibly empty = floor). Slot id `custom:<role_id>`; its `RoleSlot` carries `station_ids` (predefined
  slots send null) and kind chef when it binds stations, front_counter when it binds none. Orders still
  route by station, never by person — a custom job just subscribes its holder to its stations' queues.
- **Approval**: custom jobs and the Manager job start `pending`. Pending is a state, not a notification:
  the manager on shift clears it from the manager surface (`GET /api/staff/role-requests`,
  `POST /api/staff/role-requests/decide`; request ids are `custom:<role_id>` and `manager:<staff_id>`).
  Declining a custom job retires it and clears its creator's default pointer. The seed manager always
  starts approved, so approvals can always bootstrap.
- **Claim gates** (in `StaffStore.claim`): a pending slot answers 403 `role_pending`; the Manager slot
  and any custom slot are claimable ONLY by the person whose approved usual job they are (403
  `role_not_approved` for everyone else). A caller sees their own pending custom slot in their roles view
  (flagged `pending_approval`, claimable by nobody) and nobody else sees it at all.
- **The manager can move defaults**: `POST /api/staff/default` (Manager-slot holder only) sets or clears
  anyone's usual job; a manager-set job is active immediately — setting it IS the approval. Releasing
  others' slots is unchanged (release-other).

### The flows, as built

- **Signup**: name/language → PIN (twice) → "What's your job?" — the predefined list (the Manager job
  notes it needs approval) or "Something else" (name + station multi-select). One signup call carries
  `{default_slot_id}` or `{custom_role: {name, station_ids}}`. An active predefined pick is claimed at
  once and lands on its screen; a waiting pick lands on the signed-in panel, which names the usual job
  and its "Waiting for the manager's OK" state.
- **Login**: PIN → the usual job auto-claims when set, approved and free → straight to its surface via
  `staffHome`. The picker appears only when the usual job is taken this shift (with the note "Your usual
  job — X — is taken this shift") or the person has no usual job yet; picking a predefined job with no
  default SETS it as the default (server-side, in the claim route; the web mirrors it locally).
- **Switch job for today**: on the signed-in panel — releases the held slot and shows the picker; the
  default never changes. Ends with End shift / Sign out as before.
- **KDS**: a custom claim's `station_ids` drive the display — several stations narrow the tabs to exactly
  those (no All tab, first bound station is home), one station locks like a predefined chef, none (floor)
  keeps every station with the claim chip + End shift shown.
- **Manager surface**: two new cards under Shift readiness — "Job requests" (approve/decline, with the
  binding spelled out, e.g. "Covers Curry, Biryani") and "Team jobs" (a usual-job select per person;
  people with a waiting ask point at the requests card instead). Both fail soft off older servers.

### The two fixes

- **Onboarding dead end**: `/onboard` now carries the staff surface's "← Start" home pill on EVERY state —
  start form, review screen, success state (the review screen keeps its separate "Start over").
- **Staff boot failure**: a rejected session `start()` (e.g. the tunnel's 403 bad_origin) left the store
  status at 'none', so `/staff` spun on "Getting the kitchen ready…" forever. `start()` — and only
  `start()`; the shared `track()` is untouched, so a failed reset still can't kill a live session's
  status — now also sets `status: 'error'`, and the boot screen renders its existing "The kitchen could
  not be reached. Reload to try again." Regression assertion added to the failed-start session test.

### Limits, plainly

- If nobody holds the Manager slot and nobody has an approved Manager default (say the one manager's
  default was cleared from Team jobs before they ended their shift), the slot cannot be claimed and
  approvals cannot be decided — setting defaults requires holding it. Recovery is a seed/data fix, not
  a UI path; noted, not built.
- A custom job's name uniqueness is enforced per tenant among live roles; two simultaneous same-name
  signups race to one 409 and one orphan person row with no default (they can still sign in and pick).
- Nonce-free as before: approvals are decided by whoever HOLDS the Manager slot this shift, which is the
  product's trust model for a single restaurant's tablet.

### Tests

- Python: `tests/api/test_staff_routes.py` is now 19 tests (seed defaults, signup with predefined/custom
  jobs, the full approval circle — approve activates, decline clears — claim gates, first-pick-sticks,
  manager set/clear, per-tenant isolation). Onboarding/GM/contracts/registry suites re-run green (182),
  and a full `tests/` run under the clean-env recipe (NO_PROXY pinned to localhost — the sandbox's IPv6
  NO_PROXY entries crash httpx in unrelated suites) finished with exactly one failure:
  `tests/test_conftest_network.py::test_localhost_tcp_needs_opt_in`, the documented pre-existing
  environmental one. Ruff clean.
- Web: 35 files / **250 tests green** (242 at pass 4 + 8 new: auto-claim login, taken-default picker,
  custom-job signup waiting state, switch-for-today, manager requests + team jobs, KDS bound tabs,
  onboard Start links), `npm run build` clean.

## Pass 6 — the manager console (bujji/pass6, off bujji/pass5 e499155; NOT pushed)

The manager surface stops being a single feed and becomes a console: the attention feed stays the home,
and six sections sit beside it (kit Tabs, deep-linkable as `/manager?section=…`). Everything shown traces
to the tenant's own rows — orders ledger, shift record, staff roster, tenant config — computed by a new
server package and read through one service. No model is called anywhere in this pass: detection is
arithmetic, wording is templated, per the founder's token-economy rule.

### What was built

- **Contracts (P42)** in `contracts/api.py`: `ConsoleOrders/ConsoleOrder`, `ConsoleRevenue`,
  `ConsoleAnalytics` (sellers, station load, `ForecastDaypart`, `CapacityCheck`, `StockCheck`,
  `ConsoleCard`), `ConsoleTimesheets`, `ConsoleSettings`. TS regenerated; only the generated file changed.
- **Domain** (`domain/console.py`, pure functions, no I/O): ledger aggregations (day revenue, hourly
  distribution, item sales, station×daypart load), the forecast, both correlations, templated card text.
- **Service** (`services/manager_console.py`): loads the session tenant's snapshot + orders + shift log,
  derives business days in the tenant zone from the business clock, real-clock local days for shifts.
- **Routes** (`api/routes_manager.py`, registered in the CP4 registry): `GET /api/manager/orders`
  (`?day=`), `/revenue`, `/analytics`, `/timesheets` (`?week=`), `/settings`. SessionAuth; the tenant
  comes from the device session only, never from a parameter. Staffing facts reuse the staff routes'
  own helpers — no second derivation.
- **Shift log** (`staff.db`): a `shift_log` table (tenant/staff/slot/claimed_ms/released_ms). Claims
  append a row; releases close the open row; still-open claims were backfilled once at store open
  (idempotent). The pass-5 claim rows themselves are untouched.
- **Web**: `surfaces/manager/` is now a shell (`index.tsx`) + one file per section + `lib/manager.ts`.
  Attention keeps every pass-5 card (readiness, GM approvals, running low, in the kitchen now) and gains
  the correlation cards at the top. Job requests + Team jobs moved into **People** unchanged (same staff
  endpoints and handlers); People also shows the roster with who is on shift now. **Settings** includes
  the stock-count editor, which writes through the EXISTING `POST /api/inventory/adjust`.

### Where every number comes from

- **Orders**: open orders = ledger rows not in a terminal state, age from the business clock; history =
  one business day's rows (placed that day, or still open from it). Cancelled orders render as cancelled
  and are excluded from every value. The ledger has no voids/refunds and no payment method — neither is
  shown anywhere.
- **Revenue**: non-cancelled ledger totals by business day; today + current week (Monday start), order
  counts, average ticket (null, shown as —, when a day has no orders), today's per-hour distribution.
- **Timesheets**: the shift log. An open shift's minutes run to the moment of the read and it is labelled
  "On shift now". Days/weeks are real-clock local days in the tenant zone (claims stamp real time).
- **Analytics**: trailing 14-day window for sellers (qty desc, top/bottom five; the bottom excludes the
  top when the menu is small) and station×daypart ticket/item counts — all from ledger lines joined to
  the menu snapshot for station names.

### The forecast and the correlations

- **Forecast ladder** (tomorrow, plus the rest of today): with ≥3 same-weekday days in the trailing 28
  days it is "steady" — the same-weekday mean per daypart, blended by a recent-trend factor (last-7-days
  mean ÷ window mean, clamped 0.7–1.4, only with ≥7 history days). With some sales but a thin pattern it
  is "early" (all-history mean). With no sales at all it falls back to the tenant's setup demand curve
  (`demand.yaml`: orders/hour × daypart hours, dishes via `item_mix`) labelled "prior"; a tenant with
  neither gets zeros and says so. Every figure is half-up rounded (house style); sub-half dishes/tickets
  per daypart are dropped. The basis sentence and confidence label are part of the payload — the UI shows
  its working.
- **Capacity correlation**: for today's not-yet-ended dayparts (cooks = currently claimed, capped at the
  station's `parallel_slots`) and all of tomorrow's (cooks = approved usual-job holders for that station),
  expected station tickets (forecast orders × the trailing window's station share) are compared with
  capacity = cooks × daypart hours × **6 tickets/cook-hour** — a documented conservative default in
  `domain/console.py` (menu prep runs 1–14 min, median ≈8; one ticket per cook per 10 min leaves batching
  and expo headroom; deliberately low so the warning comes early). Surfaced in Settings. A shortfall
  generates a card: `bad` when nobody is expected or the shortfall is ≥10 tickets, else `warn`.
- **Burn-down correlation**: forecast dishes × `recipes.yaml` = ingredient need per daypart segment
  (rest of today, then tomorrow); free stock = on hand − reserved (the inventory store's own numbers).
  `projected_stockout` walks the segments spreading burn evenly inside each. The earliest plausible
  restock is the soonest `cutoff_local + lead_min` across suppliers carrying the ingredient (calendar-
  correct across DST). A card trips ONLY when the stockout lands before that restock (or no supplier
  carries it); `bad` when the stockout is today. Everything with an in-horizon stockout is listed in the
  stock watch whether carded or not — against the Spice Route seed, par-stock paneer is watched but
  correctly NOT carded, because tonight's dairy restock lands first.
- **Cards are evaluated when the analytics endpoint is read** (page load / Refresh), keyed deterministically
  (`capacity:<station>:<date>:<daypart>`, `stock:<ingredient>`) so re-reads dedupe. There is NO scheduler
  and no push in this pass: the founder's morning-lookahead / pre-service / live-watch routines are the
  documented next step, to run this same evaluation on a clock.

### Settings, honestly

Profile, menu and stations are **read-only** here. They are tenant config created by onboarding and
applied on activation; the config layer validates whole-tenant writes, not field edits, so a live field
editor would bypass the validation that keeps Spice Route bit-for-bit stable. The section says exactly
that, in product language. Stock counts are the one live edit because the inventory endpoints already
own them safely.

### Limits, plainly

- **Timesheet history has a hard floor**: passes 1–5 deleted claim rows on release, so shifts that ended
  before the shift log existed were never recorded and cannot be reconstructed. The record starts with
  this pass (plus backfilled still-open claims), and the Timesheets payload carries a `note` saying so —
  the section shows it verbatim. Nothing was back-estimated.
- Console reads are SessionAuth (any signed-in device session), matching the staff/orders/inventory read
  posture; writes still go through the existing role-gated endpoints. The single-restaurant-tablet trust
  model is unchanged.
- The GM report's "today" still resolves through env-level tenant selection (pass-4 limit, unchanged);
  the console itself is session-tenant throughout.
- Capacity uses claimed/usual-job staffing — it cannot see a manager's verbal plan, a day off, or a
  no-show; the card wording says "the usual team" / "claimed so far", never a promise.
- Forecast dayparts are the tenant's configured ones; a tenant whose history predates a menu change will
  blend old-menu sales into station shares until the window rolls past it (14 days).

### Tests

- Python: new suites — `tests/domain/test_console.py` 13 (aggregations, forecast ladder incl. trend clamp
  and prior fallback, capacity trip/no-trip, stockout-vs-restock trip logic, half-up rounding),
  `tests/api/test_manager_routes.py` 8 (session-tenant isolation, orders/revenue figures against a seeded
  ledger, the seeded capacity + stock cards, prior-only forecast honesty, timesheet open/completed shifts,
  settings read model). Staff API still 19 (shift log is behaviour-invisible to the old assertions),
  registry green with the new router. Full `tests/` run: only two failures, BOTH proven environmental
  against the unmodified pass-5 base — `test_localhost_tcp_needs_opt_in` (documented pre-existing) and
  `test_approval_executes_within_200ms_while_gm_stream_runs` (a 200 ms wall-clock budget; the base fails
  it identically in isolation in this sandbox, 534 ms vs 568 ms). Ruff clean.
- Web: 36 files / **256 tests** (250 at pass 5 + 6 new section tests in `manager-sections.test.tsx`; the
  two pass-5 manager tests now open the People tab first). Full-suite runs here show 255/256: the one
  failure is `staff.test.tsx > switch_job_for_today_releases_without_ending_the_shift`, a synchronous
  lookup racing an async fetch under parallel load — it fails identically on the unmodified pass-5 base
  in this sandbox and passes standalone on both branches (7/7). `npm run build` clean.
## Pass 7 — customer memory + goodwill engine (P43) — built by Bujji, branch bujji/pass7

The restaurant remembers its customers, and goodwill reaches the manager as a decision. Zero LLM
calls anywhere in this pass: recognition, the guest book, and every goodwill card are statistics
over the order ledger + the customer store, evaluated on read like P42's correlations.

### Surfaces

- **Kiosk checkout capture** (`surfaces/kiosk/CustomerCapture.tsx`, inside the cart panel when the
  cart has lines): optional name + phone with the honest line — "So we can call your name when it's
  ready, and remember your usual. Skip it and order as usual — nothing changes." Identify is
  debounced, fail-soft (a failed save says so and never touches the order), and never gates
  ordering: a guest who skips gets the pass-6 flow byte-for-byte. A returning number surfaces
  "Welcome back, <name> — your usual:" with one-tap add chips (dish · ordered N×), and a pending
  goodwill dish is announced before placement ("Good news — your Masala Chai is on us today…").
  The name is kept with its cart id, so the next guest at the same kiosk never inherits it.
- **Ready-surface callout**: the placed card speaks the name beside the order number ("We'll call
  Asha when it's ready" / "Asha, your order is ready!"), and the KDS ticket card and the bumped
  (ready) rail call the name next to the ticket number. Names are fetched alongside the feed
  (`GET /api/customers/names`, open orders only) and fail soft to numbers-only. The public `/live`
  board deliberately does NOT get names — its projection strips personal data by design.
- **Manager console**: new **Customers** section (guest book: totals, top regulars by visits then
  spend, the drifted, approved dishes still waiting) and **goodwill cards in Attention** next to
  the P42 correlation cards — reason with real numbers, masked phone, the freebie options as
  selectable chips, Put it on the house / Not now for the manager on shift (staff token; the
  server enforces manager-on-shift). The Attention tab badge counts both card kinds.

### Data model — `customers.db` (new store, staff.db idiom; the orders ledger is untouched)

Core store has no column migrations, so identity lives in its own SQLite file per data dir:

- `customers` PK (tenant, phone_key): name, phone as given, `marketing_opt_in` DEFAULT 0
  (schema only — nothing sends marketing anywhere), first/updated stamps.
- `session_links` (append-only): tenant, session_id, phone_key, name snapshot, linked_ms. An
  order belongs to the link live at the order's `created_ms` within its session (latest link at or
  before, else the session's earliest) — so history survives name changes and shared kiosks.
- `goodwill_state` PK (tenant, key): status pending/approved/dismissed, chosen item, decider,
  decided_ms. `goodwill_credits` PK (tenant, credit_id): suggestion key, phone, item, status,
  redeemed_order_id. Phone key = digits as typed, leading US 1 dropped, 7–15 digits (else 400).
  Phones are stored as given (the restaurant's own book, on its own volume), masked as
  `···` + last 4 in every manager surface, and never written to app logs.

### The goodwill engine — `domain/customers.py` + `services/customers.py`, named constants

- Signals, priority incident > lapsed > milestone, one standing suggestion per customer per read:
  (a) **incident** — high-value customer whose order inside 30 days was cancelled, or waited
  ≥ 2.0× the tenant's median wait AND ≥ 30 min (median from trailing 28 business days of
  non-cancelled ready orders; `placed_biz_ms`/`ready_at_biz_ms` support waits — if no median
  exists, cancellations only); high-value = spend ≥ $150.00, and once ≥ 10 identified spenders
  exist the threshold becomes min($150, the 90th-percentile spend).
  (b) **lapsed** — ≥ 3 visits and none in ≥ 14 days.
  (c) **milestone** — the next visit is the 10th, 25th, or 50th.
- Deterministic keys: `goodwill:<phone_key>:incident:<order_id>` / `:lapsed:<last_order_id>` /
  `:milestone:<next_visit_no>` — re-reads reproduce the same suggestion, decisions stick to it.
  Approved keys never re-suggest; dismissed keys cool down 14 days (`DISMISS_COOLDOWN_DAYS`).
- Options: their usual (most-ordered dish), a cheaper other dish from their history, and the
  next-most-ordered other dish ("also loved"). Value is the dish's **menu price, labeled as
  such** — tenant recipes carry quantities only, no ingredient costs exist to label "kitchen cost".

### Redemption — the comped dish rides the order

An approval mints a pending credit keyed to the phone, visible in the Customers view until spent.
At placement (`OrderService.place`), after the cart's lines are priced, a pending credit appends a
zero-priced line for the dish: totals recompute from lines, so the bill never moves; the kitchen
cooks it, inventory reserves it, the order carries the `goodwill_comp` flag, the guest's bill shows
"On the house", and the credit is marked redeemed once placement commits. If the dish is gone or
86'd that day the credit simply waits — an order is never blocked by a goodwill read.

### Deliberate limits (and why)

- **Percentage-off alternate dropped**: `pricing.totals` recomputes the subtotal from
  qty × unit_price; there is no discount representation in the order model, and adding one would
  corrupt ledger math every other surface trusts. Both alternates are dishes instead.
- **Phone-channel callers are not linked**: orders carry `session_id`, but `caller_hash` is always
  NULL today, so there is no honest key to attach a phone order to a guest. The Customers view
  says so in its own note rather than implying coverage it doesn't have.
- **No scheduler, no push**: like P42, everything is evaluated on read; the scheduled routines
  (morning lookahead / pre-service / live watch) remain the documented next step that will carry
  these cards too.
- **Names never touch the ledger**: order events may not carry guest free text (CONTRACTS §5), so
  the callout name travels via the customers store + its own endpoint, never the Order contract.

### Tests

- Python: domain `tests/domain/test_customers.py` (11), routes `tests/api/test_customers_routes.py`
  (7: gates, recognition excluding cancelled orders, callout names, view math + masking + tenant
  isolation, decide→credit→redeem at placement through the real OrderService). Full suite
  (JUnit-exact): **1547 tests — 1541 passed, 3 skipped, 3 failed**, and all three failures are
  wall-clock/sandbox tests, none in pass-7 code: `test_approval_executes_within_200ms_while_gm_stream_runs`
  and `test_localhost_tcp_needs_opt_in` fail identically on the unmodified pass-6 base (19a923d,
  clean worktree; the latter is a sandbox PermissionError raised in conftest itself), and
  `test_kds_fanout_p95_local` passes standalone on BOTH this branch and the base — it flaked only
  inside the loaded full run (the 86-sync sub-1s test did the same in an earlier run and likewise
  passes standalone on both). Ruff clean across src + tests.
- Web: 264 tests / 38 files — new suites: kiosk capture (4), manager customers + goodwill (2), KDS
  guest name (1), manager home grid (1); the five KDS story expectations now include the names read
  (strictness unchanged). Full runs twice passed everything; the final runs flaked only the known
  staff timing test (`switch_job_for_today_releases_without_ending_the_shift`, 263/264) — it is
  intermittent on the unmodified base since pass 6 (documented there), passes standalone on this
  tree, and pass 7's diff over pass 6 touches no staff code at all.
- Demo data seeded into the local sandbox tenant for the screenshots (Asha the regular, Priya the
  lapsed regular, Marco at 9 visits): `files/pass7-shots/`.

### Founder-directed addition — the console's grid home

The manager console now lands on a **Home grid** (the founder's standing ask from the hackathon
plan): one tile per area — Attention, Orders, People, Timesheets, Revenue, Analytics, Customers,
Settings — each a real button in the Card shell carrying a live headline from reads the console
already makes (cautions + goodwill waiting, open orders and their value, jobs filled, on shift
now, today's till, the next daypart's forecast or "No sales history yet", guests remembered +
goodwill waiting, the restaurant's name over "Setup & stock"). The Attention tile takes the bad
tone when any standing card is bad. No new backend: the shell additionally reads the existing
revenue and customers endpoints for two headlines and fails soft on both. The tabs remain the way
around (Home is the first tab, the way back); `?section=` deep links are unchanged; the grid is
2 columns at phone width, 3–4 at tablet/desktop. Tests: a new home-grid suite (all eight headlines
from fixtures + tile navigation), and four existing tests gained the one-tap landing step the new
default requires — the two pass-5 manager tests, the sections correlation test, and the goodwill
decision test, which now enters Attention through its tile.

### Note — the kiosk's look is Pass 8's, not this pass's

Two founder-reported kiosk fixes (the empty cart reserving 45% of the large-screen layout, and the
slashed microphone shown in the idle state) were built during this pass as a separate commit, then
dropped by the founder the same day in favor of a full kiosk redesign (Pass 8, from his supplied
reference design), which will own the kiosk's look, mic-state presentation, and cart layout. The
fix commit and its revert are both on this branch; the shipped tree is exactly the Pass 7 scope
above. For Pass 8: `CustomerCapture` and the placement-time goodwill line are deliberately
self-contained (capture = one component + one endpoint; redemption = one zero-priced line in
`OrderService.place`) so both re-home into the redesigned cart/checkout without surgery.

---

## Pass 8 — the founder's light redesign: kiosk, front door, role picker, KDS (Scopes A–E)

The founder took the kiosk and staff sign-in to ChatGPT and brought back design frames, then
extended the pass live four times. Reference frames (kept with the goal files):
`kiosk-redesign-reference.jpg`, `staff-signin-redesign-reference.jpg`,
`role-picker-redesign-reference.jpg`, `kds-redesign-reference.jpg`. The design language: warm
light theme — cream `#faf5ec`, white cards, soft warm shadows, large radii, deep warm
brown-black text, the pass-3 orange surviving as the action color — applied ONLY to the
customer/staff/kitchen surfaces below. The manager console and GM console stay dark (the
founder explicitly declined a manager redesign); onboarding was not in scope.

### The token approach (one line, because there is nothing else to know)

`.theme-light` in `src/web/src/index.css` re-points the EXISTING design-token names
(`--color-panel`, `--color-text`, `--color-accent`, the shadows, the fonts) inside a scope, so
every kit component (Card, Button, Chip, Notice, the kiosk panels) re-skins with zero forks and
zero new component variants. Light surfaces add `theme-light` to their root; dark surfaces are
untouched. Cream goes a step deeper than the panel tokens (a soft `#fdf8ef → #f7ecda` vertical
wash) so white cards read as cards. Two honest limits: the kit's `Card`/`Button`/`Tabs` carry a
few hardcoded `border-white/8`-style classes from the dark system — inside the light scope their
separation is carried by the warm `shadow-lift` instead, and nobody should "fix" that by editing
the kit (the dark manager depends on those classes as they are).

### Scope A — the customer kiosk, rebuilt to the frame (commit 27774d0)

- **Layout**: brand header (chef-hat logo tile, "RestaurantOS **AI**", tenant name + SAMPLE
  pill, connection state) and ONE language `<select>` (globe + current language) replacing the
  chip row — all five voice languages incl. Auto-detect, same `setLang` behavior. Below `lg`
  the page stacks (Welcome rail, chip-row categories, menu, cart); at `lg` it is the frame's
  3-column grid: sidebar nav / menu + docked cart / Welcome rail.
- **Sidebar categories** with a small lucide line icon per category, the active one a soft
  peach pill. **Category-at-a-time** (a deliberate call, documented in `MenuGrid.tsx`): the
  frame shows one category with its hero; the previous stacked-sections-with-scroll-spy would
  have made the hero meaningless. Category state lifts to the kiosk root so the voice
  controller's jump-to-category (`data-active-category`) drives the same state the taps do.
- **Category heroes**: wide photo, display-size name, one-line tagline per category in warm
  product voice — Biryani "Aromatic. Authentic. Always Special." (the frame's own line); the
  other seven written in the same register (Curries "Slow-simmered, made to comfort.", etc.).
- **Dish cards**: photo-forward (photo, veg/non-veg mark, spice flames, name, price, orange
  circular +), and EVERY behavior carried over — add-to-cart, the 86'd treatment with named
  staff alternatives, dietary marks, in-cart quantity chips. The card root stays a `<div>`: the
  one-click add lives on the + button (full name + price as its accessible name, as the tests
  have always addressed it).
- **Welcome rail** (pass-4's TalkPanel, reskinned): brand chef-hat mascot in a white tile,
  "Welcome!", the greeting copy, the big orange talk button with concentric sound rings and a
  NORMAL mic glyph in the ready state — `MicOff` now renders ONLY when the mic is
  denied/unsupported (the founder's complaint about the old build, fixed). All voice states,
  readback, captions, "Type instead" and the honesty caption are the same controller, untouched.
- **Cart**: the frame shows no cart (empty state), so per the founder-approved call the cart is
  a slim order bar docked at the bottom of the CENTER column (count + total + View order)
  expanding IN PLACE to the full cart — lines, quantities, the pass-7 CustomerCapture, tax,
  Review order. An empty cart renders nothing (no bar). Placing/readback force it open so the
  confirmation is never hidden behind a collapsed bar. The menu keeps the dominant space at
  every cart state; on phone the bar docks to the viewport bottom (fixed) so checkout is never
  below the fold.
- **"Your usual"**: when the capture recognizes a returning guest, the usual becomes a strip
  above the dish grid ("Welcome back, Asha — your usual:" + one-tap chips). The recognition
  state moved up: `CustomerCapture` gained an optional `onRecognized` callback; when the kiosk
  wires it, the in-cart usual block yields to the strip (the capture's standalone use — and its
  pass-7 tests — keep the block). Recognition is forgotten when the cart empties or a new order
  starts, exactly like the cart itself.
- **Banners**: the degraded-TTS banner and the drift/offline notices restyled as slim warm
  notices in the light chrome; behavior unchanged.

### Food photography (commit e616a6e) — 38 generated images, and the no-photo contract

The design lives on photography and the menu had none. 38 images were generated for the Spice
Route sample tenant (30 dishes + 8 category heroes), style-locked: the named dish in a ceramic
bowl/plate, 45° shot, warm natural light, cream background, no text, no people. They live in
the SPA's public tree at `src/web/public/menu-photos/spice_route/` (`<ITEM_CODE>.jpg`,
`hero-<category>.jpg`) and are served by the existing static SPA hosting — no backend asset
route exists or was needed.

The plumbing (commit 74eae36): `MenuItem.photo` is an OPTIONAL field end-to-end (contract,
seed validator, snapshot, generated TS types). It is presentation-only: `menu_hash` whitelists
its fields, so adding photos never moves the prompt cache key. The seed validator rejects any
photo path that is not a same-origin absolute path; onboarding has no writer for the field and
must never fabricate photography for a real restaurant. Heroes resolve BY CONVENTION as
`hero-<category>.jpg` in the same directory as the tenant's dish photos — NOT from the tenant
id (a sandbox's tenant id is not the seed slug; building the path from it 404'd every sandbox
hero in the first screenshot round — fixed in e79ceda). **Fallbacks**: a dish with no photo (or
a photo that fails to load) renders the clean text card; a category with no hero gets the cream
gradient + text treatment. A tenant with zero photography gets the same menu, never a broken
image.

### Scope B — the front door in the light language (commit 6e5d77c, with E1)

Splash, the Customer/Staff/Set-up choice, staff sign-in, the PIN pad and the signup form are
reskinned in the same scope. Staff sign-in follows its frame: top bar with the brand mark and
wordmark, "← Start" pill, display heading, "Who is signing in?", 2-column white identity cards
(colored initial avatar, name, language pill with Telugu/Tamil in their own scripts, chevron),
the "I am new here" hairline divider entry at the bottom, a light PIN pad. All behavior,
routing, role-claim and approval logic from passes 1/5 is byte-identical — reskin and relayout
only; the staff suite passed with zero test changes. (Fix f074479: the surface column is
centered on an inner wrapper so the cream paints the whole viewport — the first screenshots
showed the dark app root as bars down both sides.)

**One frame element deliberately NOT reproduced**: the staff frames show a language selector
in the top bar. There is no app-level language before sign-in — language is per-person
(`session.staff.language`) and drives that person's screens AFTER they sign in (the KDS shot
shows Carlos's board fully in Spanish, unprompted). A selector on the sign-in screen would
control nothing, so the frame's selector was not faked; the kiosk (customer-facing, where
language IS a live choice) has its selector, and the KDS carries the language pills.

### Scope C — manager sections as object grids (commit 1c3a4b0) — FOUNDER-DIRECTED

Added mid-pass by the founder: he opened Orders, saw a single-column list, and rejected it —
"each section's objects as cards in a responsive grid", like the home tiles. **Layout only**;
the dark system, endpoints, data and copy are untouched. Orders (live + history order cards),
People (Job requests stay a decision card ABOVE the grids; Team → person cards — now also
showing each person's language, which the rows never surfaced; Team jobs → control cards),
Timesheets (per-person day cards), Customers (guest cards; copy preserved verbatim, including
the lowercase "last in"), Revenue (week → day cards), Analytics (forecast / top sellers /
station load / capacity / stock watch as grid cards). **Attention was deliberately LEFT as the
ranked single-column feed** — decision cards need the width, and the founder's grid ask was
about object sections; Settings untouched. All 13 manager tests passed with zero changes
(they assert content, not list structure — the behavior freeze held by construction).

### Scope D — the staff exit (commit 433d172) — FOUNDER-REPORTED

The founder signed in as Maria (usual job Front Counter), PIN login auto-claimed it and routed
him to /kiosk — a customer surface holding a staff session with no end-shift, no back, no way
out. Fix: `kiosk/StaffBar.tsx` — when (and only when) an active staff session exists on the
device, the kiosk shows a slim deep-brown staff bar (it reads as staff chrome, not customer
UI): name + this shift's job (or "Signed in" for a session without a claim), and End shift /
Sign out, which does exactly what the manager surface's end-shift does (`staffApi.release`,
fail-soft; `clearStaffSession()`; route to /staff). No session → nothing renders; customers
never see it. The other landings were audited, not assumed: /kds already had End shift for
claim holders (and Scope E2 generalized it — below); /manager already has "End my shift"
(verified in the screenshot, not duplicated); the GM console is a PIN-gated viewer with its own
lock/exit pattern and holds no claim. New tests: `staffbar.test.tsx` (bar shows for a session
and end-shift releases + clears + routes; no session → no bar).

### Scope E — the picker frame + the KDS goes light (commits 6e5d77c, a0c1710)

**E1, the role picker**, joins the front door per its frame: personalized heading ("Hi
<name> — pick your job for this shift"), the jobs as a 2-column grid of white cards — role
icon in a saturated circle (Crown for Manager, Bell for Front Counter, ChefHat for the
kitchens), status line (green dot + "Open"; a filled slot shows its holder's name; waiting /
pending / taken states in the same visual language), chevron on open cards. The person's
USUAL job (their `default_role` slot) gets the frame's highlighted treatment — peach fill,
orange ring, a small "Your usual" pill. The left rail carries the brand chef-hat mark large
over "Great Food Starts with a Great Team" (the brand mark, NOT a copy of the frame's cartoon
characters). Claim behavior, Manager approval gates and routing are exactly passes 1/5.

**E2, the KDS goes light** — this SUPERSEDES the original "KDS keeps dark" line: the founder
supplied a light frame for it, so /kds (root + the waiting screen) joins the cream system via
the same `.theme-light` scope. Header as a white bar: brand mark + "Kitchen", SAMPLE pill,
the sim clock, the Live dot, the session chip ("<Name> · <Job>"), an explicit **End shift**,
the language pills (EN + the tenant's languages — the existing device-language switcher), and
a **Chime toggle**. **Chime decision**: the frame's toggle wires to the EXISTING arrival chime
— the KDS has chimed new tickets since pass 4 behind a persisted per-device preference
(`kds/chime.ts`); the toggle now lives in the header (bell / bell-off) driving that same
preference. No new chime was added. Station title in the display serif with the station accent
demoted to a dot (the big orange identity line fought the light design; the accent survives as
each ticket's top edge + the title dot), "Queue <n> min · <n> open", slim warm degraded
banner, tickets as white cards (shadow carried by `shadow-lift` now that the scope's `panel-2`
is a light warm gray) with every frozen behavior: station lock, bump flow, ready rail, timers,
age chips. **One behavior-adjacent broadening, deliberate**: the session chip + End shift now
show for ANY claim holder, not only locked/bound claims — Scope D's KDS exit, delivered here;
the release path is the same fail-soft release the locked chip always called. The 86'd banner
stays a full red band — that one is an alarm, not chrome. After E2, the only dark surfaces are
the manager console and the GM console, per the founder's explicit call.

### Where the frames fought real behavior (named, not silently deviated)

1. **Staff language selector** — not reproduced (see Scope B): no such state exists pre-sign-in.
2. **Sidebar vs stacked menu** — the kiosk went category-at-a-time (Scope A) rather than the
   old stacked scroll; the frame's hero demanded it, and voice category jumps still land right.
3. **KDS station color** — the frame's calm board can't carry the old full-saturation station
   identity bars; the accent moved to the title dot + ticket top edge (Scope E2).
4. **Session photographs** — the frames' cartoon mascots were not copied anywhere; the brand
   chef-hat mark stands in on the Welcome rail, the picker rail, and the KDS (Scope E).

### Deliberate limits

- Photography exists only for the Spice Route sample tenant. A real onboarded restaurant has
  none until it supplies its own — by design (the fallback IS the design for them).
- The light scope re-points tokens but does not chase the kit's hardcoded dark border classes
  (see token approach) — shadows carry the separation in light surfaces.
- Phone is supported, not designed-first: the kiosk stacks, the cart docks to the viewport
  bottom, the category chips scroll horizontally. Kiosk hardware is the tablet.
- Voice/TTS/ASR behavior is untouched this pass; TTS remains in its degraded no-key state.

### Tests

- Web: 266 tests / 39 files — 265 passed, 1 failed: `staff.test.tsx >
  switch_job_for_today_releases_without_ending_the_shift`, the documented intermittent.
  **Base evidence**: on the UNMODIFIED pass-7 base (fd6c9cc, isolated worktree), the staff
  suite fails the identical test intermittently (2 of 4 runs, then 4 FAIL lines across 6 more
  runs — always that same test); on this branch the same suite passes and fails across runs
  with the same shape (7/7 twice, 6/7 otherwise). No staff behavior was changed by this pass
  beyond classes and the full-viewport wrapper. New tests this pass: `staffbar.test.tsx` (2).
  Kiosk suites were updated only where the STRUCTURE changed (category-at-a-time navigation
  helper, expand-the-cart helper) — no behavior assertion weakened; the confirm test's
  post-order assertion now checks the placed heading is gone AND no order bar remains, because
  an empty cart renders nothing in the new design.
- Python: one full run landed 1552 passed, 3 skipped, 0 failed (1555 collected; clean env);
  a second full run of the same tree showed ONE failure — `test_localhost_tcp_needs_opt_in`
  (Permission error), which is one of the three environmental failures recorded against
  pass 7 (localhost-TCP opt-in, the 200 ms perf test, the KDS fanout p95). It is
  intermittent and environmental, not a pass-8 regression: the only Python change is the
  optional `photo` field + its validator rules + the metadata-only column migration.
- `npx tsc --noEmit` clean; production build rebuilt after every scope.

### Screenshots (goal files, pass8-shots/)

Tablet 1180×820: kiosk (empty cart, cart expanded with capture, your-usual strip, staff bar
as Maria), splash, mode choice, staff sign-in, PIN pad, role picker (Maria), KDS (Carlos,
Curry station, Spanish board), manager Orders grid, manager People grid. Phone 390×844:
splash, kiosk, kiosk cart. Two screenshot-driven fixes came out of reviewing them: the kiosk
grid row-start (e79ceda) and the hero path (same commit), plus the staff full-viewport cream
(f074479) — the tests could not have caught any of the three; look at the pictures.

### Commits (bujji/pass8, local — for the parent to merge)

74eae36 optional menu photo field end-to-end · 0610977 .theme-light scoped tokens ·
e616a6e 38 Spice Route photos · 27774d0 Scope A kiosk · 433d172 Scope D staff bar ·
6e5d77c Scope B front door + E1 picker · 1c3a4b0 Scope C manager grids · a0c1710 Scope E2
KDS light · e79ceda screenshot fixes (grid + hero path) · f074479 staff full-viewport cream ·
this handoff.

## Pass 9 — the console steal: severity, evidence, filters, wired decisions, home greeting (+ Scopes K & L) (branch `bujji/pass9`, off bujji/pass8 21a3577; NOT pushed)

The founder reviewed marketing frames of the manager console and approved stealing specific
pieces into the REAL console. The console stays dark — no restyle, no sidebar, no invented
features. Two founder-directed scopes arrived mid-pass (K: kiosk cart layering; L: stale
on-device menu) and are included with their own commits.

### What shipped

1. **Severity pills on every Attention card.** One mapping for the whole feed, from the tone the
   engine already assigned — nothing is re-scored: `bad` → High, `warn` → Medium, everything
   else → Low. The goodwill tones land incident suggestions at High (their tone is `bad`),
   lapsed regulars at Medium (`warn`), milestones at Low (`ok`) by the same rule. Shift
   readiness derives its tone from the open-jobs condition it already computes; the approvals
   and kitchen-now cards are informational → Low; Running low follows its own band tone.
   Rendered as a small uppercase pill in the card header (`surfaces/manager/attention.tsx`).
2. **Evidence visuals on the cards.** The payload was extended with the checks' already-computed
   values (plumbing, no new math): `ConsoleCard` gains `subject`, `expected_tickets`,
   `capacity_tickets`, `dishes` and `burn_curve`. The stock card's curve is the segment walk
   `projected_stockout` already performs, drawn out (`_burn_curve` mirrors its arithmetic on the
   same needs list; the contract test asserts the curve starts at the watch's free stock and
   lands on zero exactly at its `stockout_biz`). The web draws a compact SVG sparkline (run-out
   point dotted, both ends captioned in words) and an expected-vs-capacity bar pair whose
   expected bar is split at the capacity point so the shortfall segment is the visible part.
3. **Category filters with live counts.** All (n) · Inventory · Staffing · Guests · Operations.
   Mapping: stock cards + Running low → Inventory; capacity cards + Shift readiness → Staffing;
   goodwill → Guests; Waiting for your approval + In the kitchen now → Operations. Counts are
   computed from the same reads the feed renders. Filtering is view-only; the server's rank
   order is preserved inside a filter (pinned by a DOM-order test).
4. **Decision buttons only where a real action exists.** The 86 mechanism EXISTS and is wired:
   `POST /api/kds/availability` (kitchen service `set_availability`; any kitchen role may 86,
   only a manager may un-86). A stock card's payload now names the dishes its run-out ingredient
   blocks (on sale, recipes using it, menu order); the card offers "86 dishes using <subject>"
   with an inline confirm naming every dish, then posts per dish and shows a done state (or an
   honest inline error). Capacity cards get a working "Open People" deep link (section switch).
   Goodwill Approve/Dismiss unchanged; text-only suggestions stay text-only.
5. **Manager home.** A greeting on the business clock — "Good <morning/afternoon/evening>,
   <manager's first name>" — and a Top cautions strip: the feed's top 3 by severity, then feed
   rank, one line each (pill + headline + primary action), tapping through to Attention. Built
   only from the reads home already loads (analytics + goodwill).

### Scope K (founder-directed): the kiosk cart never overlays the dish grid

Verified against the pass-8 layout at tablet 1180×820 and phone 390×844, collapsed and
expanded: the cart bar/card are grid siblings of the menu viewport in the menu column's second
row; expanding the cart shrank the menu viewport 598 → 219 px at tablet size with zero rect
overlap, and `elementFromPoint` at every in-view tile's centre hits the tile, never the panel
(tiles below the menu region are clipped by its own scroll edge — normal scrolling, not
coverage). A new suite (`cart-layering.test.tsx`) pins the structural contract. One real gap
fixed: the post-order "Order placed" card had no height cap or internal scroll — it now
matches the cart card's, so a long order cannot overflow its row.

### Scope L (founder-directed): a returning kiosk drops its stale sandbox menu

Mechanism, proven end to end: the "on-device copy" is the session's sandbox. Its menu is a
clone-time copy of the template tenant; a server menu change re-materializes the
template/`live`/`demo` tenants (`seed/materialize.py`, keyed by `seed_hash`) but never the
sandboxes, so a returning browser's `/api/state` served the pre-photo menu forever while a
fresh profile cloned the new template. Nothing on the client persisted the menu — the
persistence is the `ros_sess` cookie pointing at the stale clone. Fix (client, kiosk bootstrap,
`surfaces/kiosk/menuSync.ts`): once per page load, fingerprint the served menu's seed-owned
content (photos and prices included; availability and the tenant-local `menu_version` excluded —
an 86 is not drift) against the public `live` snapshot (rebuilt from the same seed); on a
mismatch, reset the session — the product's own re-clone — and load the fresh snapshot. An
unreachable server or a refused reset keeps the cached copy serving; staleness only loses to a
reachable, newer server state. Tests cover the stale, current and unreachable cases.

### Tests

- Web: **276 tests / 41 files, all passed** (pass 8: 266). New: manager-sections +5 (pills +
  evidence rendering, filter counts + filtering + rank order, the 86 flow's three posts, Open
  People navigation, greeting + strip order/navigation), `cart-layering.test.tsx` (2),
  `menu-sync.test.tsx` (3). `npx tsc` clean; production build rebuilt.
- Python: full clean-env run — 1547 collected, exactly one failure:
  `tests/test_conftest_network.py::test_localhost_tcp_needs_opt_in` (Permission error), the
  documented environmental failure recorded identically against passes 7 and 8 (it exercises
  the sandbox's localhost-TCP opt-in; pass 9's Python diff is the console payload + contract
  assertions). The manager routes suite is 8/8, including the new payload contract assertions.
  Two process notes for the next builder: (a) a full-suite run WITH the shell's proxy env vars
  set fails spuriously across tests/voice and tests/agents (httpx errors) — always unset them,
  per the sandbox recipe; (b) a second full suite running concurrently on this box (a pass-10
  worktree shared the venv) starved the run to a crawl at ~88% — rerun when the box is free.

### Screenshots (goal files, pass9-shots/)

Tablet 1180×820: `manager-attention.png` (HIGH pills, expected-vs-capacity bars, Open People,
filter chips with live counts), `manager-attention-inventory.png` (Paneer stock card: burn-down
sparkline, "1130 free now · runs out 7:21 PM", the wired "86 dishes using Paneer"),
`manager-home.png` ("Good evening, Lakshmi" + the top-cautions strip over the tiles),
`kiosk-cart-expanded-tablet.png` (Scope K: cart expanded with 3 items, the checkout capture
visible in the card's own scroll, dish cards above it clipped only by the menu viewport's edge).

### Commits (bujji/pass9, local — for the parent to merge)

3cd3d5b backend payload (contracts + service + contract tests) · af6d649 web (pills, evidence,
filters, 86, greeting/strip + tests) · 3dfe7b4 Scope K · fa2067e Scope L · this handoff.
## Pass 10 — a realistic order history, simulated through the app itself (branch `bujji/pass10-sim`, off pass-8 head 21a3577; NOT pushed)

The founder's ask (Oct 30): "simulate some orders with specific customer info at some particular
days so we can make it look like a realistic analytics to showcase the power of our application."
One new tool does it: `scripts/simulate_history.py`. No engine, projection or web code changed.

### What the tool does

- **History is real activity, replayed.** The script boots the app in-process (the real lifespan:
  providers, commit hook, startup sync, handlers, bus) and winds the tenant's stored clock spec to
  each simulated moment — the same statement shape `seed/scenario.py` uses to set a tenant clock.
  Every order goes through `OrderService.place` (menu re-pricing, availability, station tickets,
  stock reservations, `order.placed`), cooks through `KitchenService.ticket_action` (start, then
  bump at a plausible wait; median ~15 min, a few long tails on anonymous orders only), and is
  picked up via `OrderService.mark_picked_up`. Vikram's bad visit is cancelled through
  `OrderService.cancel`; the bus runs throughout, so inventory consumes on bump and releases on
  cancel exactly as in production. **No raw SQL writes to orders/lines/tickets/ledger/customers
  anywhere** — the only direct row write is the tenant clock spec, restored verbatim at the end
  (in a `finally`, so a crash restores it too).
- **Guests identify like guests.** Each cast visit runs `CustomerService.identify` (name + phone,
  a fresh session per visit) immediately before placing from that session, so customers.db session
  links form exactly as kiosk identifications make them and repeat visits accumulate.
- **The plan is pure and deterministic** (fixed seed 20261030; the plan layer imports no app
  code). Window T-26..T-1 anchored to the tenant's own business clock read at runtime, every day
  open; volume follows the tenant's demand-curve shape scaled to one kiosk (Mon ~22 → Sat ~44,
  Fri/Sat heaviest, Mon quietest); daypart mix tilts the seed `item_mix` (tiffin at lunch, biryani
  at dinner, paneer steady); plus 8 completed anonymous lunch orders earlier today (the
  pre-existing open board is never in the plan and stays untouched). Plan totals: **814 orders
  (813 paid, 1 cancelled), average placed ticket $19.84 incl. tax** ($18.55 subtotal).
- **The cast** (phones are consistent 555-01xx): Priya — 9 visits T-26..T-19, then nothing:
  lapsed regular, next visit is her 10th, usual Paneer Butter Masala. Asha — 6 visits, latest
  T-2, usual Masala Dosa + Filter Coffee (the pair 4 times). Vikram — 5 visits, every ticket
  $55+, plus exactly one cancelled order at T-5 as his latest contact: the incident candidate.
  Ten more named guests (Rohan 4, Sneha 4, Diego 3, Fatima 3, Kenji 2, Maria 2, Tom 2, Grace 1,
  Omar 1, Nina 1); everything else anonymous.
- **Idempotent + guarded.** Every placement carries a deterministic idempotency key derived from
  the plan hash (`pass10:<hash>:<seq>`), so a re-run can never double an order; a run-state file
  in the tenant's data dir (`<data_dir>/simulate_history/<tenant>.json`) records the anchor day,
  the original clock spec and completion — a completed re-run no-ops ("already simulated"), an
  interrupted one resumes from the ledger's state. The tool refuses any tenant whose template is
  not the sample template or whose name is not the sample name, unless `--force`.
- **Inventory honesty.** 26 days of real placement drained most ingredients to zero through
  recipes (paneer, chicken, flour, goat… all hit 0). After the run, every ingredient below its
  run-start level is restored to that level through `InventoryService.adjust` (source
  `receiving`) — the regular deliveries those 26 days would have brought, documented below as one
  this-morning delivery. Paneer is then tuned through the same adjust path (one `count`
  adjustment, 1610 → 762 g) until the console's own projected run-out lands in the evening
  window: **7:46 PM tonight**, free 282 g against ~1200 g forecast use. No projection or engine
  code was touched.

### Sandbox proof (fresh `dinner_rush` sandbox `sbx_01M6773QAMWCM93G4ZMDJ32R5E`, T = Sat Nov 7 2026)

Verified through the app's own services after the run (a re-run was also exercised: 6 s,
"already simulated", exit 0):

- **Cast, as the ledger reads it** — Priya 9 visits · $191.30 · last 19 days ago · usual Paneer
  Butter Masala; Asha 6 · $100.45 · 2 days · Masala Dosa; Vikram 5 · $305.76 · 6 days; Rohan 4 ·
  $90.87 · 3; Sneha 4 · $79.08 · 4; Diego 3 · $70.01 · 2; Fatima 3 · $66.28 · 5; Tom 2 · $49.71 ·
  7; Kenji 2 · $41.15 · 6; Maria 2 · $35.27 · 3; Grace/Omar/Nina 1 each. 13 customers, 10 repeat.
  Lapsed list: Priya only. A returning-guest kiosk read for Asha's phone returns known, 6 visits,
  usual = Masala Dosa ×5, Filter Coffee ×4, Masala Chai ×1.
- **Attention feed (15 cards)**: the two goodwill cards stand — **[incident] "Vikram's last order
  was cancelled"** (5 visits, $305.76, options incl. Goat Biryani) and **[lapsed] "Priya hasn't
  been in for 19 days"** ("Their usual: Paneer Butter Masala ($15.49 menu price)" — the demo
  line) — plus **"Paneer runs out today about 7:46 PM"** and **"Curry looks short for Dinner
  today"** (forecast ~14 tickets, nobody on the station), with the rest of the station family.
- **Analytics**: forecast confidence **Steady — "Built from 3 past Sundays of this restaurant's
  own sales, blended with the recent trend"** (history-based, no longer the demand prior). Best
  sellers by real counts: Chicken Dum Biryani 91, Paneer Butter Masala 50, Chicken Tikka Masala
  48, Goat Biryani 41, Veg Biryani 35.
- **Revenue**: week Mon Nov 2 → today: $437.20 (23 orders, "1 cancelled" — Vikram's) · $489.59 ·
  $514.68 · $617.89 · $827.81 (Fri) · today $347.44 / 19 orders (8 simulated lunch + the 11
  pre-existing board orders); average ticket today $18.29; week total $3,234.61 / 166 orders.
- **Stock at the end** (on hand / reserved / free): paneer 762 / 480 / 282 g (run-out 7:46 PM);
  chicken 11,000 / 560 / 10,440 g; biryani rice 36 / 1 / 35 portions; makhani base 24 / 2 / 22
  portions — i.e. every position back at the scenario's own levels except paneer, tuned as
  above. The delivery restore covered 26 ingredients (the big ones: chicken 11,000 g, flour
  11,000 g, onion 7,000 g, milk 6,000 ml, mixed veg 6,000 g, yogurt 6,000 g, potato 6,000 g, goat
  5,600 g, lentil 4,500 g, spinach 3,000 g, mango pulp 3,000 ml, chickpea 2,640 g, butter 2,200 g,
  cream 2,100 g, moong 1,980 g, paneer 1,610 g, cashew 1,500 g, coconut 1,100 g, garlic 1,100 g,
  gongura 1,000 g; plus cauliflower 400 g, coffee 312 g, mustard 194 g, tea 140 g, egg 51,
  biryani rice 36 portions, dosa batter 38, sambar 45, makhani base 24, gulab jamun 45, water 32,
  soy sauce 20 ml).

### Tests

- New: `tests/infra/test_simulate_history_plan.py` (9 tests) — the plan's shape without running
  the simulation: determinism, 700–900 window orders, avg ticket band, every day trades,
  weekend > Monday, Priya/Asha/Vikram shapes (incl. the single T-5 cancellation as Vikram's
  latest contact and every Vikram ticket ≥ $55), cast sizes/phones, opening-hours containment,
  today-lunch-only.
- Python full suite (clean env, all proxy vars unset): **1556 collected, 1 failed** —
  `test_localhost_tcp_needs_opt_in`, the known environmental failure from the pass-7/8 record.
  (An earlier contaminated run under `all_proxy` failed 18 httpx-URL tests; with the recipe env
  every one of them passes — environment, not code.)
- Web: 266 tests — 265 passed + 1 flake on the first run; **266/266 on the re-run**. Ruff clean
  over `src tests scripts`. Typecheck unchanged (no web source touched).

### Screenshots (goal files, pass10-shots/, tablet 1180×820)

`03-manager-home.png` (Attention 15 · Revenue $347.44/19 orders · Analytics "Dinner: ~26 orders"
· Customers "13 remembered, 2 goodwill dishes waiting"), `04-attention.png` +
`05-attention-scrolled.png` (the capacity cautions, Curry first), `09-attention-goodwill.png`
(Vikram incident + Priya lapsed cards with their on-the-house options), `11-attention-paneer.png`
("Paneer runs out today about 7:46 PM"), `06-analytics.png` (Steady forecast + best sellers),
`07-revenue.png` (week day-by-day), `08-customers.png` (regulars grid), plus the join/sign-in
frames `00/01/02`. Capture route worth remembering: a join link's token is signed with the
per-process session secret — mint and server must share `RESTOS_SESSION_SECRET`, and the token
is single-use (a crashed browser attempt burns it).

### Running it against production (a human step — NOT done here)

    cd ~/workspace/restaurantos-ai-work && .venv/bin/python scripts/simulate_history.py --tenant live

(Defaults read `RESTOS_DATA_DIR`; `--dry-run --today YYYY-MM-DD` previews the plan from the seed
files. Expect ~5 minutes for the full run. Do not run it twice — the second run no-ops by
design — and do not run it against `demo` as well unless a second, separate history there is
actually wanted.)

### Commits (bujji/pass10-sim, local — for the parent to merge)

See git log on this branch: the simulator + its plan tests, then this handoff.

---

## Pass 12 — Operator console v1: capture first, then the founders' fleet + support inbox (P44) — built by Bujji, branch `bujji/pass12`, off 1ba3508 (= production main 923cb34 content); NOT pushed

Built 2026-10-30. The agreed order held: the restaurant-side capture shipped in the same pass as
the operator surface that reads it, so no queue exists without its reader. No LLM calls anywhere
in this pass; no new design language — the operator surface is the manager console's dark system
(panels, rings, chips, tones) pointed at platform data instead of one restaurant's.

### A) Capture — the manager console's Support section

- New **Support** tab in the manager console (after Settings). The manager on shift files a
  **problem report** or a **feature request** (kind chips, title ≤ 120, details ≤ 2000) and reads
  the tenant's own queue back underneath with its standing (New / In progress / Done chips,
  reporter + filed time). Anyone else holding a station sees a note that only the manager files;
  the form is never offered to them.
- Storage: new **`support_items`** table (SQLAlchemy Core, `store/tables.py`) — tenant_id, id
  (`sup_<ULID>`, prefix registered in `contracts/ids.py`), kind, title, body, reporter_id,
  reporter_name, reporter_role, status, created_ms/updated_ms; `metadata.create_all` creates it
  in existing databases like every table here. It sits beside `feedback` in `STATE_TABLES`, so a
  sandbox reset keeps filed items (the same call the codebase made for page feedback).
- API (`api/routes_operator.py`, feature **P44**, registered in `REQUIRED_ROUTERS` so it is
  always on): `POST /api/support` + `GET /api/support` ride the device session (tenant from the
  cookie) plus the staff bearer, gated by the staff routes' own `_manager_on_shift` — a
  non-manager gets 403 `manager_required`, no session gets 401. The reporter is the claim holder
  the gate already resolved; `reporter_role` is stored as `manager` (the gate guarantees it).

### B) Operator console — `/operator`, founders only

- Gated by the **existing admin auth** (`POST /api/admin/login` → `ros_admin` cookie;
  `require_global_admin`). The route is registered feed `none`, session `false`, and nothing
  links to it from the splash, kiosk or staff paths — owners arrive by address. The surface
  opens with its own token login card when the fleet read comes back 401/403.
- **Fleet** (`GET /api/admin/fleet`): every real tenant — `sbx_*` sandboxes and the
  `spice_route` template excluded — oldest first, from existing tables only: name + kind,
  onboarded (`tenants.created_ms`), menu item count (`menu_items`), **team size from the staff
  store** (see decisions), last order (`max(orders.created_ms)`), last activity
  (`tenants.last_active_ms`).
- **Support inbox** (`GET /api/admin/support`): all tenants' items newest first (cap 200)
  with tenant name joined in, plus `open_count` (status != done). `POST
  /api/admin/support/{id}/status` walks an item to any of new / in_progress / done (unknown
  id → 404); the UI offers the three as buttons, current state highlighted, and updates the row
  in place. The restaurant's own Support section shows the same standing — one queue, two ends.

### Design decisions worth knowing

- **Staff count comes from the P41 staff store, not the ros.db `staff` table.** The `staff`
  table holds onboarded config rows (and stays empty for tenants created by `add_tenant`);
  people — seed personas and signups — live in the staff store. The fleet reuses
  `routes_staff._base` + `staff_store(c).people(...)`, the same read the sign-in screen makes,
  so the number is the team a restaurant actually signs in with.
- **A new table instead of extending `feedback`.** `feedback` is page+note only, written by the
  page-feedback slot with no auth context; kind/status/reporter/tenant-queue semantics are a
  different shape, and overloading it would have bent both.
- Support times are **real times** (UTC ms like `feedback`), not tenant business times — the
  operators work across tenants and timezones, so both UIs format them in the reader's zone.
- The status walk accepts any target status from the API (the UI leads new → in_progress →
  done); an operator can also send an item back to New, which a strict forward-only walk would
  forbid for no product gain.
- The operator endpoints live under **`/api/admin/*`**, not `/api/operator/*` (corrected in the
  fixup commit): the cross-tenant walk (`tests/security/`) classifies only `/api/admin/*` (and
  `/api/health/deep`) as admin-scope — every other `/api/*` route must answer `wrong_tenant` to
  a foreign session, which a GlobalAdmin gate by design does not. The codebase's other
  GlobalAdmin routes (kill-switch, caps) already live under `/api/admin/`; this pass follows.
  The same fixup registered the pass in the standing contracts: the `support_items` SPEC
  transcription (`tests/contracts/test_tables.py`), the `KEPT_TABLES` classification
  (`store/sandbox.py`), `EXPECTED_MODULES` (`tests/integration/test_registry.py`), and an
  `UNPROBED_PARAMS` entry for `item_id` in the extended cross-tenant walk (a support item is a
  platform-queue resource the admin gate refuses before the id is read, so no tenant id can
  name one).

### Tests

- New Python: `tests/api/test_operator_routes.py` (10 tests) — session/manager gating, filing
  both kinds with the reporter stamped, validation bounds, per-tenant isolation, admin gating
  (401 without credentials, 403 with only a restaurant session), fleet inclusion/exclusion and
  its counts incl. a placed order's `last_order_at`, the cross-tenant inbox with the open count
  moving as an item walks to done, 404/422 edges.
- Python full suite (clean env, all proxy vars unset): **1566 tests — 1562 passed, 3 skipped,
  1 failed** — `test_localhost_tcp_needs_opt_in`, the known environmental failure from the
  pass-7/8 record. (Counts read from the run's JUnit report.)
- Web: **280/280** (`vitest run`, 43 files; 276 before this pass + 4 new) and `tsc` clean. New:
  `surfaces/manager/support.test.tsx` (manager files a feature request, POST pinned, own queue
  reads back; a station holder is told only the manager files and no `/api/support` call is
  made) and `surfaces/operator/operator.test.tsx` (token login, fleet card facts, inbox walk
  new → in_progress → done with both POSTs pinned and the open count falling to 0; wrong token
  refused).

### Screenshots (goal files, pass12-shots/, tablet 1180×820, local dev server, mock AI)

`01-manager-support.png` (the filed printer report under Your reports, New), `02-operator-login.png`,
`03-operator-console.png` (fleet: the dev boot's live + demo tenants, 30 items / 5 people each;
the sandbox-filed report in the inbox as New), `04-operator-in-progress.png` (same row after the
walk). Capture note: `scripts/dev.sh` runs the venv's editable install — point `PYTHONPATH` at
the worktree `src` or the server silently serves the main checkout's code.

### Left undone (deliberately, per scope)

- No reply thread from operators back to a restaurant — v1 is capture + status only (the agreed
  sequencing: console v1 first, threads/analytics later).
- No fleet health alerting, no per-tenant drill-in page; the inbox caps at the newest 200 items.
- Nothing deployed, pushed or PR'd — the parent reviews, merges and deploys.

### Commits (bujji/pass12, local — for the parent to merge)

`01f7740` backend (table, contracts, routes, registry, generated TS, route tests) ·
`3658b22` web (support lib, manager section, operator surface, route, web tests) ·
`0146c60` this handoff · a fixup commit (contract registrations + the operator endpoints'
move under `/api/admin/*`, with the entry above corrected to match).

— Bujji, 2026-10-30

## Pass 13 — Inventory management: the Inventory Manager responsibility, opening stock, the nightly closing check, photo-found ingredients, bill scan (branch `bujji/pass13`, off pass-12 head 4b2be97; NOT pushed)

Vamsi's spec (2026-10-31): someone is assigned the Inventory Manager role; a single user can hold multiple roles; initial inventory is entered at setup; every day the app shows possible low-stock ingredients to cross-check at night before closing; low stock is estimated from order history and the initial inventory size; images auto-identify new ingredients and their quantities; purchasing bills are scanned to add stock. The parent audit found the deterministic core already built (BOM reserve/consume/release/receive/adjust, the burn/stockout forecast, shelf-photo counting, supplier POs) — this pass wires the missing surfaces to it and rebuilds none of it.

### What shipped

1. **Inventory Manager responsibility + multi-role** (`50b1f27`). A new `responsibilities` table in the staff store (`services/staff_identity.py`), additive by construction: it never touches the default role, the PIN-login flow, or per-shift slot claims. `POST /api/staff/responsibilities` (manager-on-shift only, 404 on an unknown person) grants/revokes `inventory_manager`; `StaffPerson.responsibilities` rides every roster/login response. Authorization is one dependency (`InventoryAccess` in `routes_inventory.py`): a session with the manager cap OR a staff Bearer token whose person holds the responsibility; the actor stamp is the session role or `inventory_manager`, so the ledger says who really counted. Denial is 403 `inventory_access` with `details.needs = ["manager", "inventory_manager"]`. It gates stock adjust, setup, the closing check, shelf-count confirm, and bill-scan confirm. Manager console: People cards carry a responsibility chip next to the usual job (Carlos reads "Usual job: Curry Chef 1" + an Inventory Manager chip), and the Team jobs card has a per-person Give responsibility / Take back row.
2. **Opening-stock entry.** `GET/POST /api/inventory/setup`: the view pairs the full catalog with whether setup is done (an `initial` event exists); saving validates the whole grid first, then writes one auditable `initial` adjustment per ingredient whose count differs — and when every count matches the placeholder pars, one same-level marker `initial` event so "setup done" itself is on the ledger. Web: the manager console's new **Inventory** section opens with the grid, every row prefilled with its par (per the spec), flagged "Setup step" until saved, then "Entered" with re-entry one tap away. Placement decision: the grid lives in the manager Inventory section rather than the onboarding review — the onboard draft has no ingredient model (pars are inferred server-side at materialize), so onboarding placement would have meant inventing a draft ingredient model; the section is the setup step for a fresh tenant either way, and Settings → Inventory remains the natural later home.
3. **Tonight's closing check.** `GET /api/inventory/closing-check` + `POST .../complete`. Due = the existing forecast says the ingredient needs watching/acting (band) OR its stockout ETA lands on or before the NEXT business day's service end (seeded template close, fallback 23:59) — evaluated against current on_hand, which already reflects opening stock minus order consumption; each row carries expected remaining at tomorrow's close (`free − burn × hours left`), sorted soonest-out first. Blank counts mean "matches the books"; entered counts apply a `count` adjust per variance before the completion row (`closing_checks` table: business date, who, when, per-ingredient book-vs-counted) is written. A second completion the same business day is 409. On-demand and evaluated on read, like everything else in this product — no push is faked. Web: the ClosingCheck card in the Inventory section (count inputs prefilled with the book qty as placeholder, completion banner naming who/when and listing variances).
4. **Photo identifies NEW ingredients.** The Spark shelf read now also returns `new_items` (name, unit ea/g/ml, count) for countable stock the kitchen does not list; `spark.read_proposals` drops anything already catalogued, uncountable-unit, duplicated, or zero-count. Proposals ride the pending count (no SAM pass — there is no catalog entry for a second count to agree with, and the UI says the count is Spark's). `ShelfConfirmRequest.new_ingredients` accepts proposals by name (a name the photo never proposed is 400, checked before the pending count is consumed); accepting creates the ingredient through `InventoryService.add_ingredient` — slug `ING_<NAME>` deduped against the catalog, opening stock = counted qty (× pack size for weighed units), par/reorder defaulting to the opening / half of it, both editable right in the review — in the same confirm, and the created item rides back in the confirm response so the screen can show it. Known-ingredient behavior is byte-identical; `count_photo` kept its signature (it is now a wrapper over `count_photo_full`).
5. **Bill scan → stock in.** `perception/bill_scan.py` + `perception/pdf_text.py`. Upload a photo (the usual prepare path) or a PDF (dependency-free text-layer extraction — FlateDecode streams + Tj/TJ strings; an image-only PDF gets an honest 400 "take a photo instead"). The parse yields a DRAFT: supplier, per line the raw name/qty/unit/price, a deterministic catalog match (exact name/id, else a single whole-word-subset candidate; ambiguity stays unmatched), and a suggested stock qty (same unit passes through; kg→g and L→ml convert ×1000; pack-words or untranslatable units fall back to packs × pack size; unmatched lines suggest their bare qty for the create form). Confirm receives through the existing inventory receive path (PO-receipt deltas, correlation = the receipt id) or creates the ingredient for create-lines, then writes the `supplier_receipts` row; `GET /api/supplier/receipts` lists them in the supplier surface (and the Inventory section shows "Bills received"). Mock AI mode returns a deterministic simulated draft built from the tenant's own catalog, `simulated: true` and badged "Simulated scan — offline mode" on screen, the supplier-inbox precedent. Pending drafts expire in 15 minutes; the 12 MB cap and one-photo-per-person discipline follow the shelf flow.

Contracts: `AdjustSource` gains `"initial"`; new id prefixes `chk`/`bdr`/`brc`; `closing_checks` + `supplier_receipts` join `STATE_TABLES` in both the sandbox cloner and the materializer (a sandbox now carries them like every other state table); TS contracts regenerated (+118 lines in api.ts).

### Tests

- Python: **1613 tests — 1609 passed, 3 skipped, 1 failed**, the failure only the known environmental `test_localhost_tcp_needs_opt_in`. New coverage: `tests/api/test_inventory_management_routes.py` (grant/revoke + the multi-role authorization matrix — a cook holding the responsibility adjusts/counts/sets up, a plain cook gets 403 `inventory_access`; opening stock writes `initial` events; closing-check list/variance/completion/409; responsibilities endpoint gates) and `tests/perception/test_bill_scan.py` (proposal filtering, bill matching + unit conversion, PDF text extraction, the recorded live draft, the mock simulated draft, confirm → on_hand + receipt record), plus shelf-proposal cases in `test_shelf_routes.py`. Two existing suites were updated for the deliberate contract changes (the adjust/shelf denial shape, and `StaffPerson` gaining `responsibilities`).
- Web: **284 tests across 44 files**; new `surfaces/manager/inventory.test.tsx` (opening entry save body pinned, closing list + completion/variance rendering, bill draft → receive body pinned, People chip + grant/revoke bodies pinned) and two proposal tests in the shelf-count suite. Full-suite runs on this VM intermittently fail ONE rotating timing-sensitive test (`staff.test.tsx` switch-job, once `approve.test.tsx` digest) — reproduced on the untouched pass-12 tree (the staff file failed 3 of 4 solo runs there), so it is a pre-existing environment flake, not a pass-13 regression; every pass-13 test passed in every run. `tsc -p tsconfig.json` clean, `vite build` clean, `ruff check src tests scripts` clean.

### Screenshots (goal files, pass13-shots/)

Tablet 1180×820, local dev server, mock AI, fresh tenant: `01-opening-stock-entry.png` (the grid, pars prefilled, "Setup step"), `02-closing-check.png` ("26 to check", Paneer ACT with ETA + counted inputs), `03-photo-new-ingredient.png` (the shelf dialog: Paneer AGREE plus "New in this photo" — Greek Yogurt Tubs accepted, opening 12 ea; the shelf POST was fulfilled with the exact ShelfCount JSON the live server returns, since mock mode's photo count is the manual fallback by design), `04-bill-scan-draft.png` (the simulated draft, matched lines, stock-in prefilled), `05-people-two-roles.png` (Carlos: Curry Chef 1 + Inventory Manager chip). Two screenshot-driven fixes: the grid now prefills pars (the spec's words) instead of current on_hand (cc10f1c), and the proposal reorder input was widened so its hint is not clipped (165d09d).

### Left undone / notes

- The closing check's "or run a shelf photo per ingredient" path is the existing GM shelf slot (it counts the whole shelf, not one ingredient); the check's counted inputs are the per-ingredient path. Nothing scheduled exists behind the check — it is tonight's list whenever it is opened, per the product's honesty rule.
- Commits: `50b1f27` backend · `5b4b8e3` web · `cc10f1c` + `165d09d` screenshot polish · this handoff. NOT pushed, no PR — the parent merges. Deploy waits on the Fly machine, which stays stopped at Vamsi's word.

## Pass 14 — Regulars win-back marketing (P45) — built by Bujji, branch `bujji/pass14`, off 4b2be97 (pass 12 head); NOT pushed

Built 2026-10-31. P43 identified the regulars and stored `marketing_opt_in` (default off, "nothing
sends anything"); this pass is the outbound half: find the drifted regulars who opted in, draft each
a personal invitation, let the manager edit and approve it, send it through a messaging port, hold
the promised dish as a goodwill credit, and mark the outreach returned when the guest orders again.
No new credit system and no new consent system — both ride what P43 already keeps. (Phase is **P45**:
P44 was already the operator console's.)

### 1) The Win back view (manager console → Customers, new card under Drifting away)

`GET /api/manager/winback` (session auth) returns the contactable list — lapsed regulars (3+ visits,
last visit ≥14 days ago, P43's own definitions) with `marketing_opt_in = true` — each with visits,
last visit, usual dish, days away, and the state of their current case (none / draft / sent /
returned + any credit already waiting). Guests who never opted in are returned **only as a count**
(`not_contactable_count`): no names, no numbers, nothing messageable. Case keys are deterministic
(`winback:<masked number>:lapsed:<last order id>`) so a case is re-derived from the same facts and a
fresh drift is a fresh case; the key carries the MASKED number — unlike the goodwill suggestion key,
this one travels to staff surfaces.

### 2) Drafts — always drafts until a human sends

`POST /api/manager/winback/draft` (manager on shift only) takes case keys and writes one draft per
guest: name, usual dish, days away, and the offer — **their usual on the house**, the same dish the
lapsed-goodwill suggestion would hold. In live AI mode the text comes from the existing Meta path
(`Purpose.GM`, customer data class, tenant spend scope) with the template as an automatic fallback
on any failure; in mock mode it is the deterministic template. The source (`model` / `template`) is
recorded on the draft. Drafting refuses the whole request if any key is unknown/not-lapsed (400
`not_lapsed`) or belongs to a guest without opt-in (400 `not_opted_in`) — batch selection can never
smuggle in a non-consenting guest. Re-drafting an open case returns the existing draft; a case
already sent or returned is reported under `skipped`. `POST /api/manager/winback/edit` saves the
manager's rewrite (drafts only; a sent message is a record and refuses edits, 400 `already_sent`).

### 3) Approve & send — a port, an honest channel, a record

Sending is the approval: `POST /api/manager/winback/send` (manager on shift; the sender is recorded
as the approver) re-checks **every guest's opt-in at send time** — a guest who opted out after
drafting refuses the whole batch (400 `not_opted_in`, nothing sent). It then sends through a new
`MessagingPort` (`contracts/ports.py`, a `Container` field, wired by a PROVIDERS entry
`services.messaging` like every other port): `SimulatedMessaging` in mock mode (recorded, labelled
simulated; the response carries `X-Simulation: simulated-messaging`, the supplier inbox's header
idiom) and `UnavailableMessaging` in live mode — no SMS provider is configured in this product, so
the send refuses plainly with 400 `no_provider` and the UI's provider note says the same in words
("…can't be sent yet. Drafts can still be prepared and edited."). Every send is a row in the book:
guest, final text, offer, approver, sent time (business clock), channel, simulated flag, status.

### 4) The promise and the return — one credit book, matched on read

At send time the promised usual is minted through the goodwill book's own mint path
(`CustomerStore.mint_credit`, extracted so `record_decision` and win-back share it) under the case
key — or linked, never duplicated, if a credit for the case already waits. It then behaves exactly
like any goodwill credit: `pending_credit` at the guest's next identification, applied as a $0
`goodwill_comp` line at placement, spent once. Returns are matched on read (like cautions): a sent
outreach whose guest places a later non-cancelled order (business clock) flips to `returned` with
that order's id linked, and the guest leaves the drifted list — they are back. Stored in a new
`winback_outreach` table in the customers book (customers.db, so no store/tables registration).

### 5) Opt-in capture — the stored field, surfaced honestly

`IdentifyRequest` gains `marketing_opt_in: bool | None`: when the kiosk identification collects an
answer it is stored (and `marketing_asked` set — a new column, added by a guarded ALTER so older
books keep working); `IdentifyResult` now reports `marketing_opt_in` and `marketing_opt_in_asked`
so surfaces can ask once and never nag. Absent answer = not asked; nothing about the kiosk flow
changed shape, no new consent surface was invented, and the default stays off.

### Web

`customersApi.winback / draftWinback / editWinback / sendWinback` (generated contracts regenerated,
additive only). The **Win back** card: provider note first, the not-contactable count line,
contactable guest cards (checkbox only while no outreach exists, manager only), **Draft messages**,
a Drafts section with per-guest editable text areas + source chip + save-edit, **Approve & send
selected**, and a Sent list with Sent / **Returned** / **Simulated** chips, the offer chip and the
return's order link. Drafting/sending requires the manager's staff session (the section now takes
`me`, like Support); if the win-back read fails the card hides itself and the rest of the section
stands. Contracts note: adding the `messaging` container field grew CONTRACTS_RUNTIME §10.0 — the
two test mirrors of the field list (`tests/contracts/test_ports.py`, `tests/integration/test_boot.py`)
were amended to match, the same way the registry mirror grows.

### Tests

- Python: `tests/api/test_winback_routes.py` (8 new) — view gating (opt-in only, count-only for the
  rest, numbers never in the payload), identify opt-in storage + asked flag + answer changes, draft
  gate / opt-in refusal / unknown case / idempotence, batch draft + edit + 404, send record + credit
  minted + return flip + re-send/edit refusals, **opt-out after draft refuses the send**, live-mode
  no-provider refusal, and the win-back credit riding a return order through placement as a $0
  comp line. Full suite: 1,574 collected (1,566 + 8). In this sandbox the run shows 20 failures
  that are **identical on the pristine pass-12 base** — root-caused to the sandbox's `NO_PROXY`
  value (bracketed IPv6 entries httpx cannot parse; every one of those tests passes with
  `NO_PROXY` unset) plus the known `test_localhost_tcp_needs_opt_in` permission failure; none is in
  code this pass touched. One 200 ms timing test flaked under parallel load and passes solo.
- Web: 281 tests (280 + `winback_draft_edit_and_send_flow`: select → draft → edit → send with POST
  bodies asserted, simulated chip and waiting credit visible). One staff-surface test flakes
  intermittently in this sandbox on both the base and this branch (different tests in that file
  across runs); the first full run here passed 281/281. `tsc` clean.

### Screenshots

`~/workspace/goals/meta-global-ai-developer-hackathon-2026-application/files/pass14-shots/` —
`01-winback-list.png` (contactable Asha + Chandra, Vikram as the not-contactable count),
`02-draft-editor.png` (both template drafts in editors), `03-sent-and-returned.png` (Asha Returned
with her order linked, Chandra Sent, both labelled Simulated). Taken on the local dev server
(sample tenant's sandbox, business clock Sat 2026-11-07) with a seeded book: three regulars whose
last visits were backdated in the ledger; Asha's return is a ledger order placed after the send on
the business clock.

### Left undone / notes for the parent

- No real SMS provider exists in the product, so live mode honestly cannot send — wiring one is a
  new adapter behind `MessagingPort` when the founders pick a provider.
- The kiosk identification UI does not yet ask the marketing question; the API + stored field are
  ready and report asked/opted state (pass-level call: no new consent surface in this pass).
- Nothing deployed, pushed or PR'd — the parent reviews and merges. Pass 13 (inventory) worked in
  parallel in its own worktree; no inventory/perception/staff files were touched here.

### Commits (bujji/pass14, local — for the parent to merge)

`b35fec7` backend (messaging port + adapters, win-back service + routes, customers book outreach +
opt-in capture, contract-mirror amendments, route tests) · `6d8ec34` web (API client, Win back card,
section prop, web test, regenerated contracts) · `8c41995` fixup (win-back renumbered P44 → P45;
P44 is the operator console's) · this handoff entry.

— Bujji, 2026-10-31

## Pass 15 — Cautions carry their "why" — built by Bujji, branch `bujji/pass15`, off the pass-12 head 4b2be97 (= main aa313b0 content); NOT pushed

Founder requirement (Vamsi, 2026-10-31): "unusually long cooking time etc needs to be analyzed
with why reason." A caution now says not just WHAT is wrong but WHY — the evidenced driver,
picked from numbers the checks already computed. Zero new model calls, zero new reads: this is
statistics over the same payloads (the zero-token principle), and a cause is named only when it
clears an evidence bar — otherwise the card keeps no why line, and a wait incident says so
outright ("No single cause stands out in the record — …") rather than guessing.

### The engine (`domain/why.py`, new, pure)

- `capacity_why(...)` — a forecast-vs-hands shortfall is driven by a **surge** (expected ≥ 1.25×
  the same-weekday typical for that station/daypart, ≥ 3 tickets over) and/or an **unfilled
  slot** (a full crew's capacity beats the staffed one by ≥ 5 tickets). Two causes stack,
  biggest first ("Two things stack up: …"). With a norm on record and neither cause clearing,
  the honest no-single-cause line names the norm it compared against. With no history and a
  full crew — or nobody on the station at all (the card's own detail already says exactly
  that) — it returns `None`.
- `stock_why(...)` — a run-out is driven by a **usage climb** (the past week's daily use ≥ 1.3×
  the weeks before it; needs ≥ 12 history days) and/or **the dish doing the burning** (one dish
  ≥ 40% of the forecast need, named with its quantity and servings). Evenly spread burn at
  flat use returns `None`.
- `wait_incident_why(legs, ...)` — for a slow order, from the incident order's OWN per-station
  facts: the bottleneck is the longest leg; its evidenced causes are **overload** (tickets past
  what its cooks cover at the planner's 6/hour pace in the window), **unfilled slots**,
  a **room surge** (orders in the window ≥ 1.3× the same-weekday norm), and an **86'd dish** on
  the ticket named as a circumstance (worded as may-have, never as proven cause). Nothing
  clearing returns the no-single-cause line built only from established negatives. ALWAYS
  returns a line — an incident the founder asked "why" about always gets an answer.

### Wiring

- `contracts/api.py`: `ConsoleCard.why: str | None` and `GoodwillSuggestion.why: str | None`
  (TS contracts regenerated; both render in `surfaces/manager/index.tsx` under the Pass 9
  evidence visual / the goodwill detail, above "Suggested:"; no why → renders exactly as
  before).
- `services/manager_console.py`: capacity checks pass the same-weekday typical (mean over the
  history days sharing the plan day's weekday — the same pool discipline as the forecast) plus
  the station's slot count; stock checks accumulate the per-dish burn split and each
  ingredient's daily-use series over the history already read for the forecast. Capacity and
  stock cards in the live feed now carry whys (see screenshots).
- **Goodwill is deliberately NOT wired in the service this pass**: `services/customers.py` is
  owned by the parallel pass-14 build. The contract field, the web rendering, and the tested
  engine all ship; the follow-up hook is one function — in `CustomerService._suggestion_view`,
  for `reason == "slow"` suggestions, build `StationWindow`s from the incident order's lines
  (per-station leg = that station's ready-minus-fired span, window = the wait, tickets/cooks/
  slots from the KDS read at incident close, room counts from orders in the window vs the
  tenant's same-weekday norm, 86'd dishes from the incident order's items) and set
  `why=wait_incident_why(...)`. No order in the seeded data reconstructs another way: the
  incident is a ledger read, and the legs must come from the incident order itself, never from
  today's staffing. Lapsed-regular suggestions carry no why by design (a drift has no
  evidenced single cause in the record).

### Tests

- New `tests/domain/test_why.py` (13): surge / unfilled slot / stacking order / no-single-cause
  / null cases for capacity; top-dish and usage-climb for stock; bottleneck-with-numbers,
  surge, 86-circumstance, and the no-single-cause line for wait incidents.
- API: `tests/api/test_manager_routes.py` — Curry's tomorrow-dinner card why names the slots
  ("Only 1 of the station's 3 cooking slots have a usual-job cook — a full crew would cover
  about 72 tickets, not 24"); the nobody-on-Biryani card's why is `None`; the paneer stock
  card's why names Paneer Butter Masala with the forecast's own numbers (5700 of 11310 g,
  ~38 servings).
- Web: `manager-sections.test.tsx` (why lines on both card kinds; a feed whose payloads carry
  no why renders no "Why:" anywhere) and `customers.test.tsx` (a slow goodwill incident shows
  its why). Touched Python areas (domain + api + services): 223/223. tsc clean. Full suites
  in the builder's sandbox showed only pre-existing environmental failures (this sandbox's
  `no_proxy` breaks httpx URL parsing — identical failure set on the untouched pass-12 base,
  verified by subset parity; web timeouts under parallel load, all green on an idle box).

### Screenshots (goal files, pass15-shots/, tablet 1180×820, local dev server, mock AI)

`00-attention-feed.png` (the whole feed), `01-capacity-card-why.png` (Curry, live payload),
`02-stock-card-why.png` (Paneer, live payload), `03-goodwill-card-why.png` (the shipped
rendering fed by request interception with a suggestion payload whose why string is
`wait_incident_why`'s real output — the goodwill service hook lands with pass 14, see above).

## Merge — Passes 13 + 14 + 15 land together, plus Pass 15's goodwill hook (branch `bujji/passes-13-15`)

Merged locally in sequence (13 → 14 → 15) off the pass-12 head 4b2be97 (= main aa313b0 content), then
pushed as one PR for review and merged to main; the image build was dispatched on the new main. The
Fly machine stays stopped — nothing deployed, per Vamsi's word.

### Conflicts and fixups

- `BUJJI_SESSION_HANDOFF.md` on both merges: pure append collisions — both entries kept, in pass
  order (13, then 14, then 15).
- `src/web/src/surfaces/manager/customers.test.tsx` (pass 15 merge): pass 14's win-back fixtures and
  pass 15's slow-suggestion fixture both extend the same mock — kept both fixture sets and both
  `beforeEach` resets; nothing dropped.
- Everything else (contracts/api.py, generated TS, manager index, services) auto-merged; the wins
  were checked semantically at each checkpoint rather than trusted.
- Fixup `9a92417` (ruff): pass 14's column-list SQL tripped S608 (the only interpolation is the module
  constant; values stay bound — noqa'd per the codebase's own precedent) and an unused `now_biz` in
  the win-back view was dropped. Base was ruff-clean, so the merge restores that.
- Fixup `9daae96` (cross-tenant walk): pass 13's new `POST /api/perception/bill/{draft_id}/confirm`
  route was missing from the security walk's registry. Bill drafts are per tenant (confirm looks the
  draft up under the caller's tenant), so `draft_id` joins `UNPROBED_PARAMS` with its why, on the
  `count_id` precedent. Verified: the walk fails on the untouched base for nothing, and passes on the
  merged tree with the entry.

### The goodwill hook (f977925) — pass 15's follow-up, now wired

`CustomerService._incident_why` feeds `wait_incident_why` from the incident order's own record:
its tickets' per-station spans (created → bumped), the tickets those stations took in during the
wait (cancelled orders excluded), the cooks the shift log shows on each station over the window
(claims are attendance, the timesheets' doctrine — spans convert through the tenant clock, so no
fact is read off today's staffing), the room's order count against the same-weekday norm (pooled
over the trailing eight weeks; silent under two pool days), and any of the order's dishes 86'd
during the wait, from the availability events. Lapsed and cancelled suggestions keep `why = null`
by design. The goodwill route now hands the service the staff store + slot defs, the timesheets
route's own pattern. New `tests/api/test_goodwill_why.py`: a slow incident on an overloaded Curry
station (7 tickets, 1 of 3 slots claimed) shows "The wait built at Curry (50 min of it): … 7 tickets
in that stretch — more than its 1 cook(s) cover at the usual pace (about 5)"; a lapsed suggestion's
why is null.

### Verification on the merged tree

- Checkpoints after each merge: pass-13 areas (inventory routes, perception) green; pass-14 areas
  (win-back, customers, contract mirrors, boot/registry) green; pass-15 areas (`test_why`, manager
  routes) green; web 284 → 285 → 287 across the checkpoints, tsc clean at each.
- Python, final full run: **1607 tests — 1600 passed, 6 skipped, 1 failed**, the failure only the
  known environmental `test_localhost_tcp_needs_opt_in` (a sandbox socket-permission probe; it
  fails identically on the untouched pass-12 base). With this sandbox's `NO_PROXY` left set, ~19
  further httpx `Invalid port: ':1]'` failures appear on base and merged trees alike — environmental,
  not regressions; the counts above are with it unset.
- Web, final full run: 286/287 — the one failure is the known rotating `staff.test.tsx` switch-job
  timing flake (reproduced on the untouched pass-12 tree), green solo (7/7). `tsc` clean,
  `scripts/gen_ts_contracts.py --check` clean, `ruff check src tests scripts` clean.

— Bujji, 2026-10-31

## Pass 16 — Recipes stay secret: inferred ingredient lists are drafts until the kitchen approves them (branch `bujji/pass16`, off e7a7533 = main 6460757 content; PR opened against main for review, NOT merged, NOT deployed)

The owner's call, from his brother Damo's feedback (2026-11-01): restaurants guard their recipes as
trade secrets, so the system must never silently assume a dish's ingredients from general knowledge.
Four rules, all implemented: (1) an inferred dish ingredient list lands as a DRAFT — status
`proposed`, provenance `inferred`; (2) a chef/manager approves it (or edits, then approves) before
it drives inventory deduction, stock forecasts or cautions — unapproved dishes are flagged on the
manager console Attention feed and excluded from stock math until approved; (3) the system stores
INGREDIENTS + QUANTITIES ONLY — no process/method field exists anywhere in the data model, by
design; (4) the privacy page states all of this plainly.

### Design choices (where the spec left room)

- **Review state is a per-dish row, not a flag on recipe lines**: new `recipe_reviews` table
  (tenant, item_code, status, provenance, approved_by_id/name, approved_ms — the pass-13
  closing-check sign-off pattern, so the approval IS the audit record; no new event type).
  Provenance is one of `inferred` / `seed` / `chef_entered`; after approval the provenance stays
  what it was — the approver and time land beside it.
- **A dish with no review row counts as approved.** Pre-existing kitchens (and every sandbox
  cloned from config before this pass) behave exactly as before with zero migration. Seed/sample
  recipes are the restaurant's own known data, so the loader writes them explicit rows:
  `approved` / `seed`.
- **One filter helper**: `repo.approved_recipes()` is the only list stock math may consume.
  Switched to it: order reservation + `order_holds` corrections (`services/orders.py`), forecast
  priors and reservation overlays + consume/release (`services/inventory.py`), the closing check
  (`services/manager_console.py`), the GM situation snapshot, the governance largest-serving
  check, `repo.reservations_since` (SQL-side filter) and `menu_snapshot` allergens. Deliberately
  NOT switched: `judge/rush.py` (an eval harness over a judged menu) and `seed/scenario.py`
  (seed-level synthetic history).
- **Endpoints live in the inventory family**, gated by pass 13's `InventoryAccess` (manager
  session or the Inventory Manager's token): `GET /api/inventory/recipes/proposed`,
  `POST /api/inventory/recipes/{item_code}/approve` with an optional `{lines}` edit body.
  Approving an unknown dish is a 404, an already-approved dish a 409, empty/duplicated edited
  lines a 400, an unstocked ingredient a 404. Placement note: the spec suggested
  `/api/manager/...`; the inventory family was chosen because the gate, the service and the
  pass-13 review surfaces already live there, and the manager console links straight to it.
- **Manager approval stands in for chef approval**: the role model has no distinct chef persona
  with its own sign-in (cooks hold station jobs; the manager and the Inventory Manager hold the
  inventory gate). The copy says "your chef or manager" throughout.
- **Onboarding**: `recipes.yaml` gains a `proposed: [item codes]` list (loader-validated: a
  proposed code with no lines is a seed error). Every onboarded dish is marked — the starter
  quantities come from a small deterministic name-based mapping (a hearty plate — biryani, thali —
  starts at 2 portions of the placeholder ingredient, everything else at the placeholder fallback
  of 1). Verified before building: there is NO model-backed inference path for menu drafts in
  either mode — onboarding is rules-only — so the spec's live-mode conditional does not trigger;
  if a model drafter is ever added, its output lands in this same proposed state by construction.
- **Attention card**: `ManagerConsole.analytics_view` appends a `recipes`-kind card ("N dishes
  have unapproved ingredient lists", warn tone, names up to three dishes, points at Recipe review
  in Inventory) whenever any dish is proposed; the web maps the kind to the Inventory filter.
- **Review surface**: a Recipe review card grid at the top of the manager Inventory section —
  one card per waiting dish, editable quantity inputs, remove/add ingredient lines (from the
  kitchen's stock list), Approve button; unchanged lists approve as proposed (no body), edited
  lists send their lines. The privacy page gains an "In short" bullet, a dedicated "Your recipes
  stay yours" card, and an "Ingredient lists" retention row.

### Verification

- New tests: `tests/api/test_recipe_review_routes.py` (7) — a proposed dish reserves nothing and
  forecasts no burn (coconut, used by Chicken Chettinad alone, is the clean signal), approval
  restores both and is audited on the row, edit-then-approve stores the edited quantities, bad
  input is 404/409/400/422, onboarding output starts proposed (file, seed and materialized rows),
  the seed kitchen's queue is empty and its math unchanged; the Attention card is asserted at the
  service level. Web: one new inventory-section test (edit → approve call → approver note) and the
  privacy table test extended for the new row and statements.
- Python full suite: **1609 passed, 1 failed** — the failure only the known environmental
  `tests/test_conftest_network.py::test_localhost_tcp_needs_opt_in` (fails identically on the
  untouched base). Web: **288/288 passed**, `tsc` clean, `scripts/gen_ts_contracts.py --check`
  clean, `ruff check src tests scripts` clean (the repo is not `ruff format`-clean at base; CI
  gates on `ruff check` only).
- Screenshots in `files/pass16-shots/` (goal files): the review grid, the Attention card, the
  privacy section. All three are the real UI on a local mock-mode server running this branch: the
  tenant (Chettinad House, 5 dishes) was onboarded through the app's own onboarding flow, so its
  proposed state is the genuine article, and the manager signed in through the staff flow.
  Screenshot-driving note for next time: the dev venv's editable install points at the OLD
  worktree — a local server for a worktree build needs `PYTHONPATH=<worktree>/src`, or it
  silently serves the other tree's code.
- NOT deployed (the Fly server stays down on the owner's instruction) and the PR is left open.

— Bujji, 2026-11-01
