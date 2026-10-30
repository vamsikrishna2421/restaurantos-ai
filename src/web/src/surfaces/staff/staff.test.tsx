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

const LAKSHMI: StaffPerson = { id: 'STF_LAKSHMI', name: 'Lakshmi', language: 'te', default_role: null }
const CARLOS: StaffPerson = { id: 'STF_CARLOS', name: 'Carlos', language: 'es', default_role: null }
// pass 5: Sana's usual job is open this shift; Tara's is the slot Carlos already holds in SLOTS
const SANA: StaffPerson = {
  id: 'STF_SANA', name: 'Sana', language: 'en',
  default_role: { slot_id: 'STN_BIRYANI#1', label: 'Biryani Chef 1', pending: false },
}
const TARA: StaffPerson = {
  id: 'STF_TARA', name: 'Tara', language: 'en',
  default_role: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', pending: false },
}
const PEOPLE = [LAKSHMI, CARLOS, SANA, TARA]

const slot = (over: Partial<RoleSlot>): RoleSlot => ({
  slot_id: 'manager', label: 'Manager', kind: 'manager', station_id: null, station_ids: null,
  holder: null, mine: false, pending_approval: false, ...over,
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
    const body = call.body as {
      name: string
      language: StaffPerson['language']
      default_slot_id?: string
      custom_role?: { name: string; station_ids: string[] }
    }
    // echo the chosen usual job the way the server resolves it: custom jobs and the Manager job wait
    let defaultRole = null
    if (body.custom_role) {
      defaultRole = { slot_id: 'custom:CUS_T1', label: body.custom_role.name, pending: true }
    } else if (body.default_slot_id) {
      const found = SLOTS.find((s) => s.slot_id === body.default_slot_id)
      defaultRole = {
        slot_id: body.default_slot_id, label: found?.label ?? body.default_slot_id,
        pending: body.default_slot_id === 'manager',
      }
    }
    const person = { id: 'STF_NEW', name: body.name, language: body.language, default_role: defaultRole }
    return json(201, { token: 'tok_new', staff: person, expires_at: '2026-11-08T06:00:00.000Z' })
  }
  if (method === 'GET' && path === '/api/staff/roles') return json(200, SLOTS)
  if (method === 'POST' && path === '/api/staff/roles/claim') {
    const body = call.body as { slot_id: string }
    const found = SLOTS.find((s) => s.slot_id === body.slot_id)
    if (!found || found.holder) return fail(409, 'slot_taken')
    return json(200, { ...found, holder: { staff_id: 'STF_LAKSHMI', name: 'Lakshmi' }, mine: true })
  }
  if (method === 'POST' && path === '/api/staff/roles/release') return json(200, { ok: true })
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

  // pass 5: the PIN is followed by "what's your job?" — the pick rides on the one signup call, and an
  // approved station job is claimed at once, landing on its screen with no second picker
  expect(await screen.findByRole('heading', { name: /what's your job/ })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Biryani Chef 1/ }))
  const signup = calls.find((c) => c.method === 'POST' && c.path === '/api/staff/signup')
  expect(signup?.body).toEqual({
    name: 'Priya', language: 'te', pin: '4321', default_slot_id: 'STN_BIRYANI#1',
  })
  await waitFor(() => expect(window.location.pathname).toBe('/kds'))
  expect(window.location.search).toBe('?station=STN_BIRYANI')
})

test('login_with_a_free_usual_job_skips_the_picker', async () => {
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Sana/ }))
  press('1', '1', '1', '1')
  // no job picker at all: her usual job was free, so she lands straight on its station screen
  await waitFor(() => expect(window.location.pathname).toBe('/kds'))
  expect(window.location.search).toBe('?station=STN_BIRYANI')
  expect(screen.queryByText(/pick your job for this shift/)).not.toBeInTheDocument()
  const claim = calls.find((c) => c.method === 'POST' && c.path === '/api/staff/roles/claim')
  expect((claim?.body as { slot_id: string }).slot_id).toBe('STN_BIRYANI#1')
})

test('login_with_the_usual_job_taken_lands_on_the_picker_with_a_note', async () => {
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /Tara/ }))
  press('1', '1', '1', '1')
  expect(await screen.findByRole('heading', { name: /pick your job for this shift/ })).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('Your usual job — Curry Chef 1 — is taken this shift')
})

test('signup_with_a_job_of_your_own_waits_for_the_manager', async () => {
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  fireEvent.click(await screen.findByRole('button', { name: /I am new here/ }))
  fireEvent.change(screen.getByLabelText('Your name'), { target: { value: 'Ravi' } })
  fireEvent.click(screen.getByRole('button', { name: /Next: choose your PIN/ }))
  press('4', '3', '2', '1')
  expect(await screen.findByText('Enter it once more')).toBeInTheDocument()
  press('4', '3', '2', '1')

  fireEvent.click(await screen.findByRole('button', { name: /Something else/ }))
  fireEvent.change(screen.getByLabelText('Job name'), { target: { value: 'Runner' } })
  fireEvent.click(screen.getByRole('button', { name: 'Curry' }))
  fireEvent.click(screen.getByRole('button', { name: /Join and ask the manager/ }))

  const signup = calls.find((c) => c.method === 'POST' && c.path === '/api/staff/signup')
  expect(signup?.body).toEqual({
    name: 'Ravi', language: 'en', pin: '4321',
    custom_role: { name: 'Runner', station_ids: ['STN_CURRY'] },
  })
  // signed in, usual job named, visibly waiting — and no claim was attempted for it
  expect(await screen.findByText('You are signed in')).toBeInTheDocument()
  expect(screen.getByText(/Your usual job/)).toHaveTextContent('Runner')
  expect(screen.getByText(/Waiting for the manager's OK/)).toBeInTheDocument()
  expect(calls.some((c) => c.path === '/api/staff/roles/claim')).toBe(false)
})

test('switch_job_for_today_releases_without_ending_the_shift', async () => {
  window.localStorage.setItem('ros_staff', JSON.stringify({
    token: 'tok_carlos',
    staff: CARLOS,
    claim: { slot_id: 'STN_CURRY#1', label: 'Curry Chef 1', kind: 'chef', station_id: 'STN_CURRY' },
  }))
  render(<StaffSurface params={{}} query={new URLSearchParams()} />)
  expect(await screen.findByText('You are on shift')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Switch job for today' }))
  expect(await screen.findByRole('heading', { name: /pick your job for this shift/ })).toBeInTheDocument()
  expect(calls.some((c) => c.method === 'POST' && c.path === '/api/staff/roles/release')).toBe(true)
  // still signed in as Carlos afterwards: taking the open job lands on its screen under his session
  fireEvent.click(screen.getByRole('button', { name: /Biryani Chef 1/ }))
  await waitFor(() => expect(window.location.pathname).toBe('/kds'))
  const saved = JSON.parse(window.localStorage.getItem('ros_staff') ?? 'null')
  expect(saved.staff.id).toBe('STF_CARLOS')
})
