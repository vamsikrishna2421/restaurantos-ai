/**
 * `/kiosk` (CONTRACTS_API §8, WORK_PACKAGES P20): the guest's ordering screen. Tap-to-talk with live captions, the
 * reply as text then audio, menu tiles that add through `cart_ops` (no AI), the order card with totals from code and
 * the confirm button, a typed fallback and a language chip. The screen opens straight on ordering — session and
 * voice socket start on mount, and only the microphone waits for the first talk tap (the gesture its permission
 * prompt needs). Menu availability comes live from the tenant feed the shell keeps open; the conversation runs
 * over `/ws/voice` (see controller.ts).
 */

import { Wifi, WifiOff } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useStore } from 'zustand'
import { SampleBadge } from '../../components/badges'
import { Chip, cx } from '../../components/ui'
import type { DegradedComponent } from '../../contracts/generated'
import { browserAudioDeps } from '../../lib/audio/capture'
import { browserSpeech } from '../../lib/audio/speech'
import type { VoiceLang, VoiceStatus } from '../../lib/audio/voiceSocket'
import { LANGUAGE_NAMES } from '../../lib/i18n'
import type { SurfaceProps } from '../../router'
import { onPersisted, useSession, useTenant, type DegradedMap } from '../../stores'
import { CartPanel } from './CartPanel'
import { KioskController, type KioskDeps } from './controller'
import { MenuGrid } from './MenuGrid'
import type { KioskState } from './state'
import { TalkPanel } from './TalkPanel'

const LANGS: readonly VoiceLang[] = ['auto', 'en', 'te', 'ta', 'es']

/** The session cookie for this kiosk: read it, and create it (with its sandbox) when the surface opens. */
export async function ensureSession(force: boolean): Promise<void> {
  const session = useSession.getState()
  const info = force ? await session.load(true) : session.info ?? (await session.load())
  if (!info) await session.start()
}

/** Whether the session still exists: a REST read that only says no on a 401 (offline is "maybe", so keep trying). */
export async function probeSession(): Promise<boolean> {
  const info = await useSession.getState().load(true)
  return info !== null || useSession.getState().status !== 'none'
}

