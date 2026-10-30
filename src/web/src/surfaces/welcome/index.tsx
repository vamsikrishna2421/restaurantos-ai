/**
 * `/` (P41): the front door. A splash first — the designed brand mark (the hat logo and the RestaurantOS AI
 * wordmark, large) — behind one Enter button; then the mode choice: Customer (the kiosk), Staff (shift sign-in),
 * or setting up a new restaurant (onboarding). Nothing here creates a session or reads tenant data: this is the
 * cover of the product, and everyone who opens the app lands here first.
 */

import { ArrowLeft, ArrowRight, ChefHat, Store, UtensilsCrossed } from 'lucide-react'
import { useState, type ComponentType } from 'react'
import { BrandHat, Button } from '../../components/ui'
import { navigate, type SurfaceProps } from '../../router'

interface Mode {
  title: string
  blurb: string
  icon: ComponentType<{ className?: string }>
  to: string
}

const MODES: readonly Mode[] = [
  { title: 'Customer', blurb: 'See the menu and place an order — tap, or just say it.', icon: UtensilsCrossed, to: '/kiosk' },
  { title: 'Staff', blurb: 'Sign in with your PIN and take your job for this shift.', icon: ChefHat, to: '/staff' },
  { title: 'Set up a new restaurant', blurb: 'Bring your menu over and open your doors here.', icon: Store, to: '/onboard' },
]

export default function WelcomeSurface(_props: SurfaceProps) {
  const [entered, setEntered] = useState(false)
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-10 p-6 text-center">
      <div className="flex flex-col items-center gap-4">
        <BrandHat size={88} />
        <h1 className="text-5xl font-black tracking-tight sm:text-6xl">
          RestaurantOS <span className="text-accent">AI</span>
        </h1>
        <p className="max-w-md text-muted">
          The operating system for your restaurant — orders, kitchen and stock, looked after with you.
        </p>
      </div>
      {entered ? (
        <section aria-label="How are you using RestaurantOS today?" className="flex w-full max-w-3xl flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-3">
            {MODES.map((mode) => (
              <button key={mode.to} type="button" onClick={() => navigate(mode.to)}
                className="flex flex-col items-center gap-3 rounded-2xl border border-line bg-panel p-6 text-center transition-colors hover:border-accent focus-visible:outline-2 focus-visible:outline-accent">
                <mode.icon className="size-10 text-accent" />
                <span className="text-xl font-bold">{mode.title}</span>
                <span className="text-sm text-muted">{mode.blurb}</span>
              </button>
            ))}
          </div>
          <button type="button" onClick={() => setEntered(false)}
            className="mx-auto inline-flex items-center gap-1 text-sm text-muted underline">
            <ArrowLeft className="size-4" /> Back
          </button>
        </section>
      ) : (
        <Button variant="primary" size="xl" onClick={() => setEntered(true)}>
          Enter <ArrowRight className="size-6" />
        </Button>
      )}
    </main>
  )
}
