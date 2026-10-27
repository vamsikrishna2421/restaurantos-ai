/**
 * Front-of-house tasks (CONTRACTS §7: a pickup PO or a callback flag creates one): Done, or "Can't go" with a reason.
 * "Can't go" marks the task blocked (REST #53), which the GM watches and re-plans around (P1-2).
 */

import { Ban, Check, ClipboardList, PhoneCall, Truck } from 'lucide-react'
import { useState } from 'react'
import { Badge, Button, Card, cx } from '../../components/ui'
import type { StaffTask, TaskKind } from '../../contracts/generated'

export const BLOCK_REASONS = ['No one free to go', 'Supplier closed or sold out', 'No car, or too far'] as const

const KINDS: Readonly<Record<TaskKind, { label: string; Icon: typeof Truck }>> = {
  pickup: { label: 'Pickup', Icon: Truck },
  callback: { label: 'Callback', Icon: PhoneCall },
  other: { label: 'Task', Icon: ClipboardList },
}

export interface FohTasksProps {
  tasks: readonly StaffTask[]
  busy: ReadonlySet<string>
  onDone: (task: StaffTask) => void
  onBlocked: (task: StaffTask, reason: string) => void
}

export function FohTasks({ tasks, busy, onDone, onBlocked }: FohTasksProps) {
  const [asking, setAsking] = useState<string | null>(null)
  if (tasks.length === 0) return null
  const title = (
    <span className="inline-flex items-center gap-2">
      <Truck aria-hidden className="size-5 text-accent" />
      Front of house
    </span>
  )
  return (
    <Card title={title} panel="tasks" bodyClassName="space-y-3 p-3">
      {tasks.map((task) => {
        const kind = KINDS[task.kind] ?? KINDS.other
        const blocked = task.status === 'blocked'
        return (
          <article key={task.id} aria-label={`${kind.label} task`} data-task={task.id} data-status={task.status}
            className={cx('rounded-xl border p-3', blocked ? 'border-warn/60 bg-warn/10' : 'border-line bg-panel-2')}>
            <p className="flex flex-wrap items-center gap-2">
              <Badge tone={blocked ? 'warn' : 'accent'} icon={<kind.Icon className="size-3.5" />}>{kind.label}</Badge>
              {blocked && <Badge tone="warn" icon={<Ban className="size-3.5" />}>Can't go</Badge>}
            </p>
            <p className="mt-2 text-lg font-semibold leading-snug">{task.note}</p>
            {blocked && task.blocked_reason && (
              <p className="mt-1 text-sm font-semibold text-warn">Reported: {task.blocked_reason}</p>
            )}
            {asking === task.id ? (
              <div role="group" aria-label="Why can't you go?" className="mt-3 flex flex-wrap gap-2">
                {BLOCK_REASONS.map((reason) => (
                  <Button key={reason} variant="danger" onClick={() => {
                    setAsking(null)
                    onBlocked(task, reason)
                  }}>
                    {reason}
                  </Button>
                ))}
                <Button variant="ghost" onClick={() => setAsking(null)}>Cancel</Button>
              </div>
            ) : (
              <div className="mt-3 flex flex-wrap gap-2">
                <Button variant="ok" size="lg" className="flex-1" icon={<Check />} loading={busy.has(task.id)}
                  onClick={() => onDone(task)}>
                  Done
                </Button>
                {!blocked && (
                  <Button size="lg" icon={<Ban />} disabled={busy.has(task.id)} onClick={() => setAsking(task.id)}>
                    Can't go
                  </Button>
                )}
              </div>
            )}
          </article>
        )
      })}
    </Card>
  )
}
