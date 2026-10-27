// @vitest-environment jsdom
// (the docblock and the jest-dom import keep this suite green even when vitest runs without src/web/vite.config.ts)
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { SampleClip, SampleUploadMeta } from '../../contracts/generated'
import { fakeAudio, pcmFrame } from '../../lib/audio/testkit'
import { CONSENT_TEXTS, Recorder } from './index'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const forbidden = () => json(403, { error: { code: 'forbidden_role', message: 'Admins only', details: {} } })

const CLIP: SampleClip = {
  id: 'smp_01', language: 'te-en', label: 'Telugu-English biryani order', source: 'consented_recording',
  duration_ms: 240, transcript: 'rendu chicken biryani', transcript_en: 'two chicken biryani', consent_id: 'cns_1',
  audio_url: '/api/judge/samples/smp_01/audio',
}

async function settle(rounds = 30) {
  for (let i = 0; i < rounds; i++) await Promise.resolve()
}

async function click(name: string | RegExp) {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }))
    await settle()
  })
}

function fillConsent(alias = 'akshitha') {
  fireEvent.change(screen.getByLabelText('Speaker alias'), { target: { value: alias } })
  fireEvent.click(screen.getByRole('checkbox', { name: /has read this text and agrees/i }))
}

beforeEach(() => {
  window.history.replaceState(null, '', '/record')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('record_redirects_on_403', async () => {
  const fetch = vi.fn().mockImplementation(async () => forbidden())
  vi.stubGlobal('fetch', fetch)
  render(<Recorder audio={fakeAudio().deps} />)
  expect(screen.getByRole('button', { name: 'Save consent' })).toBeDisabled() // alias and checkbox first
  expect(screen.getByRole('button', { name: 'Record' })).toBeDisabled() // and no recording before consent
  fillConsent()
  await click('Save consent')
  const [path, init] = fetch.mock.calls[0]
  expect(path).toBe('/api/admin/consents')
  expect(JSON.parse(init.body)).toEqual({ subject_alias: 'akshitha', kind: 'sample_clip',
    text_version: CONSENT_TEXTS.sample_clip.version })
  expect(window.location.pathname + window.location.search).toBe('/?admin=1')
})

test('a consented clip uploads as 16 kHz mono WAV with its consent id; a 403 on upload redirects too', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(json(200, { id: 'cns_1' }))
    .mockResolvedValueOnce(json(200, CLIP))
    .mockResolvedValueOnce(forbidden())
  vi.stubGlobal('fetch', fetch)
  const audio = fakeAudio()
  render(<Recorder audio={audio.deps} />)
  fillConsent('vamsi')
  await click('Save consent')
  expect(screen.getByText('Saved · cns_1')).toBeInTheDocument()

  await click('Record')
  expect(audio.contexts[0].modules).toEqual(['/worklets/pcm-capture.js'])
  act(() => [1, 2, 3].forEach((v) => audio.nodes[0].emit(pcmFrame(v))))
  expect(screen.getByLabelText('Recorded time')).toHaveTextContent('0:00')
  await click('Stop')
  expect(audio.tracks[0].stopped).toBe(true) // the microphone is released between clips
  expect(screen.getByRole('button', { name: 'Upload clip' })).toBeDisabled() // label and transcript first
  fireEvent.change(screen.getByLabelText('Label'), { target: { value: CLIP.label } })
  fireEvent.change(screen.getByLabelText(/Transcript \(as spoken/), { target: { value: 'rendu chicken biryani' } })
  fireEvent.change(screen.getByLabelText('Transcript in English'), { target: { value: 'two chicken biryani' } })
  await click('Upload clip')

  const [path, init] = fetch.mock.calls[1]
  expect(path).toBe('/api/admin/samples')
  const form = init.body as FormData
  const meta = JSON.parse(form.get('meta') as string) as SampleUploadMeta
  expect(meta).toEqual({ language: 'te-en', label: CLIP.label, source: 'consented_recording',
    transcript: 'rendu chicken biryani', transcript_en: 'two chicken biryani', consent_id: 'cns_1' })
  const wav = form.get('wav') as Blob
  expect(wav.type).toBe('audio/wav')
  expect(wav.size).toBe(44 + 3 * 2560)
  const header = new DataView(await wav.arrayBuffer())
  expect(header.getUint32(24, true)).toBe(16_000)
  expect(header.getUint16(22, true)).toBe(1)
  expect(header.getInt16(44, true)).toBe(1)
  expect(screen.getByText(/Uploaded “Telugu-English biryani order”/)).toBeInTheDocument()

  // the next clip, same consent: the server now refuses the role → to the admin login
  await click('Record')
  act(() => audio.nodes.at(-1)!.emit(pcmFrame(4)))
  await click('Stop')
  fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'again' } })
  fireEvent.change(screen.getByLabelText(/Transcript \(as spoken/), { target: { value: 'okka samosa' } })
  await click('Upload clip')
  expect(window.location.pathname + window.location.search).toBe('/?admin=1')
})
