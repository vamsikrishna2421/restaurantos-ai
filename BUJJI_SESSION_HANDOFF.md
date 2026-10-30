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
