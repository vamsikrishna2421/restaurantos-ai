/**
 * AdminLogin on `/?admin=1` (CONTRACTS_API §1 and #40; MASTER_PLAN B25): a founder's admin token in, the `ros_admin`
 * cookie out (the page never sees it). The token field is a password field that is never stored, logged or kept after
 * the request. `/record` sends a refused admin here.
 */

import { KeyRound, ShieldCheck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { SampleBadge } from '../../components/badges'
import { Button, Card } from '../../components/ui'
import { isApiError } from '../../lib/api'
import { Link } from '../../router'
import { errorText, judgeApi, retryIn } from './api'

export function AdminLogin() {
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (token.trim() === '' || busy) return
    setBusy(true)
    setError(null)
    try {
      await judgeApi.adminLogin(token.trim())
      setDone(true)
    } catch (err) {
      setError(isApiError(err, 'forbidden_role') ? 'That admin token is not valid.'
        : isApiError(err, 'rate_limited') ? `Too many attempts.${retryIn(err)}` : errorText(err))
    } finally {
      setToken('')
      setBusy(false)
    }
  }
  return (
    <main className="mx-auto flex min-h-full w-full max-w-md flex-col justify-center gap-4 p-4">
      <SampleBadge className="self-start" />
      <Card title={<span className="inline-flex items-center gap-2"><KeyRound aria-hidden className="size-5" />
        Admin sign-in</span>}>
        {done ? (
          <div role="status" className="flex flex-col gap-3">
            <p className="flex items-center gap-2 font-semibold text-ok">
              <ShieldCheck aria-hidden className="size-5" />Signed in as an admin on this browser.
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              <Link to="/record" className="text-accent underline">Recorder</Link>
              <Link to="/monitor" className="text-accent underline">Call monitor</Link>
              <Link to="/" className="text-accent underline">Action page</Link>
            </div>
          </div>
        ) : (
          <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-3" aria-label="Admin sign-in">
            <label htmlFor="admin-token" className="text-sm font-medium">Admin token</label>
            <input id="admin-token" type="password" autoComplete="off" spellCheck={false} value={token}
              onChange={(e) => setToken(e.target.value)}
              className="rounded-lg border border-line bg-ink px-3 py-2 text-text focus:border-accent focus:outline-none" />
            <Button type="submit" variant="primary" loading={busy} disabled={token.trim() === ''}>Sign in</Button>
            {error && <p role="alert" className="text-sm text-bad">{error}</p>}
            <Link to="/" className="text-sm text-muted underline">Back to the action page</Link>
          </form>
        )}
      </Card>
    </main>
  )
}
