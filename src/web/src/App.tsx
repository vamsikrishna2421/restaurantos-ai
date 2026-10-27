import { useEffect, useState } from 'react'

type Health = { status: string; ai_mode?: string } | null

export default function App() {
  const [health, setHealth] = useState<Health>(null)

  useEffect(() => {
    let alive = true
    fetch('/api/health')
      .then((r) => (r.ok ? r.json() : null))
      .then((h: Health) => alive && setHealth(h))
      .catch(() => alive && setHealth(null))
    return () => {
      alive = false
    }
  }, [])

  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <h1 className="text-3xl font-semibold tracking-tight">
        RestaurantOS <span className="text-accent">AI</span>
      </h1>
      <p className="text-muted">The AI General Manager for restaurants.</p>
      <p className="text-sm text-muted" data-testid="backend-status">
        Backend: {health ? `${health.status} (${health.ai_mode ?? 'unknown'})` : 'offline'}
      </p>
    </main>
  )
}
