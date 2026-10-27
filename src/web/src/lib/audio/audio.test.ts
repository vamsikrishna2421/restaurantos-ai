// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { MicCapture, MIC_CONSTRAINTS } from './capture'
import { encodeWav, floatToPcm16, Pcm16Reader, pcm16FromBytes, pcm16ToFloat, rmsLevel } from './pcm'
import { PcmPlayer } from './player'
import { PushToTalk } from './ptt'
import { pickVoice, speakText } from './speech'
import { fakeAudio, FakeSource, fakeSpeech, FakeVoiceSocket, pcmFrame } from './testkit'
import { isVoiceSessionId, newTurnKey, parseVoiceMessage, PROBE_AFTER, VoiceSocket, type VoiceStatus } from './voiceSocket'

beforeEach(() => {
  FakeVoiceSocket.all = []
})

afterEach(() => {
  vi.useRealTimers()
})

const settle = async (rounds = 20) => {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
}

test('pcm helpers: float ↔ PCM16, odd-byte streams, level and a 16 kHz mono WAV header', () => {
  expect(Array.from(floatToPcm16([0, 1, -1, 2, -2, 0.5]))).toEqual([0, 32767, -32768, 32767, -32768, 16384])
  expect(pcm16ToFloat(new Int16Array([-32768, 0, 16384]))[0]).toBe(-1)
  const bytes = new Uint8Array([0x01, 0x00, 0xff, 0x7f, 0x00, 0x80])
  expect(Array.from(pcm16FromBytes(bytes))).toEqual([1, 32767, -32768])
  const reader = new Pcm16Reader()
  expect(Array.from(reader.push(bytes.slice(0, 3)))).toEqual([1])
  expect(Array.from(reader.push(bytes.slice(3)))).toEqual([32767, -32768])
  expect(rmsLevel(new Int16Array(10))).toBe(0)
  expect(rmsLevel(new Int16Array(10).fill(-32768))).toBe(1)

  const wav = new DataView(encodeWav(new Int16Array([1, -2, 3])))
  const ascii = (at: number) => String.fromCharCode(...new Uint8Array(wav.buffer, at, 4))
  expect([ascii(0), ascii(8), ascii(12), ascii(36)]).toEqual(['RIFF', 'WAVE', 'fmt ', 'data'])
  expect(wav.byteLength).toBe(50)
  expect(wav.getUint32(4, true)).toBe(42)
  expect(wav.getUint16(22, true)).toBe(1) // mono
  expect(wav.getUint32(24, true)).toBe(16_000)
  expect(wav.getUint16(34, true)).toBe(16) // bits
  expect(wav.getUint32(40, true)).toBe(6)
  expect(wav.getInt16(46, true)).toBe(-2)
})

test('the capture asks for a cleaned-up mic at 16 kHz, and falls back to the device rate when refused', async () => {
  const audio = fakeAudio()
  const cap = new MicCapture(audio.deps)
  const frames: Int16Array[] = []
  cap.onFrame = (f) => frames.push(f)
  const started = cap.start()
  expect(audio.contexts).toHaveLength(1) // created synchronously, inside the gesture
  expect(audio.contexts[0]).toMatchObject({ sampleRate: 16_000, resumes: 1 })
  await started
  expect(cap.state).toBe('running')
  expect(audio.constraints).toEqual([MIC_CONSTRAINTS])
  expect(MIC_CONSTRAINTS.audio).toMatchObject({ echoCancellation: true, noiseSuppression: true, autoGainControl: true })
  expect(audio.nodes[0].options.processorOptions.sampleRate).toBe(16_000)
  audio.nodes[0].emit(pcmFrame(7))
  audio.nodes[0].emit(new ArrayBuffer(10)) // not a frame: dropped
  expect(frames.map((f) => [f.length, f[0]])).toEqual([[1280, 7]])
  cap.stop()
  expect(audio.tracks[0].stopped).toBe(true)
  expect(cap.state).toBe('stopped')

  const firefox = fakeAudio({ refuse16k: true, deviceRate: 44_100 })
  const cap2 = new MicCapture(firefox.deps)
  await cap2.start()
  expect(cap2.state).toBe('running')
  expect(firefox.contexts.map((c) => [c.sampleRate, c.state])).toEqual([[16_000, 'closed'], [44_100, 'running']])
  expect(firefox.nodes.at(-1)?.options.processorOptions.sampleRate).toBe(44_100)
  expect(cap2.ctx).toBe(firefox.contexts[1])

  const denied = new MicCapture(fakeAudio({ deny: 'NotAllowedError' }).deps)
  await denied.start()
  expect(denied.state).toBe('denied')
  expect(denied.ctx?.state).toBe('running') // playback still unlocked
})

test('a stop during the permission prompt releases the microphone it then gets', async () => {
  const audio = fakeAudio()
  const cap = new MicCapture(audio.deps)
  const started = cap.start()
  cap.stop()
  await started
  expect(cap.state).toBe('stopped')
  expect(audio.tracks[0].stopped).toBe(true)
  expect(audio.nodes).toHaveLength(0)
  await cap.start() // and a later start works
  expect(cap.state).toBe('running')
})

