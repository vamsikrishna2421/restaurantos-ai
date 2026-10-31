/**
 * The People section (P41/P42): the roster, who is on shift right now, and the pass-5 management —
 * job requests waiting on the manager, and everyone's usual job. All of it rides the staff API
 * (`/api/staff…`); this section owns no second copy of that logic.
 */

import { Avatar, Button, Card, Chip } from '../../components/ui'
import type { RoleRequest, RoleSlot, StaffPerson } from '../../contracts/generated'
import { LANGUAGE_NAMES } from '../../lib/i18n'

const PEOPLE_GRID = 'grid gap-3 sm:grid-cols-2 xl:grid-cols-3'

/** Standing responsibilities a person can hold on top of their job (pass 13), by contract name. */
export const RESPONSIBILITY_LABELS: Record<string, string> = {
  inventory_manager: 'Inventory Manager',
}

export interface PeopleSectionProps {
  isManager: boolean
  slots: RoleSlot[] | null
  requests: RoleRequest[] | null
  people: StaffPerson[]
  busy: string | null
  onDecide: (request: RoleRequest, approve: boolean) => void
  onChangeDefault: (person: StaffPerson, slotId: string) => void
  onToggleResponsibility: (person: StaffPerson, grant: boolean) => void
}

export default function PeopleSection({ isManager, slots, requests, people, busy, onDecide,
  onChangeDefault, onToggleResponsibility }: PeopleSectionProps) {
  const heldBy = new Map((slots ?? []).filter((s) => s.holder).map((s) => [s.holder!.staff_id, s]))
  return (
    <div className="flex flex-col gap-6">
      {/* pass 8 (Scope C): the job requests stay decision cards, above the roster grid */}
      {isManager && requests !== null && (
        <Card title="Job requests" tone={requests.length > 0 ? 'warn' : undefined}
          className="motion-safe:animate-rise"
          actions={requests.length > 0 ? <Chip tone="warn">{requests.length}</Chip> : undefined}>
          {requests.length === 0 ? (
            <p className="text-muted">No job requests are waiting.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {requests.map((request) => (
                <li key={request.request_id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-warn/8 px-3.5 py-2.5 ring-1 ring-warn/25">
                  <span className="flex min-w-0 items-center gap-2.5">
                    <Avatar name={request.staff_name} className="size-7 text-xs" />
                    <span className="truncate">
                      <span className="font-semibold">{request.staff_name}</span>
                      {' — '}{request.label}
                      <span className="block text-sm text-muted">{request.detail}</span>
                    </span>
                  </span>
                  <span className="flex gap-2">
                    <Button size="sm" variant="primary" loading={busy === request.request_id}
                      onClick={() => onDecide(request, true)}>
                      Approve
                    </Button>
                    <Button size="sm" variant="ghost" loading={busy === request.request_id}
                      onClick={() => onDecide(request, false)}>
                      Decline
                    </Button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <Card title="Team" className="motion-safe:animate-rise">
        {people.length === 0 ? (
          <p className="text-muted">The team list could not be loaded.</p>
        ) : (
          <ul className={PEOPLE_GRID}>
            {people.map((person) => {
              const held = heldBy.get(person.id)
              return (
                <li key={person.id}
                  className="flex flex-col gap-2.5 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
                  <span className="flex min-w-0 items-center gap-2.5">
                    <Avatar name={person.name} className="size-9 text-sm" />
                    <span className="truncate text-base font-semibold">{person.name}</span>
                  </span>
                  <span className="text-sm leading-relaxed text-muted">
                    <span className="block">
                      Usual job: {person.default_role
                        ? `${person.default_role.label}${person.default_role.pending ? ' (waiting for the manager’s OK)' : ''}`
                        : 'not chosen yet'}
                    </span>
                    <span className="block">{LANGUAGE_NAMES[person.language] ?? person.language}</span>
                  </span>
                  {person.responsibilities.length > 0 && (
                    <span className="flex flex-wrap gap-1.5">
                      {person.responsibilities.map((responsibility) => (
                        <Chip key={responsibility} tone="accent">
                          {RESPONSIBILITY_LABELS[responsibility] ?? responsibility}
                        </Chip>
                      ))}
                    </span>
                  )}
                  <span className="mt-auto">
                    {held
                      ? <Chip tone="ok">On shift: {held.label}</Chip>
                      : <Chip>Not on shift</Chip>}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </Card>

      {isManager && people.length > 0 && slots !== null && (
        <Card title="Team jobs" className="motion-safe:animate-rise">
          <ul className={PEOPLE_GRID}>
            {people.map((person) => (
              <li key={person.id}
                className="flex flex-col gap-2.5 rounded-xl bg-white/2 px-3.5 py-3 ring-1 ring-white/5">
                <span className="flex min-w-0 items-center gap-2.5">
                  <Avatar name={person.name} className="size-7 text-xs" />
                  <span className="truncate font-semibold">{person.name}</span>
                </span>
                {person.default_role?.pending ? (
                  <Chip tone="warn">Asked for {person.default_role.label} — see Job requests</Chip>
                ) : (
                  <label className="flex flex-wrap items-center gap-2 text-sm text-muted">
                    Usual job
                    <select aria-label={`Usual job for ${person.name}`}
                      value={person.default_role?.slot_id ?? ''}
                      disabled={busy === `default:${person.id}`}
                      onChange={(e) => onChangeDefault(person, e.target.value)}
                      className="rounded-lg border border-white/10 bg-panel-2 px-2.5 py-2 font-medium text-text outline-none transition-[border-color,box-shadow] focus:border-accent/60 focus:ring-4 focus:ring-accent/15">
                      <option value="">No usual job</option>
                      {slots.filter((s) => !s.pending_approval).map((slot) => (
                        <option key={slot.slot_id} value={slot.slot_id}>{slot.label}</option>
                      ))}
                    </select>
                  </label>
                )}
                {/* pass 13: a standing responsibility lives next to the job, never instead of it */}
                <span className="flex flex-wrap items-center gap-2 text-sm text-muted">
                  Inventory Manager
                  {person.responsibilities.includes('inventory_manager') ? (
                    <>
                      <Chip tone="accent">Holds it</Chip>
                      <Button size="sm" variant="ghost" loading={busy === `resp:${person.id}`}
                        onClick={() => onToggleResponsibility(person, false)}>
                        Take back
                      </Button>
                    </>
                  ) : (
                    <Button size="sm" variant="secondary" loading={busy === `resp:${person.id}`}
                      onClick={() => onToggleResponsibility(person, true)}>
                      Give responsibility
                    </Button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
