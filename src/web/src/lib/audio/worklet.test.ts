// @vitest-environment node
/**
 * The real `public/worklets/pcm-capture.js`, evaluated the way an AudioWorkletGlobalScope runs it: with
 * `AudioWorkletProcessor`, `registerProcessor` and `sampleRate` as globals, fed 128-sample render quanta.
 */
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, test } from 'vitest'
import { FRAME_BYTES, FRAME_SAMPLES } from './pcm'

const SOURCE = readFileSync(new URL('../../../public/worklets/pcm-capture.js', import.meta.url), 'utf8')
const QUANTUM = 128

interface Processor {
  process(inputs: Float32Array[][]): boolean
}

function loadWorklet(contextRate: number, viaOptions = true) {
  const posted: { data: unknown; transfer: unknown }[] = []
  const registered: Record<string, new (options: unknown) => Processor> = {}
  class AudioWorkletProcessor {
    readonly port = { postMessage: (data: unknown, transfer: unknown) => posted.push({ data, transfer }) }
  }
  runInNewContext(SOURCE, {
    AudioWorkletProcessor, sampleRate: contextRate, Int16Array, Math, Number,
    registerProcessor: (name: string, cls: new (options: unknown) => Processor) => {
      registered[name] = cls
    },
  })
  const Ctor = registered['pcm-capture']
  const node = new Ctor(viaOptions ? { processorOptions: { sampleRate: contextRate } } : {})
  const feed = (samples: Float32Array) => {
    for (let at = 0; at < samples.length; at += QUANTUM) {
      expect(node.process([[samples.subarray(at, at + QUANTUM)]])).toBe(true)
    }
  }
  const frames = () => posted.map((p) => new Int16Array(p.data as ArrayBuffer))
  return { registered, node, posted, feed, frames }
}

const sine = (rate: number, seconds: number, hz = 440, amp = 0.5) =>
  Float32Array.from({ length: Math.round(rate * seconds) }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / rate))

test('worklet_frames_1280_samples_16k', () => {
  const w = loadWorklet(16_000)
  expect(Object.keys(w.registered)).toEqual(['pcm-capture'])

  // 80 ms at 16 kHz is exactly one frame: 1,280 samples, 2,560 bytes, transferred (not copied)
  const input = sine(16_000, 0.08)
  w.feed(input.subarray(0, FRAME_SAMPLES - QUANTUM))
  expect(w.posted).toHaveLength(0)
  w.feed(input.subarray(FRAME_SAMPLES - QUANTUM))
  expect(w.posted).toHaveLength(1)
  const [{ data, transfer }] = w.posted
  expect(data).toBeInstanceOf(ArrayBuffer)
  expect((data as ArrayBuffer).byteLength).toBe(FRAME_BYTES)
  expect(transfer).toEqual([data])
  const frame = w.frames()[0]
  expect(frame).toHaveLength(FRAME_SAMPLES)
  // no resampling at 16 kHz: each sample is the PCM16 of its input
  for (const i of [0, 1, 17, 640, 1279]) expect(frame[i]).toBe(Math.round(input[i] * (input[i] < 0 ? 32768 : 32767)))

  // one second → 12 whole frames (the 13th is still filling), each 1,280 samples
  const w2 = loadWorklet(16_000)
  w2.feed(sine(16_000, 1))
  expect(w2.frames().map((f) => f.length)).toEqual(Array(12).fill(FRAME_SAMPLES))
})

test('the worklet downsamples 48 kHz and 44.1 kHz contexts to the same 16 kHz frames', () => {
  // 48 kHz: three input samples per output sample; a constant level stays constant
  const w48 = loadWorklet(48_000)
  w48.feed(new Float32Array(3 * FRAME_SAMPLES).fill(0.25))
  expect(w48.frames()).toHaveLength(1)
  expect(new Set(w48.frames()[0])).toEqual(new Set([Math.round(0.25 * 32767)]))

  // 44.1 kHz: 80 ms (3,528 samples) is one frame, and one second yields 16,000 samples (12 frames + 640 pending)
  const w44 = loadWorklet(44_100)
  w44.feed(new Float32Array(3584).fill(-0.5)) // 28 quanta ≥ 3,528
  expect(w44.frames()).toHaveLength(1)
  expect(w44.frames()[0].every((s) => s === -16384)).toBe(true)
  const w44s = loadWorklet(44_100, false) // the global sampleRate when no option is given
  w44s.feed(sine(44_100, 1, 300))
  expect(w44s.frames()).toHaveLength(12)

  // clipping is clamped, and an empty input (no stream yet) keeps the node alive without output
  const wc = loadWorklet(16_000)
  expect(wc.node.process([[]])).toBe(true)
  expect(wc.node.process([])).toBe(true)
  wc.feed(new Float32Array(FRAME_SAMPLES).fill(3))
  expect(wc.frames()[0][0]).toBe(32767)
})
