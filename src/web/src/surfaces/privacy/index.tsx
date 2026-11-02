/**
 * `/privacy` (P36; ARCHITECTURE_OPS §11 and §13 "Privacy and retention"; MASTER_PLAN §5 "Privacy"; DECISIONS D16,
 * D30(c)): what this demo keeps, for how long, and who else handles it. A static page: it calls no API.
 *
 * Each period is the one the code enforces: the 04:00 ET retention job, the nightly backup (3 kept) and the hourly
 * sandbox sweep (P30c), the log rotation and the Meta file sweeper (P29), the session cookie (P11), the consented-call
 * recorder (P28: `RETENTION_DAYS = 30`, swept only when a recording starts) and the off-host copy
 * (`scripts/backup_offhost.sh`, which never prunes). The live and demo kitchens' call records, consented clip files and
 * the off-host copies have no automatic deletion, and the page says so. The hosting row follows D30(c) and changes
 * when the judging host is chosen.
 */

import { Lock } from 'lucide-react'
import { SampleBadge } from '../../components/badges'
import { Card } from '../../components/ui'
import { CHROME } from '../../lib/videoMode'
import { Link, type SurfaceProps } from '../../router'

const SHORT: readonly string[] = [
  'Spice Route is a sample restaurant: no food is prepared and nothing is paid for.',
  'The phone greeting says you are talking to an AI, that the call is transcribed, and that no food is prepared.',
  'No recording of your voice is kept, except the founders\' own consented test calls and consented clips from the ' +
    'founders and friends.',
  'What you say is kept as text for 7 days in the database (24 hours in a sandbox kitchen); backups of the live and ' +
    'demo kitchens keep it longer (see below). Events and logs never contain it.',
  'Your phone number is never stored: only a keyed hash and its last 4 digits, shown only in your own kitchen.',
  'Meta\'s contributor-tier model only writes synthetic test sentences for our evals: it never sees anything you say ' +
    'or type.',
  'Your recipes stay yours: we draft ingredient lists from general knowledge, your chef approves them before ' +
    'anything uses them, and we store ingredients and quantities only — never how a dish is cooked.',
  'When the AI helps run your kitchen, it never sees your ingredient or dish names: it works with code ' +
    'labels and numbers instead, and the key that matches codes to names never leaves your kitchen\'s own data.',
]

/** Pass 16: the recipe promise, stated plainly — a restaurant's recipes are its trade secret, and this page
 * says exactly what the system does and never does with them. */
const RECIPES: readonly string[] = [
  'When your kitchen is set up from your menu, we infer a draft ingredient list for each dish from general ' +
    'knowledge — the ingredients a dish like that usually has, and how much of each a serving uses.',
  'A draft is only a draft. Your chef or manager reviews it, can change any quantity or line, and must approve ' +
    'it before anything uses it. Until a dish is approved, its list drives no stock deduction, no forecast and ' +
    'no low-stock warning — and the manager\'s Attention feed says plainly how many dishes are still waiting.',
  'We store ingredients and quantities only. That is all the system needs to count stock.',
  'Your cooking process — how a dish is prepared, in what order, with what technique — is never asked for, ' +
    'never stored and never shared. There is no field for it anywhere in the system. It stays your secret.',
]

/** Pass 17: the names promise — back-office AI reasoning runs on per-kitchen codes, and the key never
 * leaves the kitchen's own data. The honest limit is stated in the card itself: quantities travel. */
const EGRESS: readonly string[] = [
  'To plan around a shortage, or to draft a message to a regular, the AI needs your numbers — how much ' +
    'of something is left, how fast it is selling, how many servings a dish takes. It does not need names.',
  'So every ingredient and dish carries a code that means nothing outside your kitchen (like ING_0147 or ' +
    'ITEM_0231). The AI sees the codes and the quantities; its answers are turned back into real names on ' +
    'your own system before anyone here reads them.',
  'The key that matches codes to names is stored only in your kitchen\'s own data, and the codes are yours ' +
    'alone: the same ingredient carries a different code in every other restaurant.',
  'Honestly: quantities and volumes do travel with the codes — the AI cannot reason about your stock ' +
    'without them. What never travels is what your ingredients and dishes are.',
]