function sessionStore(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

export function browserKioskDeps(): KioskDeps {
  return { audio: browserAudioDeps(), speech: browserSpeech(), storage: sessionStore(), ensureSession, probeSession }
}

const DEGRADED_TEXT: Readonly<Record<DegradedComponent, string>> = {
  meta: 'The AI model is degraded: simple orders and the menu buttons still work.',
  asr: 'Speech recognition is degraded: typing and the menu buttons still work.',
  tts: 'Voice replies are degraded: replies show as text (and the browser voice where available).',
  twilio: 'The phone line is degraded. This kiosk is not affected.',
  db: 'The kitchen system is having trouble saving orders.',
}

function DegradedBanner({ degraded }: { degraded: DegradedMap }) {
  const entries = Object.entries(degraded).filter(([c]) => c !== 'twilio') as [DegradedComponent,
    NonNullable<DegradedMap[DegradedComponent]>][]
  if (entries.length === 0) return null
  return (
    <div role="status" className="shrink-0 border-b border-warn/40 bg-warn/10 px-4 py-2 text-sm text-warn">
      {entries.map(([component, info]) => (
        <p key={component}>
          {DEGRADED_TEXT[component]}{info.chaos ? ' (chaos drill)' : ''}
        </p>
      ))}
    </div>
  )
}

function Connection({ status }: { status: VoiceStatus }) {
  const ok = status === 'ready'
  const text = ok ? 'Connected' : status === 'idle' || status === 'closed' ? 'Not connected'
    : status === 'unauthorized' ? 'Session ended' : 'Reconnecting…'
  return (
    <span className={cx('inline-flex items-center gap-1.5 text-sm', ok ? 'text-ok' : 'text-warn')}>
      {ok ? <Wifi aria-hidden className="size-4" /> : <WifiOff aria-hidden className="size-4" />}
      {text}
    </span>
  )
}

export interface KioskProps {
  /** Test seams: fake audio, sockets, speech and storage (production uses the browser's). */
  deps?: Partial<KioskDeps>
}

export function Kiosk({ deps }: KioskProps) {
  const [ctl] = useState(() => new KioskController({ ...browserKioskDeps(), ...deps }))
  // the kiosk opens on the ordering interface: session and voice socket start on mount (controller.start)
  useEffect(() => {
    void ctl.start()
    return () => ctl.dispose()
  }, [ctl])
  // the persisted record of this kiosk's turns, for a turn whose answer was lost with its socket (sync.ts)
  useEffect(() => {
    const offs = [
      onPersisted('cart.updated', (payload) => ctl.onFeed({ type: 'cart.updated', payload })),
      onPersisted('order.placed', (payload) => ctl.onFeed({ type: 'order.placed', payload })),
      onPersisted('turn.completed', (payload) => ctl.onFeed({ type: 'turn.completed', payload })),
    ]
    return () => offs.forEach((off) => off())
  }, [ctl])
  // stable handlers, so the memoized menu and cart skip the 80 ms mic-level renders
  const on = useMemo(() => ({
    toggle: () => ctl.toggleTalk(), text: (t: string) => ctl.sendText(t),
    toggleTyped: () => ctl.toggleTyped(), add: (code: string) => ctl.addItem(code),
    qty: (lineNo: number, qty: number) => void ctl.setQty(lineNo, qty), confirm: (yes: boolean) => void ctl.confirm(yes),
    newOrder: () => ctl.newOrder(),
  }), [ctl])
  const s: KioskState = useStore(ctl.store)
  const menu = useTenant((t) => t.data?.menu ?? null)
  const tenantName = useTenant((t) => t.data?.tenant.name ?? 'Spice Route (sample)')
  const degraded = useTenant((t) => t.degraded)
  const ttsState = degraded.tts?.state ?? 'ok'
  useEffect(() => ctl.setTtsDown(ttsState !== 'ok'), [ctl, ttsState])
  const placedOrder = useTenant((t) => t.data?.orders_open.find((o) => o.id === s.placedOrderId) ?? null)
  const placed = useMemo(() => (s.placedOrderId
    ? { orderId: s.placedOrderId, order: placedOrder, replyText: s.reply?.text ?? null } : null),
  [s.placedOrderId, placedOrder, s.reply])
  const restaurant = tenantName.replace(/\s*\(sample\)\s*$/i, '')
  const speaks = (lang: VoiceLang) => lang === 'auto' || s.speakLanguages.includes(lang)

  return (
    <>
      <div className="flex h-full min-h-0 flex-col bg-ink text-text" data-testid="kiosk-body">
        <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="flex items-center gap-3">
            <span className="text-2xl font-bold tracking-tight">{restaurant}</span>
            <SampleBadge name={tenantName} />
          </div>
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Language">
            {LANGS.map((lang) => (
              <Chip key={lang} selected={s.lang === lang} disabled={s.busy} onClick={() => ctl.setLang(lang)}
                lang={lang === 'auto' ? undefined : lang}
                title={speaks(lang) ? undefined
                  : 'Understood; replies stay in English until this language is reviewed'}>
                {lang === 'auto' ? 'Auto' : LANGUAGE_NAMES[lang]}
              </Chip>
            ))}
            <Connection status={s.voice} />
          </div>
        </header>
        <DegradedBanner degraded={degraded} />
        {s.notice && (
          <div role={s.notice.tone === 'bad' ? 'alert' : 'status'}
            className={cx('flex shrink-0 items-center justify-between gap-3 px-4 py-2 text-sm',
              s.notice.tone === 'bad' ? 'bg-bad/15 text-bad' : s.notice.tone === 'warn' ? 'bg-warn/10 text-warn'
                : 'bg-panel-2 text-text')}>
            <span>{s.notice.text}</span>
            <button type="button" className="text-xs underline" onClick={() => ctl.dismissNotice()}>Dismiss</button>
          </div>
        )}
        {/* lg: an app layout — the left column is the scrolling menu over the cart, and the voice panel is
            docked as a persistent right rail (full height, its own scroll, in view while the menu scrolls).
            Smaller screens scroll as one page in the order talk, cart, menu (the order-* classes). */}
        <main className={cx('mx-auto grid min-h-0 w-full max-w-[1500px] flex-1 grid-cols-[minmax(0,1fr)] gap-4',
          'overflow-x-hidden overflow-y-auto p-4',
          'lg:grid-cols-[minmax(0,1fr)_minmax(360px,440px)] lg:grid-rows-[minmax(0,1fr)_minmax(0,45%)] lg:overflow-hidden')}>
          <div className="order-1 lg:order-none lg:col-start-2 lg:row-span-2 lg:min-h-0 lg:overflow-y-auto lg:[&>section]:min-h-full">
            <TalkPanel view={s} onToggle={on.toggle} onText={on.text}
              onToggleTyped={on.toggleTyped} />
          </div>
          {menu && (
            <div className="order-2 lg:order-none lg:col-start-1 lg:row-start-2 lg:flex lg:min-h-0 lg:overflow-y-auto">
              <CartPanel menu={menu} lang={s.lang} cart={s.cart} rejected={s.rejected}
                readback={s.dialogue === 'readback' || s.dialogue === 'confirm'} busy={s.busy}
                placing={s.pending === 'confirm'} enabled placed={placed} onQty={on.qty} onAdd={on.add}
                onConfirm={on.confirm} onNewOrder={on.newOrder} />
            </div>
          )}
          <div className="order-3 min-w-0 lg:order-none lg:col-start-1 lg:row-start-1 lg:min-h-0 lg:overflow-y-auto lg:pr-1" data-testid="menu-scroll">
            {menu ? (
              <MenuGrid menu={menu} lang={s.lang} cart={s.cart} enabled onAdd={on.add} />
            ) : (
              <p className="p-8 text-center text-muted">Loading the menu…</p>
            )}
          </div>
        </main>
      </div>
    </>
  )
}

export default function KioskSurface(_props: SurfaceProps) {
  return <Kiosk />
}
