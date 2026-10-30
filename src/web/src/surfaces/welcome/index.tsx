/**
 * `/` (P41): the front door. A splash first — the designed brand mark (the hat logo and the RestaurantOS AI
 * wordmark, large) — behind one Enter button; then the mode choice: Customer (the kiosk), Staff (shift sign-in),
 * or setting up a new restaurant (onboarding). Nothing here creates a session or reads tenant data: this is the
 * cover of the product, and everyone who opens the app lands here first.
 *
 * Pass 3 gives the cover its ceremony: the hat sits in a lit app-icon tile over ambient orange/blue/violet
 * light, the wordmark is display-sized with the gradient on "AI", and each way in is a raised card with its
 * own tinted icon tile that lifts on hover. Same doors, same words — presentation only.
 *
 * Pass 8 moves the cover into the founder's warm light system (the `.theme-light` token scope: cream
 * room, white cards, soft warm shadows) — the ceremony survives as the white hat tile and the display
 * wordmark; the dark room's colored light pools do not.
 */

import { ArrowLeft, ArrowRight, ChefHat, Store, UtensilsCrossed } from 'lucide-react'
import { useState, type ComponentType } from 'react'
import { BrandHat, Button, cx } from '../../components/ui'
import { navigate, type SurfaceProps } from '../../router'

interface Mode {
  title: string
  blurb: string
  icon: ComponentType<{ className?: string }>
  /** The icon tile's tint: a soft fill, a ring and a small glow in the mode's own colour. */
  tile: string
  to: string
}

const MODES: readonly Mode[] = [
  {
    title: 'Customer', blurb: 'See the menu and place an order — tap, or just say it.', icon: UtensilsCrossed,
    tile: 'bg-accent/15 text-accent shadow-glow-accent-sm ring-accent/30', to: '/kiosk',
  },
  {
    title: 'Staff', blurb: 'Sign in with your PIN and take your job for this shift.', icon: ChefHat,
    tile: 'bg-ai/15 text-ai-2 shadow-glow-ai-sm ring-ai/30', to: '/staff',
  },
  {
    title: 'Set up a new restaurant', blurb: 'Bring your menu over and open your doors here.', icon: Store,
    tile: 'bg-accent-2/15 text-accent-2 shadow-glow-accent-sm ring-accent-2/30', to: '/onboard',
  },
]

export default function WelcomeSurface(_props: SurfaceProps) {
  const [entered, setEntered] = useState(false)
  return (
    <main className="theme-light relative flex min-h-full flex-col items-center justify-center gap-12 overflow-hidden p-6 text-center text-text">
      <div className="relative flex flex-col items-center gap-6 motion-safe:animate-rise">
        <div className="rounded-[2rem] bg-panel p-4 shadow-lift ring-1 ring-line">
          <BrandHat size={76} />
        </div>
        <div className="flex flex-col items-center gap-3">
          <h1 className="max-w-full text-[13vw] font-black leading-none tracking-tight min-[420px]:text-6xl sm:text-7xl">
            RestaurantOS{' '}
            <span className="bg-linear-to-r from-accent to-accent-2 bg-clip-text text-transparent">AI</span>
          </h1>
          <p className="max-w-md text-lg leading-relaxed text-muted">
            The operating system for your restaurant — orders, kitchen and stock, looked after with you.
          </p>
        </div>
      </div>

      {entered ? (
        <section aria-label="How are you using RestaurantOS today?"
          className="relative flex w-full max-w-3xl flex-col gap-5 motion-safe:animate-rise">
          <div className="grid gap-4 sm:grid-cols-3">
            {MODES.map((mode, i) => (
              <button key={mode.to} type="button" onClick={() => navigate(mode.to)}
                style={{ animationDelay: `${i * 90}ms` }}
                className="flex flex-col items-center gap-4 rounded-3xl bg-panel p-7 text-center shadow-lift ring-1 ring-line transition-[transform,box-shadow] duration-300 hover:-translate-y-1 hover:shadow-glow focus-visible:outline-2 focus-visible:outline-accent motion-safe:animate-rise">
                <span className={cx('flex size-14 items-center justify-center rounded-2xl ring-1', mode.tile)}>
                  <mode.icon className="size-7" />
                </span>
                <span className="text-xl font-bold tracking-tight">{mode.title}</span>
                <span className="text-sm leading-relaxed text-muted">{mode.blurb}</span>
              </button>
            ))}
          </div>
          <button type="button" onClick={() => setEntered(false)}
            className="mx-auto inline-flex items-center gap-1.5 rounded-full bg-panel px-4 py-2 text-sm font-medium text-muted shadow-lift ring-1 ring-line transition-colors hover:text-text hover:ring-accent/40 focus-visible:outline-2 focus-visible:outline-accent">
            <ArrowLeft className="size-4" /> Back
          </button>
        </section>
      ) : (
        <div className="relative motion-safe:animate-rise" style={{ animationDelay: '140ms' }}>
          <Button variant="primary" size="xl" className="px-10 shadow-glow-accent" onClick={() => setEntered(true)}>
            Enter <ArrowRight className="size-6" />
          </Button>
        </div>
      )}
    </main>
  )
}
