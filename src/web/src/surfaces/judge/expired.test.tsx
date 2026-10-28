// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
// A kitchen that lapsed while the action page still held its SessionInfo (a tab left open overnight), and the only
// clips the hero and the story may play under their Telugu-English labels. REST goes through the real api client to
// a stubbed fetch; voice lines use the fake socket and audio, as in judge.test.tsx.
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { clearToasts } from '../../components/ui'
import type { EventType, SampleClip, SessionInfo } from '../../contracts/generated'
import { encodeWav } from '../../lib/audio/pcm'
import { fakeAudio, FakeVoiceSocket } from '../../lib/audio/testkit'
import { resetSessionForTests, resetTenantFeedForTests, useSession, useTenant } from '../../stores'
import { event } from '../../stores/testkit'
import { fixtureState } from '../../test/fixtures'
import { ActionPage } from '.'
import { CANONICAL_TEXT, heroClip, usePasscodeOffer } from './api'
import { resetLiveForTests } from './live'
import { cancelStory, useResetAsk } from './story'
import { basics, type Call, CLIP, fail, json, meter, SESSION, stubFetch, VS } from './testkit'

let calls: Call[] = []
let route: (call: Call) => Response | null = () => null
let seq = 100

beforeEach(() => {
  calls = stubFetch((call) => route(call) ?? basics(call) ?? fail(404, 'not_found'))
  route = () => null
  seq = 100
  FakeVoiceSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  resetLiveForTests()
  cancelStory()
  usePasscodeOffer.setState({ answer: null })
  useResetAsk.setState({ open: false })
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  cleanup()
  cancelStory()
  clearToasts()
  resetTenantFeedForTests()
  vi.unstubAllGlobals()
})

const RUSH = { running: true, phase: 'pre_call', speed: 10 }
/** Another sandbox: the one a Start click makes after the first kitchen lapsed. */
const NEW_KITCHEN: SessionInfo = { ...SESSION, tenant_id: 'sbx_01M5Z8NEWK1TCHEN00000000000' }
const clip = (language: SampleClip['language'], source: SampleClip['source'], id = `clip_${language}_${source}`) =>
  ({ ...CLIP, id, language, source, label: `${language} order`, consent_id: null }) as SampleClip

const page = () => render(<ActionPage lineDeps={{ connect: FakeVoiceSocket.connect, audio: fakeAudio().deps,
  fetchBytes: async () => encodeWav(new Int16Array(1600)) }} />)
const count = (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path).length
const signedIn = () => useSession.setState({ info: SESSION, status: 'ready', error: null }) // read before it lapsed
const apply = (type: EventType, payload: object) => act(() => useTenant.getState().apply(event(++seq, type, payload)))

/** The server's side: `dead` answers until POST /api/session sets a new cookie for `fresh`, then the new kitchen's. */
function lapsed(fresh: SessionInfo, dead: (c: Call) => Response | null): (c: Call) => Response | null {
  let created = false
  return (c) => {
    if (c.method === 'POST' && c.path === '/api/session') {
      created = true
      return json(200, fresh)
    }
    if (created && c.method === 'GET' && c.path === '/api/session') return json(200, fresh)
    if (c.path === '/api/ops/meter') return json(200, meter(0, 0))
    return created ? null : dead(c)
  }
}

test('expired_session_start_makes_a_new_kitchen', async () => {
  // the cookie lapsed overnight: the kitchen's routes and GET /api/session answer 401 until Start makes a new one
  const server = lapsed(SESSION, (c) => (c.path === '/api/judge/rush' ? fail(401, 'no_session') : null))
  route = (c) => server(c) ?? (c.path === '/api/judge/rush' ? json(200, RUSH) : null)
  signedIn()
  page()
  fireEvent.click(screen.getByRole('button', { name: 'Run the full story live' }))
  expect(await screen.findByText('Your kitchen has expired. Press any Start button for a new one.'))
    .toBeInTheDocument()
  expect(count('GET', '/api/session')).toBe(1) // the refusal re-read the session, which dropped the dead one
  expect(useSession.getState().info).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  await waitFor(() => expect(count('POST', '/api/judge/rush')).toBe(2))
  expect(count('POST', '/api/session')).toBe(1) // a new kitchen, not the dead cookie again
  expect(useSession.getState().info).toEqual(SESSION)
  expect(screen.queryByText(/Your kitchen has expired/)).toBeNull()
})

