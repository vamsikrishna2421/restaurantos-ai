/**
 * The published eval report on /proof (P36; MASTER_PLAN §6): the cell tables by language × channel with N, the fast
 * path with and without carrier words, keyword lift, Muse Spark 1.3 vs 1.2, the GM against the Playbook, the misses
 * and "What we learned about Muse models". A measure the report does not carry yet says so in words; it never shows a
 * zero standing in for "not measured". Whatever was measured on the synthetic E1 text set (cells, the fast path, the
 * E1 run's learnings) is labelled "synthetic, same model family", and its learnings are in smaller type.
 */

import { CircleCheck, CircleX, Lightbulb } from 'lucide-react'
import type { ReactNode } from 'react'
import { Badge, Card, cx } from '../../components/ui'
import type { EvalCell, EvalMiss, EvalReport, KeywordLift, Learning } from '../../contracts/generated'
import { formatLatency, formatMicros, formatNumber } from '../../lib/format'
import {
  channelName,
  f1,
  learningValue,
  measuredOn,
  perMille,
  splitCells,
  styleName,
  SYNTHETIC_SCRIPT,
  totalN,
} from './model'

// cost_micros_per_order (evals/report.py) is the mean Meta cost of one case, and each case is one parse
const HEAD = ['Language', 'Channel', 'N', 'Exact match', 'Item F1', 'Modifier F1', 'Qty F1', 'p50', 'p95',
  'Cost per parse']

const SyntheticBadge = () => <Badge tone="ai">synthetic, same model family</Badge>

/** One table of cells; `size` sets the type: real results large, synthetic ones small (publication rule). Language and
 * channel are both row headers, so a screen reader names the pair with every number. */
