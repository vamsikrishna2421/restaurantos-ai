// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MIC_CONSTRAINTS } from '../../lib/audio/capture'
import { FakeSource, fakeAudio, fakeSpeech, FakeVoiceSocket, pcmFrame, type FakeAudio } from '../../lib/audio/testkit'
import { resetSessionForTests, resetTenantFeedForTests, useSession, useTenant } from '../../stores'
import { event } from '../../stores/testkit'
import { fixtureState, storyState, taxCents } from '../../test/fixtures'
import type { KioskDeps } from './controller'
import { Kiosk } from './index'
import { cart, READY, reply, result, SESSION, settle, SID } from './testkit'

let clock = 0
let keys = 0

function kioskDeps(audio: FakeAudio, over: Partial<KioskDeps> = {}): Partial<KioskDeps> {
  return {
    audio: audio.deps, speech: null, storage: window.sessionStorage, ensureSession: async () => {},
    connect: FakeVoiceSocket.connect, random: () => 0, now: () => clock, newKey: () => `tk-${++keys}`, ...over,
  }
}

/** Render the kiosk, pass the audio gate and let the server say ready. */
async function openKiosk(over: Partial<KioskDeps> = {}, audio = fakeAudio()) {
  render(<Kiosk deps={kioskDeps(audio, over)} />)
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Tap to start' }))
    await settle()
  })
  const sock = FakeVoiceSocket.latest()
  act(() => {
    sock.open()
    sock.recv(READY)
  })
  return { audio, sock, node: audio.nodes[0], talk: screen.getByTestId('talk-button') }
}

const recv = (sock: FakeVoiceSocket, msg: object) => act(() => sock.recv(msg))
const sentTypes = (sock: FakeVoiceSocket) => sock.messages().map((m) => m.type)

beforeEach(() => {
  clock = 1_000_000
  keys = 0
  FakeVoiceSocket.all = []
  resetSessionForTests()
  resetTenantFeedForTests()
  window.sessionStorage.clear()
  useSession.setState({ info: SESSION, status: 'ready', error: null })
  useTenant.getState().load(fixtureState())
})

afterEach(() => {
  cleanup()
  resetTenantFeedForTests()
  vi.useRealTimers()
})

test('audio_gate_starts_worklet', async () => {
  const audio = fakeAudio()
  render(<Kiosk deps={kioskDeps(audio)} />)
  expect(screen.getByRole('dialog', { name: /order with your voice/i })).toBeInTheDocument()
  expect(audio.contexts).toHaveLength(0) // nothing starts on page load
  expect(FakeVoiceSocket.all).toHaveLength(0)
  // until talking is possible the Space bar is left alone (it still presses the focused gate button)
  expect(fireEvent.keyDown(window, { code: 'Space', key: ' ' })).toBe(true)
  fireEvent.keyUp(window, { code: 'Space', key: ' ' })

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Tap to start' }))
    // inside the tap itself: a 16 kHz context, resumed (iOS unlocks audio only in a gesture)
    expect(audio.contexts.map((c) => [c.sampleRate, c.resumes])).toEqual([[16_000, 1]])
    await settle()
  })
  expect(audio.constraints).toEqual([MIC_CONSTRAINTS])
  expect(audio.contexts[0].modules).toEqual(['/worklets/pcm-capture.js'])
  expect(audio.nodes.map((n) => n.name)).toEqual(['pcm-capture'])
  expect(audio.contexts[0].streamSources[0].outputs).toEqual([audio.nodes[0]])
  expect(screen.queryByRole('dialog')).toBeNull()

  const sock = FakeVoiceSocket.latest()
  expect(sock.url).toMatch(/\/ws\/voice\?purpose=order&lang=auto$/)
  act(() => {
    sock.open()
    sock.recv(READY)
  })
  expect(sock.messages()).toEqual([{ type: 'warm' }])
  // the worklet runs from the gate on, but nothing leaves the page outside a turn
  act(() => audio.nodes[0].emit())
  expect(sock.binary()).toHaveLength(0)
  expect(screen.getByRole('button', { name: 'Hold to talk' })).toBeEnabled()
})

