/**
 * Scope L: a returning kiosk device must not keep serving a stale menu.
 *
 * The mechanism, end to end: a browser's session owns a sandbox cloned from the deployment's
 * template tenant, and the clone's menu is a copy made at clone time. When the server's menu
 * changes (a new seed is materialized — photos added, a price corrected, a dish added), the
 * template, `live` and `demo` tenants are rebuilt in place, but existing sandboxes are not: a
 * device that visited before the change keeps its old sandbox, so its `/api/state` snapshot —
 * and therefore this screen — keeps the old menu forever. A fresh browser profile clones the
 * new template, which is why only returning devices were stuck.
 *
 * The revalidation, run once per page load from the kiosk's session bootstrap: fingerprint the
 * menu this device is being served against the deployment's current menu (the public `live`
 * snapshot, rebuilt from the same seed as the template). The fingerprint covers the seed-owned
 * content only — items (codes, names, prices, categories, stations, photos, modifiers allowed),
 * the modifier book, the stations and the tax rate — never availability, alternatives or the
 * tenant-local `menu_version`, which a session legitimately moves on its own (an 86 bumps the
 * version and must not look like drift). On a mismatch the stale copy is dropped the way the
 * product already drops it: the session is reset, which re-clones the sandbox from the current
 * template, and the fresh snapshot is loaded. Every failure keeps the device exactly as it was —
 * an unreachable server (or a refused reset) means the cached copy keeps serving; staleness only
 * loses to a reachable, newer server state.
 */

import type { MenuSnapshot, TenantState } from '../../contracts/generated'
import { api } from '../../lib/api'
import { useSession, useTenant } from '../../stores'

/** How long the bootstrap waits for the feed's first snapshot before giving up on revalidating. */
const SNAPSHOT_WAIT_MS = 8_000

/** A stable fingerprint of a menu's seed-owned content (see the module note for what is excluded). */
export function menuFingerprint(menu: MenuSnapshot): string {
  return JSON.stringify({
    tax_bp: menu.tax_bp,
    items: menu.items.map((i) => [i.code, i.names, i.price_cents, i.category, i.station_id, i.is_veg,
      i.spice_level, i.aliases, i.allowed_modifiers, i.prep_min, i.photo]),
    modifiers: menu.modifiers.map((m) => [m.code, m.labels, m.aliases]),
    stations: menu.stations.map((s) => [s.id, s.names, s.parallel_slots]),
  })
}

let attempted = false

/** Tests: forget that this page load already revalidated. */
export function resetMenuSyncForTests(): void {
  attempted = false
}

/** The store's current snapshot, waiting briefly for the feed's first load if it is still on its way. */
function currentData(): Promise<ReturnType<typeof useTenant.getState>['data']> {
  const data = useTenant.getState().data
  if (data !== null) return Promise.resolve(data)
  return new Promise((resolve) => {
    const timer = globalThis.setTimeout(() => {
      unsub()
      resolve(useTenant.getState().data)
    }, SNAPSHOT_WAIT_MS)
    const unsub = useTenant.subscribe((s) => {
      if (s.data !== null) {
        globalThis.clearTimeout(timer)
        unsub()
        resolve(s.data)
      }
    })
  })
}

/**
 * Drop this device's sandbox when the server's menu has moved past it (see the module note).
 * Runs at most once per page load; never throws — every failure path keeps the current menu.
 */
export async function refreshStaleSandboxMenu(): Promise<void> {
  if (attempted) return
  const data = await currentData()
  if (data === null) return // no snapshot this load: nothing cached to compare, nothing to drop
  attempted = true
  let canonical: MenuSnapshot
  try {
    canonical = (await api<TenantState>('GET', '/api/live/state')).menu
  } catch {
    return // the server cannot be reached: the copy on the device keeps serving
  }
  if (canonical.tenant_id === data.menu.tenant_id) return // this device IS the canonical tenant
  if (menuFingerprint(canonical) === menuFingerprint(data.menu)) return // the copy is current
  try {
    await useSession.getState().reset() // re-clone the sandbox from the current template
  } catch {
    return // refused (the reset limit, a network drop): keep the old sandbox; a later visit retries
  }
  try {
    useTenant.getState().load(await api<TenantState>('GET', '/api/state'))
  } catch {
    // the feed resyncs on the sandbox.reset envelope and lands the same snapshot; nothing to do
  }
}
