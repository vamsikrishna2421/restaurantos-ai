/**
 * `/record` (CONTRACTS_API §3 #42 and #50, §8; WORK_PACKAGES P20): the admin-only recorder for consented clips.
 *
 * 1. **Consent.** The speaker's alias, what the clip is for, the consent text and a checkbox; "Save consent" posts
 *    `/api/admin/consents` and keeps the returned id. Nothing can be uploaded without it.
 * 2. **Clip.** Language, a label and the transcript (native script and English).
 * 3. **Record and upload.** 16 kHz mono through the push-to-talk worklet, a preview, then a multipart upload to
 *    `/api/admin/samples` (`wav` + `meta`, a JSON `SampleUploadMeta` with the consent id).
 *
 * Admin rights come from the `ros_admin` cookie, which the page cannot read: any 401/403 from an admin call sends the
 * browser to `/?admin=1` (the admin login).
 */

import { CircleStop, Mic, RotateCcw, ShieldCheck, Upload } from 'lucide-react'
import { useEffect, useReducer, useState, type ReactNode } from 'react'
import { SampleBadge } from '../../components/badges'
import { Button, Card, cx } from '../../components/ui'
import type { ConsentKind, ConsentRequest, Lang, SampleClip, SampleUploadMeta } from '../../contracts/generated'
import { api, isApiError, isAuthError } from '../../lib/api'
import { browserAudioDeps, captureProblem, type AudioDeps } from '../../lib/audio/capture'
import { formatClock } from '../../lib/format'
import { navigate, type SurfaceProps } from '../../router'
import { ClipRecorder } from './recorder'

type ClipKind = Extract<ConsentKind, 'sample_clip' | 'eval_clip'>

export const CONSENT_TEXTS: Readonly<Record<ClipKind, { version: string; label: string; text: string }>> = {
  sample_clip: {
    version: 'sample_clip-v1',
    label: 'Judge sample clip (played on the action page)',
    text: 'I agree that RestaurantOS AI may store this recording of my voice and its transcript, and play it to '
      + 'hackathon judges and visitors as a sample order that runs through the live speech recognition. I can ask '
      + 'the team to delete it at any time, and it will then be removed.',
  },
  eval_clip: {
    version: 'eval_clip-v1',
    label: 'Evaluation clip (accuracy numbers on /proof)',
    text: 'I agree that RestaurantOS AI may store this recording of my voice and its transcript and use it to '
      + 'measure speech-recognition accuracy. Only aggregate results are published. I can ask the team to delete it '
      + 'at any time, and it will then be removed.',
  },
}

const CLIP_LANGS: readonly (readonly [Lang, string])[] = [
  ['en', 'English'], ['te', 'Telugu'], ['ta', 'Tamil'], ['es', 'Spanish'], ['hi', 'Hindi'],
  ['te-en', 'Telugu-English'], ['ta-en', 'Tamil-English'], ['es-en', 'Spanish-English'], ['hi-en', 'Hindi-English'],
]

export const ADMIN_LOGIN = '/?admin=1'

/** Not an admin (or no admin cookie): to the admin login. Returns true when it redirected. */
function redirectIfNotAdmin(err: unknown): boolean {
  if (!isAuthError(err)) return false
  navigate(ADMIN_LOGIN, { replace: true })
  return true
}

function errorText(err: unknown): string {
  if (isApiError(err, 'consent_missing')) return 'The server has no consent for this clip. Save the consent again.'
  if (isApiError(err, 'network_error')) return 'The server could not be reached. Check the connection and retry.'
  if (isApiError(err) && err.status === 413) return 'The clip is too large to upload.'
  return isApiError(err) ? err.message : 'Something went wrong. Please retry.'
}

const FIELD = 'w-full rounded-lg border border-line bg-ink px-3 py-2 text-text focus:border-accent focus:outline-none'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-sm font-medium">
      {label}
      {children}
    </label>
  )
}

