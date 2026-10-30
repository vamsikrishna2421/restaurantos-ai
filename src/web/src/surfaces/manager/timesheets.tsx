/**
 * The Timesheets section (P42): one week of the shift record (`/api/manager/timesheets`). Claiming a job
 * is attendance, so the record is the claim log itself — a shift still open reads "on shift now" and its
 * minutes run to the moment this page was loaded. The server's note states the record's limit: shifts
 * that ended before the record began were never kept, and are missing rather than estimated.
 */

import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Chip } from '../../components/ui'
import type { ConsoleTimesheets } from '../../contracts/generated'
import { formatBizMs } from '../../lib/format'
import { managerApi } from '../../lib/manager'
import { dayLabel, SectionState, shiftLength } from './shared'

function shiftWeek(isoDate: string, deltaDays: number): string {
  const parsed = new Date(`${isoDate}T12:00:00`)
  parsed.setDate(parsed.getDate() + deltaDays)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`
}

export default function TimesheetsSection() {
  const [data, setData] = useState<ConsoleTimesheets | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(async (week?: string) => {
    try {
      setData(await managerApi.timesheets(week))
      setError(false)
    } catch {
      setError(true)
    }
  }, [])

  useEffect(() => {
    void load(undefined)
  }, [load])

  return (
    <SectionState loading={data === null && !error} error={error}>
      {data && (
        <div className="flex flex-col gap-6">
          <Card title={`Week of ${dayLabel(data.week_start)}`} className="motion-safe:animate-rise"
            actions={(
              <span className="flex items-center gap-2">
                <Button size="sm" variant="ghost"
                  onClick={() => void load(shiftWeek(data.week_start, -7))}>Earlier week</Button>
                <Button size="sm" variant="ghost"
                  onClick={() => void load(shiftWeek(data.week_start, 7))}>Later week</Button>
                <Button size="sm" variant="ghost" onClick={() => void load(undefined)}>This week</Button>
              </span>
            )}>
            {data.people.length === 0 ? (
              <p className="text-muted">No shifts were recorded this week.</p>
            ) : (
              <ul className="flex flex-col gap-4">
                {data.people.map((person) => (
                  <li key={person.staff_id}>
                    <p className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
                      <span className="font-semibold">{person.name}</span>
                      <span className="text-sm tabular-nums text-muted">
                        Week total: <span className="font-semibold text-text">{shiftLength(person.week_minutes)}</span>
                      </span>
                    </p>
                    <ul className="flex flex-col gap-1">
                      {person.days.map((day) => (
                        <li key={day.date}
                          className="rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                          <span className="text-sm font-medium">{dayLabel(day.date)}</span>
                          <ul className="mt-1 flex flex-col gap-0.5 text-sm text-muted">
                            {day.shifts.map((shift, i) => (
                              <li key={`${shift.slot_id}-${i}`} className="flex flex-wrap items-center gap-2">
                                <span>
                                  {shift.slot_label} · {formatBizMs(Date.parse(shift.started_at))}
                                  {' – '}
                                  {shift.ended_at === null
                                    ? 'still on shift'
                                    : formatBizMs(Date.parse(shift.ended_at))}
                                  {' · '}{shiftLength(shift.minutes)}
                                </span>
                                {shift.in_progress && <Chip tone="ok">On shift now</Chip>}
                              </li>
                            ))}
                          </ul>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-4 text-sm text-muted">{data.note}</p>
          </Card>
        </div>
      )}
    </SectionState>
  )
}
