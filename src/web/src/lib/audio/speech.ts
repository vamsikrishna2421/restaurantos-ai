/**
 * Browser speech, the kiosk's last TTS fallback (ARCHITECTURE_VOICE_MEDIA §7.5: live TTS → stitched segments →
 * browser `speechSynthesis`; captions always). Used when the server reports TTS degraded (no ElevenLabs key yet,
 * quota, chaos drill): the reply is spoken in its own language only when this browser has a voice for it, otherwise
 * the captions carry it alone. A Telugu reply is never read out by an English voice.
 */

import type { SpeakLang } from '../../contracts/generated'

export interface VoiceLike {
  lang: string
  name: string
  localService?: boolean
  default?: boolean
}

export interface UtteranceLike {
  text: string
  lang: string
  voice: unknown
  rate: number
  onend: (() => void) | null
  onerror: (() => void) | null
}

export interface SpeechDeps {
  getVoices(): VoiceLike[]
  speak(u: UtteranceLike): void
  cancel(): void
  makeUtterance(text: string): UtteranceLike
  /** Subscribe to `voiceschanged` (Chrome fills the list asynchronously); returns the unsubscribe function. */
  onVoicesChanged?(cb: () => void): () => void
}

/** `window.speechSynthesis`, or null when the browser has none (jsdom, some kiosks). */
export function browserSpeech(): SpeechDeps | null {
  const g = globalThis as unknown as {
    speechSynthesis?: SpeechSynthesis
    SpeechSynthesisUtterance?: new (text: string) => SpeechSynthesisUtterance
  }
  const synth = g.speechSynthesis
  const Utterance = g.SpeechSynthesisUtterance
  if (!synth || !Utterance) return null
  return {
    getVoices: () => synth.getVoices(),
    speak: (u) => synth.speak(u as unknown as SpeechSynthesisUtterance),
    cancel: () => synth.cancel(),
    makeUtterance: (text) => new Utterance(text) as unknown as UtteranceLike,
    onVoicesChanged: (cb) => {
      if (typeof synth.addEventListener !== 'function') return () => {} // older engines: the timeout decides
      synth.addEventListener('voiceschanged', cb)
      return () => synth.removeEventListener('voiceschanged', cb)
    },
  }
}

/** Preferred regions per reply language; any other voice of the language comes after them. */
const PREFERRED: Readonly<Record<SpeakLang, readonly string[]>> = {
  en: ['en-us', 'en-in', 'en-gb'],
  te: ['te-in'],
  ta: ['ta-in', 'ta-lk', 'ta-sg'],
  es: ['es-us', 'es-mx', 'es-419', 'es-es'],
}

function norm(tag: string): string {
  return tag.replace('_', '-').toLowerCase()
}

/** The best voice for `lang`, or null when this browser cannot speak it. */
export function pickVoice(voices: readonly VoiceLike[], lang: SpeakLang): VoiceLike | null {
  const own = voices.filter((v) => norm(v.lang) === lang || norm(v.lang).startsWith(`${lang}-`))
  if (own.length === 0) return null
  for (const region of PREFERRED[lang]) {
    const exact = own.filter((v) => norm(v.lang) === region)
    const local = exact.find((v) => v.localService)
    if (local ?? exact[0]) return local ?? exact[0]
  }
  return own.find((v) => v.localService) ?? own[0]
}

/** The voices, waiting up to `timeoutMs` for Chrome's asynchronous list. */
export function loadVoices(speech: SpeechDeps, timeoutMs = 800): Promise<VoiceLike[]> {
  const now = speech.getVoices()
  if (now.length > 0 || !speech.onVoicesChanged) return Promise.resolve(now)
  return new Promise((resolve) => {
    let off = () => {}
    const timer = globalThis.setTimeout(() => {
      off()
      resolve(speech.getVoices())
    }, timeoutMs)
    off = speech.onVoicesChanged!(() => {
      globalThis.clearTimeout(timer)
      off()
      resolve(speech.getVoices())
    })
  })
}

export interface SpeakHandle {
  /** Resolves when the speech ends (or fails). */
  done: Promise<void>
}

/**
 * Speak `text` in `lang` with a matching browser voice. Resolves null when no voice exists (captions only), else a
 * handle whose `done` settles when speaking stops. Anything still being spoken is cancelled first.
 */
export async function speakText(speech: SpeechDeps | null, text: string, lang: SpeakLang): Promise<SpeakHandle | null> {
  if (!speech || text.trim() === '') return null
  try {
    const voice = pickVoice(await loadVoices(speech), lang)
    if (!voice) return null
    speech.cancel()
    const u = speech.makeUtterance(text)
    u.voice = voice
    u.lang = voice.lang
    u.rate = 1
    const done = new Promise<void>((resolve) => {
      u.onend = () => resolve()
      u.onerror = () => resolve()
    })
    speech.speak(u)
    return { done }
  } catch {
    return null // a broken speech engine means captions only, never an error on screen
  }
}