test('the gate is modal: focus starts on Start and stays in the gate, and the kiosk behind it is inert', async () => {
  const ensureSession = vi.fn().mockRejectedValueOnce(Object.assign(new Error('429'), { code: 'rate_limited' }))
    .mockResolvedValue(undefined)
  render(<Kiosk deps={kioskDeps(fakeAudio(), { ensureSession })} />)
  const start = screen.getByRole('button', { name: 'Tap to start' })
  expect(start).toHaveFocus()
  expect(screen.getByTestId('kiosk-body')).toHaveAttribute('inert')
  screen.getByRole('button', { name: 'Auto' }).focus() // a language chip behind the gate (inert in a browser)
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Tab', shiftKey: true })
  expect(start).toHaveFocus()

  start.blur()
  await act(async () => {
    fireEvent.click(start) // refused: the gate stays, and the focus comes back for another try
    await settle()
  })
  expect(within(screen.getByRole('dialog')).getByRole('alert')).toHaveTextContent(/going a little fast/)
  expect(start).toHaveFocus()
  await act(async () => {
    fireEvent.click(start)
    await settle()
  })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByTestId('kiosk-body')).not.toHaveAttribute('inert')
})

test('ptt_start_frames_stop', async () => {
  const { sock, node, talk } = await openKiosk()
  act(() => node.emit(pcmFrame(5))) // heard just before the press: sent as pre-roll
  fireEvent.pointerDown(talk, { pointerId: 1, button: 0, pointerType: 'touch' })
  expect(sock.messages().at(-1)).toEqual({ type: 'start', turn_key: 'tk-1', mode: 'ptt' })
  expect(talk).toHaveAttribute('data-state', 'listening')
  act(() => {
    node.emit(pcmFrame(6))
    node.emit(pcmFrame(7))
  })
  const frames = sock.binary()
  expect(frames.map((f) => f.byteLength)).toEqual([2560, 2560, 2560])
  expect(frames.map((f) => new Int16Array(f.buffer, f.byteOffset, 1280)[0])).toEqual([5, 6, 7])

  clock += 1800
  fireEvent.pointerUp(talk, { pointerId: 1, button: 0, pointerType: 'touch' })
  expect(sentTypes(sock).at(-1)).toBe('start') // the tail: the last word often outlasts the finger
  act(() => {
    node.emit(pcmFrame(8))
    node.emit(pcmFrame(9))
  })
  expect(sock.messages().at(-1)).toEqual({ type: 'stop', turn_key: 'tk-1' })
  act(() => node.emit(pcmFrame(10)))
  expect(sock.binary()).toHaveLength(5) // nothing after stop
  expect(talk).toHaveAttribute('data-state', 'thinking')

  recv(sock, result({ transcript: 'one samosa', cart: cart([['SAMOSA', 1]]) }))
  expect(talk).toHaveAttribute('data-state', 'idle')

  // the Space bar works the same way (and never scrolls the page); a tap under 250 ms is cancelled, not a turn
  expect(fireEvent.keyDown(window, { code: 'Space', key: ' ' })).toBe(false)
  expect(sock.messages().at(-1)).toEqual({ type: 'start', turn_key: 'tk-2', mode: 'ptt' })
  clock += 100
  fireEvent.keyUp(window, { code: 'Space', key: ' ' })
  expect(sock.messages().at(-1)).toEqual({ type: 'cancel', turn_key: 'tk-2' })
  expect(screen.getByText(/hold the button while you talk/i)).toBeInTheDocument()
})