interface Row {
  store: string
  holds: string
  kept: string
}

const RETENTION: readonly Row[] = [
  { store: 'Transcripts', holds: 'What a guest said, its English gloss and the reply, per turn',
    kept: '7 days in the database (24 hours in a sandbox kitchen), deleted at the next 04:00 ET cleanup; for the ' +
      'live and demo kitchens, also in up to 3 nightly backup copies and, on Fly.io, in the off-host backups' },
  { store: 'Events', holds: 'Orders, tickets, plans and approvals; never what a guest said, never a phone number',
    kept: 'Call and turn events: 7 days. The rest: until their kitchen is reset or deleted' },
  { store: 'Sandbox kitchens', holds: 'Everything in a judge\'s or visitor\'s own kitchen, feedback notes included',
    kept: 'Deleted 24 hours after last use' },
  { store: 'The public live kitchen', holds: 'Orders from calls to the public number',
    kept: 'Reset every day from 04:00 ET, once no call or rush is running' },
  { store: 'Nightly backups', holds: 'The database, without sandbox kitchens', kept: 'The newest 3 copies' },
  { store: 'Host volume snapshots', holds: 'The data volume, when the app runs on Fly.io',
    kept: 'Fly.io\'s default snapshot retention' },
  { store: 'Off-host backups', holds: 'When the app runs on Fly.io: a daily copy of the database without sandbox ' +
    'kitchens, and the spend records, taken by the founders off the host',
    kept: 'Kept by the founders; no deletion period is set yet' },
  { store: 'Logs', holds: 'Errors and timings with trace ids; no transcripts, phone numbers or Twilio request bodies',
    kept: '7 days' },
  { store: 'Call recordings', holds: 'None of guests or judges: only the founders\' own consented test calls, on the ' +
    'demo kitchen', kept: '30 days (older ones are deleted when the next consented call is recorded)' },
  { store: 'Consented clips', holds: 'Sample and test clips recorded with consent by the founders and friends',
    kept: 'Until the founders delete them; removing a consent stops the clip being served at once (the file itself ' +
      'is not deleted automatically)' },
  { store: 'Call records', holds: 'When a call started and ended, its lane and outcome, the caller hash and last 4 ' +
    'digits', kept: 'Sandbox: deleted with the kitchen. Live and demo kitchens: kept (a daily reset does not delete ' +
    'them)' },
  { store: 'Phone numbers', holds: 'Never stored or logged: a keyed hash (HMAC) and the last 4 digits instead',
    kept: 'With the call record' },
  { store: 'Ingredient lists', holds: 'For each dish: its ingredients and the quantity of each per serving, ' +
    'whether the list was drafted by us or entered by the kitchen, and who approved it and when. Never ' +
    'preparation steps, method or technique',
    kept: 'Kept with the kitchen until it is reset or deleted' },
  { store: 'AI name codes', holds: 'The key matching each ingredient and dish to the code the AI sees in ' +
    'its place — the only place that key exists',
    kept: 'Kept with the kitchen until it is reset or deleted' },
  { store: 'Shelf photos', holds: 'A photo a manager takes to count stock, with its EXIF, XMP and other metadata ' +
    'removed before it leaves the server', kept: 'Never stored: processed in memory and sent to Meta (Muse Spark and ' +
    'SAM 3.1); only its SHA-256 hash is kept, with the stock change a person confirmed' },
  { store: 'Your browser', holds: 'One session cookie naming your kitchen, and the kitchen display\'s language and ' +
    'chime settings. In this tab only: a judge passcode until Start, the kiosk\'s session and cart (to resume within ' +
    '15 minutes), an approval\'s result and the video-mode flag. No tracking or advertising cookies',
    kept: 'The cookie: 24 hours after your last visit. The tab\'s own storage: until the tab closes' },
]