export interface RecordProps {
  /** Test seam: fake audio (production uses the browser's). */
  audio?: AudioDeps | null
}

export function Recorder({ audio }: RecordProps) {
  const [rec] = useState(() => new ClipRecorder(audio === undefined ? browserAudioDeps() : audio))
  const [, rerender] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    rec.onChange = rerender
    return () => {
      rec.onChange = null
      rec.dispose()
    }
  }, [rec])

  const [alias, setAlias] = useState('')
  const [kind, setKind] = useState<ClipKind>('sample_clip')
  const [agreed, setAgreed] = useState(false)
  const [consentId, setConsentId] = useState<string | null>(null)
  const [lang, setLang] = useState<Lang>('te-en')
  const [label, setLabel] = useState('')
  const [transcript, setTranscript] = useState('')
  const [transcriptEn, setTranscriptEn] = useState('')
  const [busy, setBusy] = useState<'consent' | 'upload' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [uploaded, setUploaded] = useState<SampleClip | null>(null)
  const [preview, setPreview] = useState<string | null>(null)

  const clip = rec.clip
  useEffect(() => {
    if (!clip || typeof URL.createObjectURL !== 'function') return
    const url = URL.createObjectURL(clip.wav)
    setPreview(url)
    return () => {
      URL.revokeObjectURL(url)
      setPreview(null)
    }
  }, [clip])

  const consent = CONSENT_TEXTS[kind]
  const saveConsent = async () => {
    setBusy('consent')
    setError(null)
    try {
      const body: ConsentRequest = { subject_alias: alias.trim(), kind, text_version: consent.version }
      const res = await api<{ id: string }>('POST', '/api/admin/consents', body)
      setConsentId(res.id)
    } catch (err) {
      if (!redirectIfNotAdmin(err)) setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  const upload = async () => {
    if (!clip || !consentId) return
    setBusy('upload')
    setError(null)
    const meta: SampleUploadMeta = {
      language: lang, label: label.trim(), source: 'consented_recording', transcript: transcript.trim(),
      transcript_en: transcriptEn.trim(), consent_id: consentId,
    }
    const form = new FormData()
    form.append('wav', clip.wav, 'clip.wav')
    form.append('meta', JSON.stringify(meta))
    try {
      setUploaded(await api<SampleClip>('POST', '/api/admin/samples', form))
      rec.reset()
      setLabel('')
      setTranscript('')
      setTranscriptEn('')
    } catch (err) {
      if (redirectIfNotAdmin(err)) return
      if (isApiError(err, 'consent_missing')) setConsentId(null)
      setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  const recording = rec.state === 'recording' || rec.state === 'starting'
  const consentReady = alias.trim() !== '' && agreed
  const details = label.trim() !== '' && transcript.trim() !== ''
  const canUpload = consentId !== null && agreed && clip !== null && details && busy === null
  const micProblem = rec.state === 'denied' || rec.state === 'unsupported' || rec.state === 'error'
    ? captureProblem(rec.state) : null

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">Consented clip recorder</h1>
        <SampleBadge compact />
      </header>
      <p className="text-sm text-muted">
        Admin only. Record only people who have read the consent text and agreed to it. Clips are 16 kHz mono and
        at most 30 seconds.
      </p>
      {error && <p role="alert" className="rounded-lg bg-bad/15 px-3 py-2 text-bad">{error}</p>}

      <Card title="1 · Consent" tone={consentId ? 'ok' : undefined}
        actions={consentId ? <span className="text-sm text-ok">Saved · {consentId}</span> : null}>
        <div className="flex flex-col gap-3">
          <Field label="Speaker alias">
            <input className={FIELD} value={alias} disabled={consentId !== null} placeholder="e.g. vamsi"
              onChange={(e) => setAlias(e.target.value)} />
          </Field>
          <Field label="What the clip is for">
            <select className={FIELD} value={kind} disabled={consentId !== null}
              onChange={(e) => setKind(e.target.value as ClipKind)}>
              {(Object.keys(CONSENT_TEXTS) as ClipKind[]).map((k) => (
                <option key={k} value={k}>{CONSENT_TEXTS[k].label}</option>
              ))}
            </select>
          </Field>
          <blockquote className="rounded-lg border-l-4 border-accent bg-panel-2 p-3 text-sm">
            {consent.text}
            <span className="mt-1 block text-xs text-muted">Consent text {consent.version}</span>
          </blockquote>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-0.5 size-4 accent-accent" checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)} />
            The speaker has read this text and agrees to it.
          </label>
          {consentId === null ? (
            <Button variant="primary" icon={<ShieldCheck className="size-4" />} disabled={!consentReady || busy !== null}
              loading={busy === 'consent'} onClick={() => void saveConsent()} className="self-start">
              Save consent
            </Button>
          ) : (
            <Button variant="ghost" size="sm" className="self-start" onClick={() => {
              setConsentId(null)
              setAgreed(false) // a new speaker agrees afresh
            }}>
              New speaker or purpose
            </Button>
          )}
        </div>
      </Card>

      <Card title="2 · Clip details">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Language">
            <select className={FIELD} value={lang} onChange={(e) => setLang(e.target.value as Lang)}>
              {CLIP_LANGS.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
            </select>
          </Field>
          <Field label="Label">
            <input className={FIELD} value={label} maxLength={80} placeholder="Telugu-English biryani order"
              onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Field label="Transcript (as spoken, native script)">
            <textarea className={FIELD} rows={2} value={transcript} onChange={(e) => setTranscript(e.target.value)} />
          </Field>
          <Field label="Transcript in English">
            <textarea className={FIELD} rows={2} value={transcriptEn} onChange={(e) => setTranscriptEn(e.target.value)} />
          </Field>
        </div>
      </Card>

      <Card title="3 · Record and upload" panel="recorder">
        <div className="flex flex-col items-center gap-4">
          <div className="flex items-center gap-4">
            {recording ? (
              <Button variant="danger" size="xl" icon={<CircleStop className="size-6" />} onClick={() => rec.stop()}>
                Stop
              </Button>
            ) : (
              <Button variant="primary" size="xl" icon={<Mic className="size-6" />} disabled={consentId === null}
                onClick={() => void rec.start()}>
                {clip ? 'Record again' : 'Record'}
              </Button>
            )}
            <span className="font-mono text-2xl tabular-nums" aria-label="Recorded time">{formatClock(rec.ms)}</span>
          </div>
          <div className="h-2 w-full max-w-sm overflow-hidden rounded-full bg-panel-2" aria-hidden>
            <div className={cx('h-full bg-ok transition-[width] duration-75', !recording && 'opacity-0')}
              style={{ width: `${Math.round(Math.min(1, rec.level * 4) * 100)}%` }} />
          </div>
          {consentId === null && <p className="text-sm text-muted">Save the consent first.</p>}
          {micProblem && <p role="status" className="text-sm text-warn">{micProblem}</p>}
          {clip && (
            <div className="flex w-full flex-col items-center gap-3">
              {preview && <audio controls src={preview} className="w-full max-w-md" />}
              <div className="flex gap-2">
                <Button variant="ghost" icon={<RotateCcw className="size-4" />} onClick={() => rec.reset()}>Discard</Button>
                <Button variant="ok" icon={<Upload className="size-4" />} disabled={!canUpload}
                  loading={busy === 'upload'} onClick={() => void upload()}>
                  Upload clip
                </Button>
              </div>
              {!details && <p className="text-sm text-muted">Add a label and the transcript to upload.</p>}
            </div>
          )}
          {uploaded && (
            <p role="status" className="text-ok">
              Uploaded “{uploaded.label}” ({uploaded.id}, {formatClock(uploaded.duration_ms)}).
            </p>
          )}
        </div>
      </Card>
    </main>
  )
}

export default function RecordSurface(_props: SurfaceProps) {
  return <Recorder />
}
