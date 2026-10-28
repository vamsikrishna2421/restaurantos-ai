/**
 * The action page, `/` (P35; CONTRACTS_API §8; MASTER_PLAN §5; DECISIONS D17): the first thing a judge sees.
 *
 * - The hero: "Hear a Telugu-English order go through live (real Meta models)" plays a Telugu-English sample clip
 *   (`heroClip`; never another language under that label) here while the server streams the same audio through live
 *   ASR and Spark; "Run the full story live" (story.ts) reaches an approved, verified GM plan in two clicks;
 *   hold-to-talk; the public phone number.
 * - "Call now" arms this kitchen's code (Call.tsx); the meters as one chip; the status banner; Reset; "Explore
 *   further", the 5-step tour (Tour.tsx); "Watch a recorded run" (the `recorded-run` slot, always watermarked
 *   RECORDED); "What doesn't work yet"; Feedback. `?admin=1` shows the admin sign-in instead.
 *
 * Nothing is created on page load: the session and its paused sandbox come from the first click that needs them
 * (`ensureSession`); the shell reads an existing session and opens its tenant feed. A kitchen that lapsed while the
 * page was open is dropped at the first refusal (`recheckSession`), the next Start-type click makes a new one, and
 * the story starts over there (`followKitchen`). Spice Route is a sample restaurant and the SampleBadge stays in view.
 */