test('the player schedules TTS chunks back to back and goes idle after tts_end', () => {
  const audio = fakeAudio()
  const ctx = audio.deps.createContext(16_000) as ReturnType<typeof fakeAudio>['contexts'][number]
  void ctx.resume()
  ctx.currentTime = 10
  const player = new PcmPlayer(() => ctx, { leadMs: 100 })
  const idle = vi.fn()
  player.onIdle = idle
  player.begin('utt_1')
  const chunk = new Uint8Array(3200).buffer // 100 ms
  player.push(chunk)
  player.push(chunk)
  const [a, b] = ctx.sources as FakeSource[]
  expect(a.startedAt).toBeCloseTo(10.1)
  expect(b.startedAt).toBeCloseTo(10.2)
  expect(player.playing).toBe(true)
  player.end('utt_1')
  expect(idle).not.toHaveBeenCalled()
  a.finish()
  b.finish()
  expect(idle).toHaveBeenCalledOnce()
  expect(player.playing).toBe(false)

  player.begin('utt_2') // barge-in stops what is scheduled
  player.push(chunk)
  player.stop()
  expect((ctx.sources[2] as FakeSource).stopped).toBe(true)
  player.push(chunk) // after stop: dropped
  expect(ctx.sources).toHaveLength(3)
})

test('the voice socket resumes its session, backs off, and stops on 4401/4403', () => {
  vi.useFakeTimers()
  const statuses: VoiceStatus[] = []
  const messages: string[] = []
  const audio: ArrayBuffer[] = []
  const vs = new VoiceSocket({
    lang: 'te', connect: FakeVoiceSocket.connect, random: () => 0, onStatus: (s) => statuses.push(s),
    onMessage: (m) => messages.push(m.type), onAudio: (a) => audio.push(a),
  })
  vs.start()
  const first = FakeVoiceSocket.latest()
  expect(first.url).toMatch(/\/ws\/voice\?purpose=order&lang=te$/)
  first.open()
  first.recv({ type: 'ready', session_id: 'vs_A', speak_languages: ['en'], resumed: false })
  first.recv({ type: 'nonsense' })
  first.recvAudio(new ArrayBuffer(4))
  expect(messages).toEqual(['ready'])
  expect(audio).toHaveLength(1)
  expect(vs.send({ type: 'warm' })).toBe(true)

  first.drop(1006)
  expect(vs.send({ type: 'warm' })).toBe(false)
  vi.advanceTimersByTime(499)
  expect(FakeVoiceSocket.all).toHaveLength(1)
  vi.advanceTimersByTime(1)
  expect(FakeVoiceSocket.latest().url).toContain('resume=vs_A')
  FakeVoiceSocket.latest().drop(1013) // second failure before ready: 1 s
  vi.advanceTimersByTime(999)
  expect(FakeVoiceSocket.all).toHaveLength(2)
  vi.advanceTimersByTime(1)
  expect(FakeVoiceSocket.all).toHaveLength(3)

  vs.setLang('es') // reconnects at once, still resuming
  expect(FakeVoiceSocket.latest().url).toMatch(/lang=es&resume=vs_A/)
  FakeVoiceSocket.latest().drop(4403)
  vi.advanceTimersByTime(10_000)
  expect(FakeVoiceSocket.all).toHaveLength(4)
  expect(statuses.at(-1)).toBe('unauthorized')
  expect(statuses).toContain('ready')
  expect(parseVoiceMessage('{"type":"final","turn_key":"k","text":"hi","asr_ms":10}')).toMatchObject({ type: 'final' })
  expect(parseVoiceMessage('not json')).toBeNull()
  expect(newTurnKey()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
})

test('closes before ready ask the session: a gone one stops the socket, a live one keeps reconnecting', async () => {
  vi.useFakeTimers()
  const sid = 'vs_01JB8Z9KTQ4M2N7P3R5S6V8W9X'
  expect([sid, 'vs_1', 'vs_01JB8Z9KTQ4M2N7P3R5S6V8W9I', null].map(isVoiceSessionId)).toEqual([true, false, false, false])
  const statuses: VoiceStatus[] = []
  const probeSession = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false)
  const vs = new VoiceSocket({ connect: FakeVoiceSocket.connect, random: () => 0, probeSession,
    resume: 'vs_not-a-session', onStatus: (s) => statuses.push(s) })
  vs.start()
  expect(FakeVoiceSocket.latest().url).not.toContain('resume=') // a malformed id would be refused (400) forever

  // a refused handshake (401, 403, 400, 429) reaches the browser as 1006, like a dead network
  FakeVoiceSocket.latest().drop(1006)
  for (const backoff of [500, 1000]) {
    vi.advanceTimersByTime(backoff)
    FakeVoiceSocket.latest().drop(1006)
  }
  await settle()
  expect(PROBE_AFTER).toBe(3)
  expect(probeSession).toHaveBeenCalledTimes(1) // after the third: the session is fine, so a network problem
  expect(statuses.at(-1)).toBe('reconnecting')

  vi.advanceTimersByTime(2000)
  FakeVoiceSocket.latest().open()
  FakeVoiceSocket.latest().recv({ type: 'ready', session_id: sid, speak_languages: ['en'], resumed: false })
  FakeVoiceSocket.latest().drop(1006) // after a ready: a drop, not a refusal (the streak starts again)
  for (const backoff of [500, 1000, 2000]) {
    vi.advanceTimersByTime(backoff)
    FakeVoiceSocket.latest().drop(1006)
  }
  await settle()
  expect(probeSession).toHaveBeenCalledTimes(2)
  expect(statuses.at(-1)).toBe('unauthorized')
  const opened = FakeVoiceSocket.all.length
  vi.advanceTimersByTime(60_000)
  expect(FakeVoiceSocket.all).toHaveLength(opened)
  expect(FakeVoiceSocket.latest().url).toContain(`resume=${sid}`)
})

