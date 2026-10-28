/**
 * The dry run (ARCHITECTURE §6.6): a deterministic 60-minute projection without and with the plan's steps, told in
 * dollars and guest requests, always beside its assumptions and labelled "Simulation — assumptions shown". It follows
 * the preview of the steps currently ticked when there is one, else the simulation stored with the plan.
 */

import { FlaskConical } from 'lucide-react'
import type { ReactNode } from 'react'
import { Badge, Card, Chip, cx } from '../../components/ui'
import type { SimulationDiff } from '../../contracts/generated'
import { formatBizTime, formatCents } from '../../lib/format'
import { tenths } from './model'

export const SIM_LABEL = 'Simulation — assumptions shown'

const CHANNELS: Readonly<Record<string, string>> = {
  kiosk: 'Kiosk', phone: 'Phone', counter: 'Counter', typed: 'Typed', kds: 'Kitchen screens',
  supplier_inbox: 'Supplier inbox',
}

function Stat({ label, value, sub, money = false }: { label: string; value: ReactNode; sub?: string;
  money?: boolean }) {
  return (
    <div className="rounded-lg bg-panel-2/70 px-3 py-2">
      <dt className="text-[0.7rem] font-bold uppercase tracking-wider text-muted">{label}</dt>
      <dd className={cx('font-mono text-xl font-bold tabular-nums', money && 'text-accent-2')}>{value}</dd>
      {sub && <dd className="text-xs text-muted">{sub}</dd>}
    </div>
  )
}

function stockout(biz: string | null, horizon: number): string {
  return biz ? formatBizTime(biz) : `none in ${horizon} min`
}

export function DryRunPanel({ sim, selected }: { sim: SimulationDiff | null; selected: boolean }) {
  return (
    <Card panel="dryrun" aria-label="Dry run" title="Dry run"
      actions={<Badge tone="ai" icon={<FlaskConical />}>{SIM_LABEL}</Badge>}>
      {sim === null ? <p className="text-sm text-muted">The dry run appears with the plan.</p> : (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-muted">{`Next ${sim.horizon_min} business minutes, without and with the plan`}</p>
          <p data-testid="sim-headline" className="text-xl font-bold leading-snug">
            <span className="text-ok">{`${sim.open_orders_protected}/${sim.open_orders_total}`}</span>
            {' open orders '}
            <span className="text-accent-2">{`(${formatCents(sim.protected_value_cents)})`}</span>
            {' protected'}
          </p>
          <dl className="grid grid-cols-2 gap-2">
            <Stat label="Requests redirected" value={`~${tenths(sim.expected_redirected_x10)}`}
              sub="to the alternatives" />
            <Stat label="Requests lost" value={`~${tenths(sim.expected_lost_with_x10)}`}
              sub={`~${tenths(sim.expected_lost_without_x10)} without the plan`} />
            <Stat label="PO cost" value={formatCents(sim.po_cost_cents)} money />
            <Stat label="Pickup quotes" value={sim.quote_change_min ? `+${sim.quote_change_min} min` : 'no change'} />
          </dl>
          <p className="text-sm">
            <span className="text-muted">Runs out: </span>
            <span className="font-semibold">{stockout(sim.stockout_biz_without, sim.horizon_min)}</span>
            <span className="text-muted"> without the plan → </span>
            <span className="font-semibold text-ok">{stockout(sim.stockout_biz_with, sim.horizon_min)}</span>
            <span className="text-muted"> with it</span>
          </p>
          {sim.channels_changed.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted">Changes reach</span>
              {sim.channels_changed.map((c) => (
                <Chip key={c} className="px-2 py-0.5 text-xs">{CHANNELS[c] ?? c}</Chip>
              ))}
            </div>
          )}
          <div>
            <p className="text-[0.7rem] font-bold uppercase tracking-wider text-muted">
              {selected ? 'Assumptions (for the steps ticked now)' : 'Assumptions'}
            </p>
            <ul aria-label="Assumptions" className="mt-1 list-disc pl-5 text-sm text-muted">
              {sim.assumptions.map((a, i) => <li key={`${i}:${a}`}>{a}</li>)}
            </ul>
          </div>
        </div>
      )}
    </Card>
  )
}