import { ExternalLink, Phone, Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import { SampleBadge, SimClock } from '../../components/badges'
import { Button, Card, Toaster } from '../../components/ui'
import { isApiError } from '../../lib/api'
import { Link, type SurfaceProps } from '../../router'
import { Slot } from '../../slots'
import { onPersisted, useSession, useTenant } from '../../stores'
import { AdminLogin } from './AdminLogin'
import { attempt, ensureSession, formatPhone, heroClip, judgeApi, PUBLIC_NUMBER, telHref } from './api'
import { CallCard } from './Call'
import { FeedbackButton, KnownLimits, MetersChip, PasscodeOffer, ResetButton, StatusBanner } from './Extras'
import { useLiveFeed } from './live'
import { configureStory, followKitchen, storyView, useStory } from './story'
import { StoryButton, StoryCard } from './StoryCard'
import { ClipCard, HoldToTalk, useLineView } from './Talk'
import { TourCard } from './Tour'
import { VoiceLine, type LineDeps } from './voice'

const NO_CLIP = 'This server has no Telugu-English sample clip yet. Hold to talk below, call the number, or run the ' +
  'full story (it types the same order in English).'
/** A returning judge whose kitchen was swept (GET /api/session: 401 `sandbox_gone`); in-page refusals say it there. */
const LAPSED = 'Welcome back. Your earlier kitchen expired after 24 h without use; any Start button makes a new one.'

export interface ActionPageProps {
  /** Test seams for every voice line (socket, audio, clip fetch). */
  lineDeps?: LineDeps
}

export function ActionPage({ lineDeps = {} }: ActionPageProps) {
  useState(() => {
    configureStory(lineDeps)
    return true
  })
  useLiveFeed()
  const session = useSession((s) => s.info)
  const lapsed = useSession((s) => s.status === 'none' && s.error?.details.reason === 'sandbox_gone')
  const kitchen = session?.tenant_id ?? null
  useEffect(() => followKitchen(kitchen), [kitchen])
  const data = useTenant((s) => s.data)
  const driver = useStory()
  const view = storyView(data, driver)
  const [hero, setHero] = useState<VoiceLine | null>(null)
  const [talk, setTalk] = useState<VoiceLine | null>(null)
  const [heroBusy, setHeroBusy] = useState(false)
  const [heroNote, setHeroNote] = useState<string | null>(null)
  const heroView = useLineView(hero)
  const talkView = useLineView(talk)
  useEffect(() => () => hero?.dispose(), [hero])
  useEffect(() => () => talk?.dispose(), [talk])
  // a reset kitchen starts with fresh conversations (a Dialogue belongs to the kitchen it began in)
  useEffect(() => onPersisted('sandbox.reset', () => {
    setHero(null)
    setTalk(null)
  }), [])

  const playHero = () => {
    const line = hero ?? new VoiceLine('sample', lineDeps)
    line.unlock() // inside the click, so the clip is audible everywhere
    if (line !== hero) setHero(line)
    setHeroNote(null)
    setHeroBusy(true)
    void attempt(async () => {
      await ensureSession()
      const clip = heroClip(await judgeApi.samples())
      if (clip === null) setHeroNote(NO_CLIP)
      else await line.playClip(clip)
    }, (err) => {
      const missing = isApiError(err, 'not_found') || isApiError(err, 'not_wired')
      if (missing) setHeroNote(NO_CLIP)
      return missing
    }).finally(() => setHeroBusy(false))
  }
  const enableTalk = () => {
    const line = talk ?? new VoiceLine('order', lineDeps, { warm: true })
    line.unlock()
    if (line !== talk) setTalk(line)
    void attempt(async () => {
      await ensureSession()
      await line.enableMic()
    })
  }
  const menu = data?.menu ?? null
  const last = heroView.result ?? talkView.result

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-ink/90 backdrop-blur">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
          <span className="text-lg font-bold tracking-tight">RestaurantOS <span className="text-accent">AI</span></span>
          <SampleBadge name={data?.tenant.name} compact />
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {data && <SimClock />}
            <MetersChip session={session} />
            {session && <ResetButton />}
          </div>
        </div>
      </header>
      <StatusBanner />
      <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-4 py-6 sm:py-10">
        <PasscodeOffer />
        {lapsed && view.stage === 'idle' && <p role="status" className="text-sm text-warn">{LAPSED}</p>}
        <section aria-labelledby="hero-title" className="flex flex-col gap-5">
          <h1 id="hero-title" className="max-w-3xl text-3xl font-bold leading-tight tracking-tight sm:text-5xl">
            The AI General Manager for restaurants
          </h1>
          <p className="max-w-2xl text-lg text-muted">
            It takes orders by voice in Telugu, Tamil, Spanish and English, runs the kitchen screens, and when stock runs
            short it plans the fix and waits for your approval. Spice Route is a sample restaurant; the AI here runs live
            on Meta models, and every model call is metered.
          </p>
          <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
            <Button variant="primary" size="xl" className="w-full justify-start text-left sm:w-auto"
              icon={<Play className="size-6" />} loading={heroBusy} onClick={playHero}>
              Hear a Telugu-English order go through live (real Meta models)
            </Button>
            {view.stage === 'idle' && <StoryButton />}
          </div>
          <p className="flex flex-wrap items-center gap-x-2 text-sm text-muted">
            {view.stage === 'idle' && <span>The full story: one click starts the dinner rush and places the order;
              the AI plan stops at your approval.</span>}
            <span className="inline-flex items-center gap-1.5">
              <Phone aria-hidden className="size-4" />Or call
              <a href={telHref(PUBLIC_NUMBER)} className="font-semibold text-accent underline-offset-4 hover:underline">
                {formatPhone(PUBLIC_NUMBER)}</a>
            </span>
          </p>
          {heroNote && <p role="status" className="text-sm text-warn">{heroNote}</p>}
          {hero && heroView.clip && <ClipCard clip={heroView.clip} view={heroView} menu={menu} />}
        </section>
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,25rem)]">
          <div className="flex min-w-0 flex-col gap-6">
            {view.stage !== 'idle' && <StoryCard view={view} data={data} />}
            <Card panel="talk" title="Hold to talk">
              <HoldToTalk line={talk} menu={menu} onEnable={enableTalk} />
            </Card>
          </div>
          <div className="flex min-w-0 flex-col gap-6">
            <CallCard session={session} />
          </div>
        </div>
        <TourCard data={data} />
        <Slot name="recorded-run" props={{ ensureSession }} />
        <KnownLimits />
      </main>
      <footer className="border-t border-line">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-4 text-sm
          text-muted">
          <SampleBadge name={data?.tenant.name} />
          <Link to="/proof" className="underline">Proof</Link>
          <Link to="/status" className="underline">Status</Link>
          <Link to="/privacy" className="underline">Privacy</Link>
          <a href="https://github.com/vamsikrishna2421/restaurantos-ai" target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-1 underline">Source<ExternalLink aria-hidden className="size-3.5" /></a>
          {session && <span className="ml-auto"><FeedbackButton traceId={last?.trace_id ?? null}
            sessionId={last?.session_id ?? null} /></span>}
        </div>
      </footer>
      <Toaster />
    </div>
  )
}

export default function JudgeSurface({ query }: SurfaceProps) {
  return query.get('admin') === '1' ? <AdminLogin /> : <ActionPage />
}