test('push-to-talk: pre-roll, live frames, a tail after release, and cancel for a tap', () => {
  vi.useFakeTimers() // also fakes Date.now, the hold clock
  const sock = new FakeVoiceSocket('ws://x')
  sock.open()
  const vs = { send: (m: object) => (sock.send(JSON.stringify(m)), true), sendAudio: (f: ArrayBufferView) => (sock.send(f), true) }
  let n = 0
  const ptt = new PushToTalk({ socket: vs, newKey: () => `k${++n}` })
  const ends: string[] = []
  ptt.onEnd = (key, how, heldMs) => ends.push(`${key}:${how}:${heldMs}`)
  const frame = () => new Int16Array(pcmFrame())
  ptt.frame(frame())
  ptt.frame(frame()) // only the last pre-press frame is kept
  expect(ptt.press()).toBe('k1')
  expect(ptt.press()).toBeNull()
  expect(sock.messages()).toEqual([{ type: 'start', turn_key: 'k1', mode: 'ptt' }])
  expect(sock.binary()).toHaveLength(1)
  ptt.frame(frame())
  vi.advanceTimersByTime(900)
  expect(ptt.release()).toBe('stop')
  ptt.frame(frame())
  expect(sock.messages()).toHaveLength(1) // still in the tail
  ptt.frame(frame())
  expect(sock.messages().at(-1)).toEqual({ type: 'stop', turn_key: 'k1' })
  ptt.frame(frame())
  expect(sock.binary()).toHaveLength(4)

  ptt.press()
  vi.advanceTimersByTime(100) // a tap
  expect(ptt.release()).toBe('cancel')
  expect(sock.messages().at(-1)).toEqual({ type: 'cancel', turn_key: 'k2' })

  ptt.press() // frames stop coming: the tail still ends
  vi.advanceTimersByTime(1000)
  ptt.release()
  vi.advanceTimersByTime(400)
  expect(sock.messages().at(-1)).toEqual({ type: 'stop', turn_key: 'k3' })
  expect(ends).toEqual(['k1:stop:900', 'k2:cancel:100', 'k3:stop:1000'])

  ptt.press() // and a forgotten hold stops before the server's 30 s limit
  vi.advanceTimersByTime(29_000 + 400)
  expect(sock.messages().at(-1)).toEqual({ type: 'stop', turn_key: 'k4' })
})

test('browser speech only speaks a reply in its own language', async () => {
  const voices = [
    { lang: 'en-GB', name: 'Daniel' }, { lang: 'en-US', name: 'Samantha', localService: true },
    { lang: 'es-ES', name: 'Monica' }, { lang: 'es-MX', name: 'Paulina' }, { lang: 'te-IN', name: 'Telugu' },
  ]
  expect(pickVoice(voices, 'en')?.name).toBe('Samantha')
  expect(pickVoice(voices, 'es')?.name).toBe('Paulina')
  expect(pickVoice(voices, 'ta')).toBeNull()
  const speech = fakeSpeech(voices)
  const handle = await speakText(speech, 'రెండు చికెన్ బిర్యానీ', 'te')
  expect(speech.spoken).toHaveLength(1)
  expect(speech.spoken[0]).toMatchObject({ text: 'రెండు చికెన్ బిర్యానీ', lang: 'te-IN' })
  speech.spoken[0].onend?.()
  await expect(handle?.done).resolves.toBeUndefined()
  expect(await speakText(speech, 'இரண்டு பிரியாணி', 'ta')).toBeNull()
  expect(speech.spoken).toHaveLength(1)
  expect(await speakText(null, 'hi', 'en')).toBeNull()
})

test('voices that load late are waited for', async () => {
  let voices: { lang: string; name: string }[] = []
  let changed: () => void = () => {}
  const speech = { ...fakeSpeech([]), getVoices: () => voices, onVoicesChanged: (cb: () => void) => {
    changed = cb
    return () => {}
  } }
  const pending = speakText(speech, 'Hello', 'en')
  await settle()
  voices = [{ lang: 'en-US', name: 'Alex' }]
  changed()
  expect(await pending).not.toBeNull()
})
