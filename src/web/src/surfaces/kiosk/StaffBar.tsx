/**
 * The staff exit on the kiosk (pass 8, Scope D — founder-reported): a front-counter claim lands its
 * holder on /kiosk, which is a customer surface — with no way out, a signed-in staff member was
 * trapped holding a staff session. When (and only when) this device holds a staff session, a slim
 * bar at the top names the person and their job for this shift and offers the same way out the
 * manager and KDS surfaces offer: release the claim, clear the session, land on /staff. With no
 * session it renders nothing — customers never see staff chrome.
 */

import { useState } from 'react'
import { staffApi, clearStaffSession, loadStaffSession } from '../../lib/staff'
import { navigate } from '../../router'

export function StaffBar() {
  const [session] = useState(loadStaffSession)
  if (!session) return null
  const endShift = async () => {
    if (session.claim) {
      try {
        await staffApi.release(session.token)
      } catch {
        // the local sign-out below stands even when the server cannot be reached (the KDS pattern)
      }
    }
    clearStaffSession()
    navigate('/staff')
  }
  return (
    <div className="flex shrink-0 items-center gap-2.5 bg-text px-4 py-2 text-sm text-ink sm:px-5"
      data-testid="kiosk-staff-bar">
      <span className="font-bold">{session.staff.name}</span>
      <span className="opacity-75">{session.claim ? session.claim.label : 'Signed in'}</span>
      <button type="button" onClick={() => void endShift()}
        className="ml-auto shrink-0 rounded-full bg-white/15 px-3.5 py-1 font-semibold transition hover:bg-white/25">
        {session.claim ? 'End shift' : 'Sign out'}
      </button>
    </div>
  )
}