export function CellTable({ cells, label, size }: { cells: readonly EvalCell[]; label: string; size: 'lg' | 'sm' }) {
  const pad = size === 'lg' ? 'px-2 py-2' : 'px-2 py-1'
  return (
    // a named, focusable scroll region, so a keyboard can reach the columns a phone cuts off
    <div tabIndex={0} role="region" aria-label={label} className="overflow-x-auto">
      <table aria-label={label} data-size={size}
        className={cx('w-full min-w-[46rem] text-left tabular-nums', size === 'lg' ? 'text-base' : 'text-xs')}>
        <thead className="text-xs uppercase tracking-wider text-muted">
          <tr>{HEAD.map((h) => <th key={h} scope="col" className={cx(pad, 'font-semibold')}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {cells.map((c) => (
            <tr key={`${c.language}|${c.channel}|${c.source}`} data-source={c.source} className="border-t border-line">
              <th scope="row" className={cx(pad, 'font-medium')}>{styleName(c.language)}</th>
              <th scope="row" className={cx(pad, 'font-normal')}>{channelName(c.channel)}</th>
              <td className={pad} data-col="n">{formatNumber(c.n)}</td>
              <td className={cx(pad, 'font-semibold')}>{perMille(c.exact_match_pct_x10)}</td>
              <td className={pad}>{f1(c.item_f1_x1000)}</td>
              <td className={pad}>{f1(c.modifier_f1_x1000)}</td>
              <td className={pad}>{f1(c.qty_f1_x1000)}</td>
              <td className={pad}>{formatLatency(c.p50_ms)}</td>
              <td className={pad}>{formatLatency(c.p95_ms)}</td>
              <td className={pad}>{formatMicros(c.cost_micros_per_order)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Real recordings first and large; synthetic text after, smaller, labelled; then every caveat of the run. */
export function CellSections({ report }: { report: EvalReport }) {
  const { real, synthetic } = splitCells(report.cells)
  return (
    <>
      <Card panel="real" title="Real recordings" tone={real.length > 0 ? 'ok' : undefined}>
        {real.length > 0 ? (
          <>
            <p className="mb-3 text-muted">{`Recordings of real people, scored on Standard models: ` +
              `${formatNumber(totalN(real))} cases in all.`}</p>
            <CellTable cells={real} label="Real recordings" size="lg" />
          </>
        ) : (
          <p data-testid="no-real" className="text-lg">
            <span className="font-semibold">No real consented-clip results yet.</span>{' '}
            <span className="text-muted">The accuracy table below comes from synthetic cases. Results from consented
              recordings will appear here, above it, once they are scored.</span>
          </p>
        )}
      </Card>
      {synthetic.length > 0 && (
        <Card panel="synthetic" title={<span className="flex flex-wrap items-center gap-2">Synthetic cases
          <SyntheticBadge /></span>}>
          <p className="mb-2 text-sm text-muted">{`${formatNumber(totalN(synthetic))} cases in all, generated rather ` +
            'than recorded. The caveats below say how, and what exact match and F1 count.'}</p>
          <CellTable cells={synthetic} label="Synthetic cases" size="sm" />
          <p className="mt-2 text-xs text-muted">Cost per parse: the mean Meta cost of one parse; text only, so no
            speech recognition or voice.</p>
        </Card>
      )}
      <Card panel="caveats" title="Caveats">
        {report.caveats.length > 0 ? (
          <ul aria-label="Caveats" className="flex list-disc flex-col gap-1 pl-5 text-sm">
            {report.caveats.map((c) => <li key={c}>{c}</li>)}
          </ul>
        ) : <p className="text-sm text-muted">The report lists no caveats.</p>}
      </Card>
    </>
  )
}

function Stat({ label, value }: { label: ReactNode; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-line py-1.5 first:border-t-0">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="font-mono text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

const NotYet = ({ children }: { children: ReactNode }) => <p className="text-sm text-muted">{children}</p>

const points = (d: number) => learningValue({ value: d, unit: 'pct_points_x10' })

function LiftTable({ rows }: { rows: readonly KeywordLift[] }) {
  return (
    <table aria-label="Keyword lift" className="w-full text-left text-sm tabular-nums">
      <thead className="text-xs uppercase tracking-wider text-muted">
        <tr><th scope="col">Language</th><th scope="col">Channel</th><th scope="col">N</th><th scope="col">Without</th>
          <th scope="col">With</th><th scope="col">Lift</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.language}|${r.channel}`} className="border-t border-line">
            <th scope="row" className="py-1 font-medium">{styleName(r.language)}</th>
            <th scope="row" className="font-normal">{channelName(r.channel)}</th>
            <td>{formatNumber(r.n)}</td><td>{perMille(r.recall_without_x1000)}</td>
            <td>{perMille(r.recall_with_x1000)}</td><td>{points(r.recall_with_x1000 - r.recall_without_x1000)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** The fast path, keyword lift, 1.3 vs 1.2 and the GM eval: each with N, or "not measured yet". */
export function Measures({ report }: { report: EvalReport }) {
  const { fast_path: fp, keyword_lift: lift, models, gm } = report
  const ab = models.ab
  const abFacts = ab ? report.learnings.filter((l) => l.claim.includes(ab)) : []
  const failed = gm.checks.filter((c) => !c.ok)
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Card panel="fast-path" title={<span className="flex flex-wrap items-center gap-2">Fast path, with and without
        carrier words <SyntheticBadge /></span>}>
        <dl>
          <Stat label="Coverage with carrier words stripped" value={perMille(fp.coverage_with_carriers_x1000)} />
          <Stat label="Coverage as written" value={perMille(fp.coverage_x1000)} />
          <Stat label="Precision of what it parsed" value={perMille(fp.precision_x1000)} />
        </dl>
        <p className="mt-2 text-xs text-muted">The fast path reads simple orders in code, with no model call. Carrier
          words are fillers such as "please" or "can I get". Measured over every case of the synthetic E1 text set.</p>
      </Card>
      <Card panel="keyword-lift" title="Keyword lift in speech recognition">
        {lift.length > 0 ? <LiftTable rows={lift} /> : <NotYet>Not measured yet: keyword lift comes from the audio eval
          (E2), which is not in this report.</NotYet>}
      </Card>
      <Card panel="ab" title="Muse Spark 1.3 vs 1.2">
        <p className="text-sm">Headline model: <code className="font-mono">{models.headline}</code></p>
        {!ab ? (
          <NotYet>No A/B run on Muse Spark 1.2 yet: the parse numbers above are all from {models.headline}.</NotYet>
        ) : abFacts.length > 0 ? (
          <ul className="mt-2 flex flex-col gap-1 text-sm">
            {abFacts.map((l) => <li key={l.claim}>{`${l.claim}: ${learningValue(l)} (N=${formatNumber(l.n)})`}</li>)}
          </ul>
        ) : (
          <NotYet>{`The report names ${ab} as the A/B model but publishes no separate numbers for it yet.`}</NotYet>
        )}
      </Card>
      <Card panel="gm-eval" title="AI GM vs the rule-based Playbook">
        {gm.n === 0 ? <NotYet>Not measured yet: the GM eval (E3) is not in this report.</NotYet> : (
          <>
            <dl>
              <Stat label={`AI plan won (simulated outcomes, N=${formatNumber(gm.n)} fixtures)`}
                value={perMille(gm.win_rate_x1000)} />
              <Stat label="Checks passed" value={`${gm.checks.length - failed.length} of ${gm.checks.length}`} />
            </dl>
            {failed.length > 0 && (
              <ul aria-label="Failed GM checks" className="mt-2 flex flex-col gap-1 text-sm text-bad">
                {failed.map((c) => <li key={`${c.fixture}|${c.check}`}>{`${c.fixture}: ${c.check}`}</li>)}
              </ul>
            )}
          </>
        )}
      </Card>
    </div>
  )
}

/** "What we learned about Muse models": each measured fact with its number, N, date and script. A fact from the E1 run
 * (synthetic text) is in smaller type with the synthetic label; a speech fact says what it was measured on. */
export function Learnings({ learnings }: { learnings: readonly Learning[] }) {
  if (learnings.length === 0) return null
  return (
    <Card panel="learnings" tone="ai" title={<span className="inline-flex items-center gap-2">
      <Lightbulb aria-hidden className="size-5 text-ai-2" />What we learned about Muse models</span>}>
      <ul aria-label="What we learned about Muse models" className="grid gap-3 md:grid-cols-2">
        {learnings.map((l, i) => {
          const synthetic = l.script === SYNTHETIC_SCRIPT
          const note = measuredOn(l.script)
          return (
            <li key={`${i}:${l.claim}`} data-size={synthetic ? 'sm' : 'lg'}
              className="flex flex-col gap-1 rounded-xl bg-panel-2 p-3">
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className={cx('font-mono font-bold tabular-nums text-ai-2', synthetic ? 'text-lg' : 'text-2xl')}>
                  {learningValue(l)}</span>
                {synthetic && <SyntheticBadge />}
              </span>
              <span className="text-sm">{l.claim}</span>
              <span className="text-xs text-muted">
                {`N=${formatNumber(l.n)} · ${l.date} · `}<code className="font-mono">{l.script}</code>
              </span>
              {note && <span className="text-xs text-muted">{note}</span>}
            </li>
          )
        })}
      </ul>
    </Card>
  )
}

/** Every exact-match miss the report lists: the English gloss, the right cart and what came back. */
export function Misses({ misses }: { misses: readonly EvalMiss[] }) {
  return (
    <Card panel="misses" title={`Misses (${misses.length})`}>
      {misses.length === 0 ? <p className="text-sm text-muted">The report lists no misses.</p> : (
        <>
          <p className="mb-3 text-sm text-muted">Every case the parser got wrong, as published: what the guest said (in
            English), the cart it should have produced, and what came back.</p>
          <ol aria-label="Misses" className="flex flex-col gap-2">
            {misses.map((m) => (
              <li key={m.id} data-miss={m.id} className="rounded-xl bg-panel-2 px-3 py-2 text-sm">
                <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted">
                  <span className="font-mono">{m.id}</span>{styleName(m.language)}
                </p>
                <p className="my-1">{`“${m.utterance_en}”`}</p>
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 font-mono text-xs">
                  <dt className="inline-flex items-center gap-1 text-ok">
                    <CircleCheck aria-hidden className="size-3.5" />Expected</dt>
                  <dd className="break-words">{m.expected}</dd>
                  <dt className="inline-flex items-center gap-1 text-bad">
                    <CircleX aria-hidden className="size-3.5" />Got</dt>
                  <dd className="break-words">{m.got}</dd>
                </dl>
              </li>
            ))}
          </ol>
        </>
      )}
    </Card>
  )
}
