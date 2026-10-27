/**
 * pcm-capture: the AudioWorklet behind push-to-talk and the clip recorder (ARCHITECTURE_VOICE_MEDIA §7.1,
 * CONTRACTS_API §5). It turns microphone audio into the frames `/ws/voice` expects: PCM16 little-endian, mono,
 * 16 kHz, 80 ms per frame (1,280 samples, 2,560 bytes), each posted to the page as a transferred ArrayBuffer.
 *
 * The page creates its AudioContext at 16 kHz when the browser allows it, and then nothing is resampled. Otherwise the
 * context runs at the device rate (`processorOptions.sampleRate`, else the global `sampleRate`) and each output sample
 * is the mean of the input samples in its 1/16,000 s window: a box low-pass that is plenty for speech recognition.
 * The node writes no output (silence), and keeps running until the page disconnects it.
 *
 * Plain script with no imports: files in /public are served as-is, outside the Vite build.
 */

const TARGET_RATE = 16000
const FRAME_SAMPLES = 1280
const REBASE_AT = 1e6 // keep the running sample clock small so fractions stay exact

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = (options && options.processorOptions) || {}
    const rate = Number(opts.sampleRate) || sampleRate
    this.step = rate / TARGET_RATE // input samples per output sample
    this.t = 0 // input samples seen (rebased)
    this.next = this.step // where the current output window ends
    this.sum = 0
    this.n = 0
    this.last = 0
    this.frame = new Int16Array(FRAME_SAMPLES)
    this.fill = 0
  }

  emit(value) {
    const s = value < -1 ? -1 : value > 1 ? 1 : value
    this.frame[this.fill++] = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767)
    if (this.fill === FRAME_SAMPLES) {
      const buffer = this.frame.buffer
      this.port.postMessage(buffer, [buffer])
      this.frame = new Int16Array(FRAME_SAMPLES)
      this.fill = 0
    }
  }

  process(inputs) {
    const input = inputs[0]
    const channel = input && input[0]
    if (!channel) return true // not connected yet, or between streams
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i]
      this.n++
      this.t++
      while (this.t >= this.next) {
        const value = this.n > 0 ? this.sum / this.n : this.last // n is 0 only when upsampling
        this.last = value
        this.emit(value)
        this.sum = 0
        this.n = 0
        this.next += this.step
      }
    }
    if (this.t >= REBASE_AT) {
      this.t -= REBASE_AT
      this.next -= REBASE_AT
    }
    return true
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor)