test('swept_sandbox_new_kitchen_restarts_the_story', async () => {
  // the cookie still lives but its sandbox was swept: its routes are 404, and only GET /api/session says why
  const server = lapsed(NEW_KITCHEN, (c) => c.method === 'GET' && c.path === '/api/session'
    ? fail(401, 'no_session', { reason: 'sandbox_gone' }) : c.path.startsWith('/api/judge/') ? fail(404, 'not_found')
      : null)
  route = (c) => server(c) ?? (c.path === '/api/judge/call/arm' ? json(200, { dtmf_code: '482915',
    number: '+15087091967', armed_until: new Date(Date.now() + 600_000).toISOString() }) : null)
  signedIn()
  page()
  fireEvent.click(screen.getByRole('button', { name: 'Run the full story live' }))
  expect(await screen.findByText('Your kitchen expired after 24 h without use. Press any Start button for a new one.'))
    .toBeInTheDocument()
  expect(useSession.getState().info).toBeNull()

  // another Start button: a new kitchen, and the story card starts over there instead of showing the old run
  fireEvent.click(screen.getByRole('button', { name: 'Call now' }))
  expect(await screen.findByTestId('dtmf-code')).toHaveTextContent('# 482 915')
  expect(count('POST', '/api/session')).toBe(1)
  expect(useSession.getState().info?.tenant_id).toBe(NEW_KITCHEN.tenant_id)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Run the full story live' })).toBeEnabled())
  expect(screen.queryByText(/expired after 24 h/)).toBeNull()
})

test('returning_judge_is_told_the_kitchen_expired', async () => {
  // a tab reopened after the sweep: the shell's GET /api/session says why, and the first Start clears the notice
  route = lapsed(NEW_KITCHEN, (c) => c.method === 'GET' && c.path === '/api/session'
    ? fail(401, 'no_session', { reason: 'sandbox_gone' }) : null)
  await act(async () => {
    await useSession.getState().load()
  })
  page()
  expect(screen.getByText(/Welcome back\. Your earlier kitchen expired after 24 h/)).toBeInTheDocument()
  await act(async () => {
    await useSession.getState().start()
  })
  expect(screen.queryByText(/Welcome back/)).toBeNull()
})

test('hero_plays_only_a_telugu_english_clip', async () => {
  // only Telugu-English (then Telugu) clips may play under the hero's Telugu-English label
  const teEnSynthetic = clip('te-en', 'synthetic_voice')
  const teConsented = clip('te', 'consented_recording')
  const others = [clip('en', 'consented_recording'), clip('es', 'consented_recording'), clip('ta', 'synthetic_voice')]
  expect(heroClip(others)).toBeNull()
  expect(heroClip([...others, teConsented, teEnSynthetic])).toBe(teEnSynthetic)
  expect(heroClip([teEnSynthetic, CLIP, ...others])).toBe(CLIP) // a consented te-en recording comes first
  expect(heroClip([...others, teConsented])).toBe(teConsented)

  route = (c) => c.method === 'GET' && c.path === '/api/session' ? json(200, SESSION)
    : c.path === '/api/judge/samples' ? json(200, others) : null
  signedIn()
  page()
  fireEvent.click(screen.getByRole('button', { name: /Hear a Telugu-English order go through live/ }))
  expect(await screen.findByText(/This server has no Telugu-English sample clip yet/)).toBeInTheDocument()
  expect(FakeVoiceSocket.all).toHaveLength(0) // nothing was played or streamed
  expect(screen.queryByTestId('hero-clip')).toBeNull()
})

test('story_rail_says_typed_without_a_telugu_clip', async () => {
  route = (c) => c.method === 'GET' && c.path === '/api/session' ? json(200, SESSION)
    : c.path === '/api/judge/rush' ? json(200, RUSH)
      : c.path === '/api/judge/samples' ? json(200, [clip('en', 'consented_recording')]) : null
  signedIn()
  page()
  act(() => useTenant.getState().load(fixtureState()))
  fireEvent.click(screen.getByRole('button', { name: 'Run the full story live' }))
  await waitFor(() => expect(count('POST', '/api/judge/rush')).toBe(1))
  apply('rush.state', { running: true, phase: 'pre_call', speed: 10, injected_orders: 0 })
  // known while the walk-ins run: the rail never promises Telugu-English speech the story will not send
  const order = () => document.querySelector('[data-beat="order"]')
  await waitFor(() => expect(order()).toHaveTextContent('Typed in English: this server has no Telugu-English clip yet'))
  expect(order()).toHaveAttribute('data-state', 'next')
  expect(order()).not.toHaveTextContent('Telugu-English, through live speech recognition')

  apply('rush.state', { running: true, phase: 'await_order', speed: 1, injected_orders: 0 })
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv({ type: 'ready', session_id: VS, speak_languages: ['en'], resumed: false })
  })
  await waitFor(() => expect(sock.messages().at(-1)).toMatchObject({ type: 'text', text: CANONICAL_TEXT }))
  expect(sock.messages().some((m) => m.type === 'play_sample')).toBe(false)
  expect(await screen.findByText('Typed in English (this server has no Telugu-English clip yet)')).toBeInTheDocument()
})