test('captions_replace', async () => {
  const { sock, node, talk } = await openKiosk()
  fireEvent.pointerDown(talk, { pointerId: 1 })
  recv(sock, { type: 'partial', turn_key: 'tk-1', text: 'two chicken' })
  expect(screen.getByTestId('caption')).toHaveTextContent('“two chicken”')
  expect(screen.getByTestId('caption')).toHaveAttribute('data-final', 'false')
  recv(sock, { type: 'partial', turn_key: 'tk-1', text: 'two chicken biryanis' })
  recv(sock, { type: 'partial', turn_key: 'tk-0', text: 'a stale turn' })
  expect(screen.getAllByTestId('caption')).toHaveLength(1)
  expect(screen.getByTestId('caption')).toHaveTextContent(/^“two chicken biryanis”$/)

  clock += 2000
  fireEvent.pointerUp(talk, { pointerId: 1 })
  act(() => {
    node.emit()
    node.emit()
  })
  recv(sock, { type: 'final', turn_key: 'tk-1', text: 'two chicken biryanis, one without onions', asr_ms: 640 })
  expect(screen.getByTestId('caption')).toHaveTextContent(/^“two chicken biryanis, one without onions”$/)
  expect(screen.getByTestId('caption')).toHaveAttribute('data-final', 'true')
  expect(screen.queryByText(/a stale turn/)).toBeNull()

  // the reply shows as text first (captions always), with its English gloss for a Telugu reply
  recv(sock, result({ transcript: 'two chicken biryanis, one without onions', path: 'llm',
    cart: cart([['CHK_BIRY', 1], ['CHK_BIRY', 1, ['NO_ONION']]]),
    reply: reply('2 చికెన్ దమ్ బిర్యానీ. ఇంకేమైనా?', { lang: 'te', text_en: '2 Chicken Dum Biryani. Anything else?' }) }))
  const shown = screen.getByTestId('reply')
  expect(shown).toHaveTextContent('2 చికెన్ దమ్ బిర్యానీ. ఇంకేమైనా?')
  expect(shown).toHaveTextContent('2 Chicken Dum Biryani. Anything else?')
  expect(screen.getByText('AI understood')).toBeInTheDocument()
})

test('sold_out_greyed', async () => {
  useTenant.getState().load(storyState()) // 6:50 PM: Paneer Tikka and Palak Paneer are 86'd
  const { sock } = await openKiosk()
  const tikka = screen.getByRole('group', { name: /^Paneer Tikka, \$13\.99, sold out$/ })
  expect(tikka).toHaveAttribute('aria-disabled', 'true')
  expect(within(tikka).getByText('Sold out')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Add Paneer Tikka/ })).toBeNull()
  fireEvent.click(tikka)
  expect(sentTypes(sock)).toEqual(['warm'])
  // its alternatives are one tap away
  expect(within(tikka).getByRole('button', { name: 'Gobi Manchurian' })).toBeEnabled()

  // a live 86 from the kitchen greys a tile out without a reload
  const gobi = screen.getByRole('button', { name: 'Add Gobi Manchurian, $11.99' })
  expect(gobi).toBeEnabled()
  act(() => useTenant.getState().apply(event(999, 'menu.availability_changed', {
    item_code: 'GOBI_MAN', available: false, reason: 'sold_out', source: 'kitchen', plan_id: null, menu_version: 9,
  })))
  expect(screen.queryByRole('button', { name: /^Add Gobi Manchurian/ })).toBeNull()
  expect(screen.getByRole('group', { name: /^Gobi Manchurian, \$11\.99, sold out$/ })).toBeInTheDocument()
})