const PROCESSORS: readonly Row[] = [
  { store: 'Meta (Muse models)', holds: 'Speech to transcribe, order text to understand, the GM\'s situation reports ' +
    '(ingredient and dish names replaced by codes), and shelf photos to count stock (Muse Spark and SAM 3.1)',
    kept: 'Standard models with storage off (store: false); files we upload are deleted after use, and a sweeper ' +
      'removes any older than an hour' },
  { store: 'ElevenLabs', holds: 'The text of each spoken reply, to turn it into a voice',
    kept: 'Under ElevenLabs\' API terms; our cache keeps the audio only' },
  { store: 'Twilio', holds: 'Phone calls: the caller\'s number and audio pass through Twilio',
    kept: 'Call logs under Twilio\'s own retention' },
  { store: 'Hosting', holds: 'Everything this page says we keep',
    kept: 'For now a founder\'s laptop, reached through a Cloudflare quick tunnel (Cloudflare carries the traffic). ' +
      'Fly.io is the recommended host for judging, to be decided by Oct 31' },
  { store: 'Google Fonts', holds: 'Your browser loads this site\'s fonts from Google, which sees your IP address and ' +
    'browser', kept: 'Under Google\'s terms; no data from the app' },
  { store: 'Open-weight model provider', holds: 'Not enabled',
    kept: 'If one ever serves parsing during a Meta outage, it is named here first, and only one whose terms say no ' +
      'training and no retention' },
]

/** No minimum width: on a phone the three columns wrap rather than push "Kept for" off-screen. The wrapper scrolls
 * only on the narrowest screens, as a named region a keyboard can reach. */
function RowsTable({ label, head, rows }: { label: string; head: readonly [string, string, string];
  rows: readonly Row[] }) {
  return (
    <div tabIndex={0} role="region" aria-label={label} className="overflow-x-auto">
      <table aria-label={label} className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wider text-muted">
          <tr>{head.map((h) => <th key={h} scope="col" className="py-2 pr-3 font-semibold sm:pr-4">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.store} className="border-t border-line align-top">
              <th scope="row" className="py-2 pr-3 font-semibold sm:pr-4">{r.store}</th>
              <td className="py-2 pr-3 text-muted sm:pr-4">{r.holds}</td>
              <td className="py-2">{r.kept}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function PrivacySurface(_props: SurfaceProps) {
  return (
    <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col">
      <header className="flex flex-col gap-3 px-4 pt-6 pb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Lock aria-hidden className="size-8 text-accent" />
          <h1 className="text-3xl font-bold tracking-tight">Privacy</h1>
          <SampleBadge />
        </div>
        <p className="max-w-3xl text-muted">RestaurantOS AI is a hackathon demo. This page says what it keeps, for how
          long, and who else handles it.</p>
      </header>
      <main className="flex flex-col gap-6 px-4 pb-8">
        <Card panel="privacy-short" title="In short">
          <ul className="flex list-disc flex-col gap-1.5 pl-5">{SHORT.map((s) => <li key={s}>{s}</li>)}</ul>
        </Card>
        <Card panel="recipes" title="Your recipes stay yours">
          <ul className="flex list-disc flex-col gap-1.5 pl-5">{RECIPES.map((s) => <li key={s}>{s}</li>)}</ul>
        </Card>
        <Card panel="egress" title="The AI sees codes, not names">
          <ul className="flex list-disc flex-col gap-1.5 pl-5">{EGRESS.map((s) => <li key={s}>{s}</li>)}</ul>
        </Card>
        <Card panel="retention" title="What we keep, and for how long">
          <RowsTable label="Retention by store" head={['Store', 'What it holds', 'Kept for']} rows={RETENTION} />
        </Card>
        <Card panel="processors" title="Who else handles your data">
          <RowsTable label="Processors" head={['Who', 'What they receive', 'Terms']} rows={PROCESSORS} />
        </Card>
      </main>
      <footer {...CHROME} className="flex flex-wrap gap-x-4 gap-y-2 border-t border-line px-4 py-4 text-sm text-muted">
        <Link to="/proof" className="underline">Proof</Link>
        <Link to="/status" className="underline">Status</Link>
        <Link to="/" className="underline">Back to start</Link>
      </footer>
    </div>
  )
}
