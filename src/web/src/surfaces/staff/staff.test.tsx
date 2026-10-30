// @vitest-environment jsdom
// Staff sign-in (P41): pick your name → PIN pad → today's job. A taken job cannot be claimed; a wrong PIN is a
// plain refusal; a new teammate signs up with a name, a language and a PIN they choose.
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { RoleSlot, StaffPerson } from '../../contracts/generated'
import { resetSessionForTests } from '../../stores/session'
import { fail, json, stubFetch, SESSION, type Call } from '../../test/story-kit'
import StaffSurface from '.'

const LAKSHMI: StaffPerson = { id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te' }
const CARLOS: StaffPerson = { id: 'STF_CARLOS', name: 'Carlos', language: 'es' }
const PEOPLE = [LAKSHMI, CARLOS]

const slot = (over: Partial<RoleSlot>): RoleSlot => ({
  slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null,
  holder: null, mine: false, ...over,
})
const SLOTS: RoleSlot[] = [
  slot({}),
  slot({
    slot_id: 'STN_BIRYANI#1', label: 'Biryani Chef 1', kind: 'chef', station_id: 'STN_BIRYANI',
  }),
  slot({
    slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', kind: 'chef', station_id: 'STN_CURRY',
    holder: { staff_id: CARLOS.id, name: 'Carlos' },
  }),
]

let calls: Call[] = []

function respond(call: Call): Response {
  const { method, path } = call
  if (method === 'GET' && path === '/api/session') return json(200, SESSION)
  if (method === 'GET' && path === '/api/staff') return json(200, PEOPLE)
  if (method === 'POST' && path === '/api/staff/login') {
    const body = call.body as { staff_id: string; pin: string }
    if (body.pin !== '1111') return fail(403, 'bad_pin')
    const person = PEOPLE.find((p) => p.id === body.staff_id) ?? LAKSHMI
    return json(200, { token: `tok_${person.id}`, staff: person, expires_at: '2026-11-08T06:00:00.000Z' })
  }
  if (method === 'POST' && path === '/api/staff/signup') {
    const body = call.body as { name: string; language: StaffPerson['language'] }
    const person = { id: 'STF_NEW', name: body.name, language: body.language }
    return json(201, { token: 'tok_new', staff: person, expires_at: '2026-11-08T06:00:00.000Z' })
  }
  if (method === 'GET' && path === '/api/staff/roles') return json(200, SLOTS)
  if (method === 'POST' && path === '/api/staff/roles/claim') {
    const body = call.body as { slot_id: string }
    const found = SLOTS.find((s) => s.slot_id === body.slot_id)
    if (!found || found.holder) return fail(409, 'slot_taken')
    return json(200, { ...found, holder: { staff_id: 'STF_LAKSHMI', name: 'Lakshmi' }, mine: true })
  }
  return fail(404, 'not_found')
}

function press(...digits: string[]) {
  for (const d of digits) fireEvent.click(screen.getByRole('button', { name: `Digit ${d}` }))
}

beforeEach(() => {
  window.localStorage.clear()
  window.history.replaceState(null, '', '/staff')
  resetSessionForTests()
  calls = stubFetch(respond)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('login_claim_and_land_on_the_station_screen', async () => {
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Lakshmi/ }))
  expect(screen.getByText('Enter your PIN')).toBeInTheDocument()
  press('1', '1', '1', '1')

  // the taken job is greyed with its holder; the open one is tappable
  const taken = await screen.findByRole('button', { name: /Curry Chef 1/ })
  expect(taken).toBeDisabled()
  expect(taken).toHaveTextContent('Taken by Carlos')
  fireEvent.click(screen.getByRole('button', { name: /Biryani Chef 1/ }))

  await waitFor(() => expect(window.location.pathname).toBe('/kds'))
  expect(window.location.search).toBe('?station=STN_BIRYANI')
  const saved = JSON.parse(window.localStorage.getItem('ros_staff') ?? 'null')
  expect(saved.claim).toMatchObject({ slot_id: 'STN_BIRYANI#1', label: 'Biryani Chef 1', station_id: 'STN_BIRYANI' })
  expect(calls.some((c) => c.method === 'POST' && c.path === '/api/staff/roles/claim')).toBe(true)
})

test('a_wrong_pin_is_refused_and_the_pad_clears', async () => {
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Carlos/ }))
  press('9', '9', '9', '9')
  expect(await screen.findByRole('alert')).toHaveTextContent('That PIN does not match')
  expect(window.location.pathname).toBe('/staff')
})

test('signup_sets_a_pin_then_picks_a_job', async () => {
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /I am new here/ }))
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Priya' } })
  fireEvent.click(screen.getByRole('button', { name: 'తెలుగు' }))
  fireEvent.click(screen.getByRole('button', { name: /Next: choose your PIN/ }))
  press('4', '3', '2', '1')
  expect(await screen.findByText('Enter it once more')).toBeInTheDocument()
  press('4', '3', '2', '1')

  expect(await screen.findByRole('heading', { name: /pick your job for this shift/ })).toBeInTheDocument()
  const signup = calls.find((c) => c.method === 'POST' && c.path === '/api/staff/signup')
  expect(signup?.body).toEqual({ name: 'Priya', language: 'te', pin: '4321' })
})