test('button_adds_line_without_llm', async () => {
  const { sock } = await openKiosk()
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  expect(sock.messages().at(-1)).toEqual({
    type: 'cart_ops', turn_key: 'tk-1',
    ops: [{ op: 'add', line_ref: null, item_code: 'SAMOSA', qty: 1, modifiers: [] }],
  })
  // taps while that turn is in flight are merged into the next cart_ops
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  fireEvent.click(screen.getByRole('button', { name: 'Add Masala Chai, $2.99' }))
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  expect(sentTypes(sock)).toEqual(['warm', 'cart_ops'])

  clock += 120
  recv(sock, result({ path: 'buttons', transcript: '', cart: cart([['SAMOSA', 1]]), reply: reply('1 Samosa. Anything else?') }))
  expect(sock.messages().at(-1)).toEqual({
    type: 'cart_ops', turn_key: 'tk-2',
    ops: [{ op: 'add', line_ref: null, item_code: 'SAMOSA', qty: 2, modifiers: [] },
      { op: 'add', line_ref: null, item_code: 'MASALA_CHAI', qty: 1, modifiers: [] }],
  })
  expect(sentTypes(sock)).not.toContain('text')
  expect(sentTypes(sock)).not.toContain('start')
  const order = screen.getByRole('list', { name: 'Order lines' })
  expect(within(order).getByText('1× Samosa')).toBeInTheDocument()
  expect(screen.getByText('Buttons · no AI')).toBeInTheDocument()
  expect(screen.getByTestId('cart-total')).toHaveTextContent('$7.48') // the server's total: 699 + 7% tax

  recv(sock, result({ path: 'buttons', cart: cart([['SAMOSA', 3], ['MASALA_CHAI', 1]]) }))
  expect(within(order).getByText('3× Samosa')).toBeInTheDocument()
  expect(screen.getByTestId('cart-total')).toHaveTextContent(`$${((2097 + 299 + taxCents(2396)) / 100).toFixed(2)}`)
  // −/+ on a line edits it by line number, still without the model
  fireEvent.click(screen.getByRole('button', { name: 'One less Samosa' }))
  expect(sock.messages().at(-1)).toMatchObject({ type: 'cart_ops', ops: [{ op: 'set_qty', line_ref: 1, qty: 2 }] })
})

test('confirm_button_sends_confirm', async () => {
  const { sock } = await openKiosk()
  expect(screen.queryByRole('button', { name: /review order|place order/i })).toBeNull() // nothing to place yet
  const order3 = cart([['CHK_BIRY', 2], ['PNR_BM', 1]])
  recv(sock, result({ transcript: 'two chicken biryanis and a paneer curry', cart: order3 }))
  // outside the read-back the button asks for it (like DTMF 1); it never places by itself
  fireEvent.click(screen.getByRole('button', { name: 'Review order' }))
  expect(sock.messages().at(-1)).toEqual({ type: 'confirm', turn_key: 'tk-1', yes: true })
  recv(sock, result({ state: 'readback', cart: order3,
    reply: reply('2 Chicken Dum Biryani, 1 Paneer Butter Masala. $52.93, about 25 minutes. Say yes or press 1.') }))
  expect(screen.getByRole('button', { name: 'One more Paneer Butter Masala' })).toBeDisabled() // "Change" first
  fireEvent.click(screen.getByRole('button', { name: 'Change' }))
  expect(sock.messages().at(-1)).toEqual({ type: 'confirm', turn_key: 'tk-2', yes: false })
  recv(sock, result({ state: 'take_order', cart: order3 }))
  fireEvent.click(screen.getByRole('button', { name: 'Review order' }))
  recv(sock, result({ state: 'readback', cart: order3 }))

  fireEvent.click(screen.getByRole('button', { name: 'Yes, place order' }))
  expect(sock.messages().at(-1)).toEqual({ type: 'confirm', turn_key: 'tk-4', yes: true })
  expect(screen.getByRole('button', { name: 'Yes, place order' })).toBeDisabled() // no double placement

  // the order lands on the feed; the result carries the placed cart (the server already rotated to a new one)
  const order = { ...fixtureState().orders_open[0], id: 'ord_kiosk', order_no: 1048, pickup_quote_min: 25,
    total_cents: 5293 }
  act(() => useTenant.getState().apply(event(500, 'order.placed', { order, ticket_ids: [], quote: null })))
  recv(sock, result({ placed: true, order_id: 'ord_kiosk', state: 'placed', cart: order3,
    reply: reply('Order 1048 placed — ready in about 25 minutes.', { template_key: 'placed_kiosk' }) }))
  expect(screen.getByRole('heading', { name: 'Order placed' })).toBeInTheDocument()
  expect(screen.getByText('#1048')).toBeInTheDocument()
  expect(screen.getByText(/Ready in about 25 minutes/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /place order/i })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Start a new order' }))
  expect(screen.getByRole('heading', { name: /your order/i })).toBeInTheDocument()
  expect(screen.queryByText('2× Chicken Dum Biryani')).toBeNull()
})

