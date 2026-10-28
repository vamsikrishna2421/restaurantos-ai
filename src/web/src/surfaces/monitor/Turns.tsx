/**
 * One guest turn on the Call Monitor: what the guest said (native script) with its English gloss, the reply, the path
 * chip, LIVE model chips from the turn's `ai.call`s, its AI cost, and the latency HUD (stage bar plus badges against
 * the §7.4 budgets). Turn text comes from `turn.text` or the turn's trace; nothing here is model output rendered as
 * fact: every number is a measured span or a metered cost.
 */

import { CornerDownRight } from 'lucide-react'
import { LatencyBadge, ModelChip } from '../../components/badges'
import { Badge, Chip, cx } from '../../components/ui'
import type { Channel, TurnPath, TurnSpans } from '../../contracts/generated'
import { formatMicros } from '../../lib/format'
import {
  langAttr,
  LANG_NAMES,
  modelChip,
  PATH_INFO,
  stageDurations,
  turnTotal,
  type StageKey,
  type TurnRecord,
} from './model'

const SEGMENT: Readonly<Record<StageKey, string>> = {
  asr: 'bg-accent-2', handoff: 'bg-muted', parse: 'bg-ai', tts: 'bg-accent', out: 'bg-ok',
}

export function PathChip({ path }: { path: TurnPath }) {
  const info = PATH_INFO[path]
  return <Chip tone={info.tone} title={info.meaning} className="px-2.5 py-0.5 text-xs">{info.label}</Chip>
}

/** An AI cost in dollars (integer micro-dollars), or nothing when unknown. */
export function Cost({ micros, label = 'AI' }: { micros: number | null; label?: string }) {
  if (micros === null) return null
  return (
    <span title="Metered AI cost (Meta models)" className="font-mono text-xs tabular-nums text-muted">
      {`${label} ${formatMicros(micros)}`}
    </span>
  )
}

export interface LatencyHudProps {
  spans: TurnSpans | null
  path: TurnPath | null
  channel: Channel | null
}

/** The per-turn latency HUD: the stages of one turn as a proportional bar and as badges, headline first. */
export function LatencyHud({ spans, path, channel }: LatencyHudProps) {
  if (spans === null) {
    return <p className="text-xs text-muted">Latency shows once this turn's trace is recorded.</p>
  }
  const stages = stageDurations(spans, path).filter((s) => s.ms !== null)
  const total = turnTotal(spans, path, channel)
  if (stages.length === 0 && total === null) return <p className="text-xs text-muted">No timings for this turn.</p>
  const sum = stages.reduce((acc, s) => acc + (s.ms ?? 0), 0)
  return (
    <div role="group" aria-label="Latency" className="flex flex-col gap-1.5">
      {sum > 0 && (
        <div aria-hidden className="flex h-2 w-full overflow-hidden rounded-full bg-panel-2">
          {stages.map((s) => (
            <span key={s.key} className={SEGMENT[s.key]} style={{ width: `${((s.ms ?? 0) / sum) * 100}%` }} />
          ))}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        {total && <LatencyBadge label={total.label} ms={total.ms} targetMs={total.targetMs} className="font-bold" />}
        {stages.map((s) => (
          <span key={s.key} title={s.meaning} className="inline-flex items-center gap-1">
            <span aria-hidden className={cx('size-2 rounded-full', SEGMENT[s.key])} />
            <LatencyBadge label={s.label} ms={s.ms} targetMs={s.targetMs} />
          </span>
        ))}
      </div>
    </div>
  )
}

function Said({ turn }: { turn: TurnRecord }) {
  const lang = langAttr(turn.language)
  return (
    <div className="flex flex-col gap-1">
      {turn.transcript !== null ? (
        <p lang={lang} className="text-lg leading-snug">{`“${turn.transcript}”`}</p>
      ) : (
        <p className="text-sm italic text-muted">The text of this turn has not arrived yet.</p>
      )}
      {turn.utteranceEn && turn.utteranceEn !== turn.transcript && (
        <p lang="en" className="text-base text-accent-2">{turn.utteranceEn}</p>
      )}
      {turn.replyText && (
        <p className="flex items-start gap-2 text-base">
          <CornerDownRight aria-hidden className="mt-1 size-4 shrink-0 text-muted" />
          <span>
            <span className="sr-only">Reply: </span>
            {turn.replyText}
            {turn.replyEn && turn.replyEn !== turn.replyText && (
              <span lang="en" className="ml-2 text-muted">{turn.replyEn}</span>
            )}
          </span>
        </p>
      )}
    </div>
  )
}

export function TurnRow({ turn }: { turn: TurnRecord }) {
  return (
    <li aria-label={`Turn ${turn.turnNo}`} data-turn={turn.turnId}
      className="flex flex-col gap-2 border-b border-line py-3 first:pt-0 last:border-b-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-semibold text-muted">{`Turn ${turn.turnNo}`}</span>
        {turn.path && <PathChip path={turn.path} />}
        {turn.intent && <Badge>{turn.intent.replace(/_/g, ' ')}</Badge>}
        {turn.closing && <Badge tone="accent" title="The guest said they were done">closing</Badge>}
        {turn.language && <span className="text-xs text-muted">{LANG_NAMES[turn.language]}</span>}
        {turn.calls.map((call, i) => <ModelChip key={`${i}:${call.model}`} {...modelChip(call)} />)}
        <Cost micros={turn.costMicros} />
      </div>
      <Said turn={turn} />
      <LatencyHud spans={turn.spans} path={turn.path} channel={turn.channel} />
    </li>
  )
}
