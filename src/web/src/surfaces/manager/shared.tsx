/** Shared bits of the manager console sections (P42): loading/error states and small formatters. */

import { Card } from '../../components/ui'

export function SectionState({ loading, error, children }: {
  loading: boolean
  error: boolean
  children: React.ReactNode
}) {
  if (error) {
    return (
      <Card>
        <p role="alert" className="text-bad">This section could not be loaded. Try Refresh.</p>
      </Card>
    )
  }
  if (loading) {
    return (
      <Card>
        <p className="text-muted">Loading…</p>
      </Card>
    )
  }
  return <>{children}</>
}

const DAY = new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric' })

/** "Sat, Nov 7" for an ISO date ("2026-11-07"), read at noon so no zone shifts the day. */
export function dayLabel(isoDate: string): string {
  const parsed = new Date(`${isoDate}T12:00:00`)
  return Number.isNaN(parsed.getTime()) ? isoDate : DAY.format(parsed)
}

/** Whole minutes as a shift length: "2 h 30 min", "45 min", "3 h". */
export function shiftLength(minutes: number): string {
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  if (h === 0) return `${m} min`
  return m === 0 ? `${h} h` : `${h} h ${m} min`
}