test('a menu tap during the read-back asks to change first, then adds', async () => {
  const { sock } = await openKiosk()
  recv(sock, result({ state: 'readback', cart: cart([['CHK_BIRY', 1]]) }))
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  expect(sock.messages().at(-1)).toEqual({ type: 'confirm', turn_key: 'tk-1', yes: false })
  expect(screen.getByRole('button', { name: 'Yes, place order' })).not.toHaveAttribute('aria-busy')
  recv(sock, result({ state: 'take_order', cart: cart([['CHK_BIRY', 1]]),
    reply: reply('No problem — what would you like to change?') }))
  expect(sock.messages().at(-1)).toEqual({ type: 'cart_ops', turn_key: 'tk-2',
    ops: [{ op: 'add', line_ref: null, item_code: 'SAMOSA', qty: 1, modifiers: [] }] })
})

test('typed_fallback', async () => {
  const { sock } = await openKiosk()
  fireEvent.click(screen.getByRole('button', { name: 'Type instead' }))
  const box = screen.getByRole('textbox', { name: 'Type your order' })
  fireEvent.change(box, { target: { value: '  two samosas and a masala chai ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
  expect(sock.messages().at(-1)).toEqual({ type: 'text', turn_key: 'tk-1', text: 'two samosas and a masala chai' })
  expect(box).toHaveValue('')
  expect(screen.getByTestId('caption')).toHaveTextContent('two samosas and a masala chai')
  recv(sock, result({ path: 'typed', transcript: 'two samosas and a masala chai',
    cart: cart([['SAMOSA', 2], ['MASALA_CHAI', 1]]) }))
  expect(screen.getByText('2× Samosa')).toBeInTheDocument()
  expect(screen.getByText('Typed')).toBeInTheDocument()
  cleanup()

  // no microphone: typing (and the menu) is offered straight away
  FakeVoiceSocket.all = []
  await openKiosk({}, fakeAudio({ deny: 'NotAllowedError' }))
  expect(screen.getByRole('textbox', { name: 'Type your order' })).toBeInTheDocument()
  expect(screen.getByText(/microphone access was blocked/i)).toBeInTheDocument()
  expect(screen.getByTestId('talk-button')).toBeDisabled()
})

test('resume_reconnect_keeps_cart', async () => {
  vi.useFakeTimers()
  const { sock } = await openKiosk()
  recv(sock, result({ cart: cart([['CHK_BIRY', 2], ['PNR_BM', 1]]) }))
  expect(screen.getByText('2× Chicken Dum Biryani')).toBeInTheDocument()

  act(() => sock.drop(1006)) // a Wi-Fi blip
  expect(screen.getByText('Reconnecting…')).toBeInTheDocument()
  act(() => {
    vi.advanceTimersByTime(500)
  })
  const again = FakeVoiceSocket.latest()
  expect(again).not.toBe(sock)
  expect(again.url).toContain(`resume=${SID}`)
  expect(screen.getByText('2× Chicken Dum Biryani')).toBeInTheDocument()
  act(() => {
    again.open()
    again.recv({ ...READY, resumed: true })
  })
  expect(screen.getByText('Connected')).toBeInTheDocument()
  expect(screen.getByText('2× Chicken Dum Biryani')).toBeInTheDocument()
  expect(screen.getByTestId('cart-total')).toHaveTextContent('$52.93')

  // a reload within 15 minutes resumes the same session and shows the cart before the server answers
  cleanup()
  FakeVoiceSocket.all = []
  render(<Kiosk deps={kioskDeps(fakeAudio())} />)
  expect(screen.getByText('2× Chicken Dum Biryani')).toBeInTheDocument()
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Tap to start' }))
    await settle()
  })
  const reloaded = FakeVoiceSocket.latest()
  expect(reloaded.url).toContain(`resume=${SID}`)
  // the server's LRU had expired: the stale cart is cleared rather than shown
  act(() => {
    reloaded.open()
    reloaded.recv({ ...READY, session_id: 'vs_2', resumed: false })
  })
  expect(screen.queryByText('2× Chicken Dum Biryani')).toBeNull()
  expect(screen.getByText(/fresh start/i)).toBeInTheDocument()
})

test('reply audio plays gaplessly, and degraded TTS falls back to the browser voice in the reply language', async () => {
  const speech = fakeSpeech([{ lang: 'en-US', name: 'Samantha' }, { lang: 'es-MX', name: 'Paulina' }])
  const { sock, audio, talk } = await openKiosk({ speech })
  recv(sock, result({ cart: cart([['SAMOSA', 1]]), reply: reply('1 Samosa. Anything else?') }))
  recv(sock, { type: 'tts_start', utterance_id: 'utt_1', format: 'pcm_16000', text: '1 Samosa. Anything else?',
    text_en: '1 Samosa. Anything else?' })
  act(() => {
    sock.recvAudio(new Uint8Array(3200).buffer)
    sock.recvAudio(new Uint8Array(3200).buffer)
  })
  const sources = audio.contexts[0].sources as FakeSource[]
  expect(sources).toHaveLength(2)
  expect(sources[1].startedAt! - sources[0].startedAt!).toBeCloseTo(0.1)
  expect(talk).toHaveAttribute('data-state', 'speaking')
  recv(sock, { type: 'tts_end', utterance_id: 'utt_1' })
  act(() => sources.forEach((s) => s.finish()))
  expect(talk).toHaveAttribute('data-state', 'idle')
  expect(speech.spoken).toHaveLength(0)

  // TTS degraded (no ElevenLabs key yet): the Spanish reply is read by a Spanish browser voice
  recv(sock, result({ cart: cart([['SAMOSA', 2]]), reply: reply('2 samosas. ¿Algo más?', { lang: 'es' }) }))
  await act(async () => {
    sock.recv({ type: 'degraded', component: 'tts', fallback: 'captions' })
    await settle()
  })
  expect(speech.spoken.map((u) => [u.text, u.lang])).toEqual([['2 samosas. ¿Algo más?', 'es-MX']])
  expect(screen.getByText('Browser voice')).toBeInTheDocument()

  // a Telugu reply with no Telugu voice in this browser: captions only, never an English voice
  vi.useFakeTimers()
  recv(sock, result({ cart: cart([['SAMOSA', 3]]), reply: reply('3 సమోసా. ఇంకేమైనా?', { lang: 'te' }) }))
  await act(async () => {
    vi.advanceTimersByTime(700) // no server audio within the grace period
    await settle()
  })
  expect(speech.spoken).toHaveLength(1)
  expect(screen.getByText('Captions only')).toBeInTheDocument()
  expect(screen.getByTestId('reply')).toHaveTextContent('3 సమోసా. ఇంకేమైనా?')
})

test('a press during the reply barges in: the audio stops before the new turn starts', async () => {
  const { sock, audio, talk } = await openKiosk()
  recv(sock, result({ reply: reply('Anything else?') }))
  recv(sock, { type: 'tts_start', utterance_id: 'utt_9', format: 'pcm_16000', text: 'Anything else?', text_en: '' })
  act(() => sock.recvAudio(new Uint8Array(3200).buffer))
  fireEvent.pointerDown(talk, { pointerId: 1 })
  expect(sentTypes(sock).slice(-2)).toEqual(['barge_in', 'start'])
  expect((audio.contexts[0].sources[0] as FakeSource).stopped).toBe(true)
  expect(talk).toHaveAttribute('data-state', 'listening')
})

test('a forgotten hold stops itself, and a turn with no answer is given up after 20 s', async () => {
  vi.useFakeTimers()
  const { sock, node, talk } = await openKiosk({ now: () => Date.now() }) // the hold clock follows fake time
  fireEvent.pointerDown(talk, { pointerId: 1 })
  act(() => {
    vi.advanceTimersByTime(29_000) // the auto-stop, before the server's 30 s limit
    node.emit()
    node.emit()
  })
  expect(sock.messages().at(-1)).toEqual({ type: 'stop', turn_key: 'tk-1' })
  expect(talk).toHaveAttribute('data-state', 'thinking')
  fireEvent.pointerUp(talk, { pointerId: 1 }) // the late release changes nothing
  expect(sentTypes(sock).filter((t) => t === 'stop')).toHaveLength(1)

  act(() => {
    vi.advanceTimersByTime(20_000)
  })
  // the stop went out, so the turn may still land: no blind "try again" (it could add everything twice)
  expect(screen.getByText('That took too long. Checking your order with the kitchen…')).toBeInTheDocument()
  expect(talk).toHaveAttribute('data-state', 'idle')
  expect(sock.closedWith).toBe(1000) // a possibly half-open socket is replaced, resuming the session
  const again = FakeVoiceSocket.latest()
  expect(again.url).toContain(`resume=${SID}`)
  act(() => {
    again.open()
    again.recv({ ...READY, resumed: true })
    vi.advanceTimersByTime(12_000) // the feed never reported the turn
  })
  expect(screen.getByText(/could not confirm your last request.*please say it again/i)).toBeInTheDocument()
})

test('a server error ends the turn and says why; a session gone behind 1006 closes the gate until the next tap',
  async () => {
  vi.useFakeTimers()
  const ensureSession = vi.fn(async () => {})
  const probeSession = vi.fn(async () => false) // GET /api/session: 401
  const { sock } = await openKiosk({ ensureSession, probeSession })
  expect(ensureSession).toHaveBeenCalledWith(false)
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' }))
  recv(sock, { type: 'error', code: 'rate_limited', message: 'slow down' })
  expect(screen.getByRole('alert')).toHaveTextContent(/going a little fast/)
  fireEvent.click(screen.getByRole('button', { name: 'Add Samosa, $6.99' })) // not stuck: the next tap goes out
  expect(sock.messages().filter((m) => m.type === 'cart_ops')).toHaveLength(2)

  // the sandbox was swept: each reconnect is refused before accept (401), which a browser reports as 1006
  act(() => sock.drop(1006))
  for (const backoff of [500, 1000, 2000]) {
    act(() => {
      vi.advanceTimersByTime(backoff)
      FakeVoiceSocket.latest().drop(1006)
    })
  }
  await act(async () => {
    await settle()
  })
  expect(probeSession).toHaveBeenCalledOnce()
  const gate = screen.getByRole('dialog', { name: /order with your voice/i })
  expect(within(gate).getByRole('alert')).toHaveTextContent(/session ended/i)
  const opened = FakeVoiceSocket.all.length
  act(() => {
    vi.advanceTimersByTime(60_000)
  })
  expect(FakeVoiceSocket.all).toHaveLength(opened) // no endless "Reconnecting…"
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Tap to start' }))
    await settle()
  })
  expect(ensureSession).toHaveBeenLastCalledWith(true)
  expect(FakeVoiceSocket.latest().url).not.toContain('resume=') // a fresh voice session for the new cookie
})
