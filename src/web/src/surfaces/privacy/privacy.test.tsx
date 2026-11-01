// @vitest-environment jsdom
// /privacy is static: the retention per store (ARCHITECTURE_OPS §11 and §13, as the code enforces it) and the
// processors.
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import PrivacySurface from '.'

const fetchSpy = vi.fn()

beforeEach(() => {
  fetchSpy.mockReset()
  vi.stubGlobal('fetch', fetchSpy)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** Each body row as [store, kept for]. */
function rows(name: string): [string, string][] {
  const table = screen.getByRole('table', { name })
  return within(table).getAllByRole('row').slice(1).map((tr) => {
    const cells = tr.querySelectorAll('th, td')
    return [cells[0].textContent ?? '', cells[2].textContent ?? '']
  })
}

test('privacy_retention_table', () => {
  render(<PrivacySurface params={{}} query={new URLSearchParams()} />)
  expect(screen.getByRole('heading', { name: 'Privacy' })).toBeInTheDocument()
  expect(screen.getByTestId('sample-badge')).toBeInTheDocument()
  const kept = new Map(rows('Retention by store'))
  expect([...kept.keys()]).toEqual(['Transcripts', 'Events', 'Sandbox kitchens', 'The public live kitchen',
    'Nightly backups', 'Host volume snapshots', 'Off-host backups', 'Logs', 'Call recordings', 'Consented clips',
    'Call records', 'Phone numbers', 'Ingredient lists', 'Shelf photos', 'Your browser'])
  // backups keep the live and demo kitchens' transcripts past the database's 7 days
  expect(kept.get('Transcripts')).toBe('7 days in the database (24 hours in a sandbox kitchen), deleted at the next ' +
    '04:00 ET cleanup; for the live and demo kitchens, also in up to 3 nightly backup copies and, on Fly.io, in the ' +
    'off-host backups')
  expect(kept.get('Events')).toBe('Call and turn events: 7 days. The rest: until their kitchen is reset or deleted')
  expect(kept.get('Sandbox kitchens')).toBe('Deleted 24 hours after last use')
  expect(kept.get('The public live kitchen')).toBe('Reset every day from 04:00 ET, once no call or rush is running')
  expect(kept.get('Nightly backups')).toBe('The newest 3 copies')
  expect(kept.get('Host volume snapshots')).toBe('Fly.io\'s default snapshot retention')
  // no deletion yet where the code has none: the off-host copy, consented clip files, live and demo call records
  expect(kept.get('Off-host backups')).toBe('Kept by the founders; no deletion period is set yet')
  expect(kept.get('Logs')).toBe('7 days')
  expect(kept.get('Call recordings')).toBe('30 days (older ones are deleted when the next consented call is recorded)')
  expect(kept.get('Consented clips')).toBe('Until the founders delete them; removing a consent stops the clip being ' +
    'served at once (the file itself is not deleted automatically)')
  expect(kept.get('Call records')).toBe('Sandbox: deleted with the kitchen. Live and demo kitchens: kept (a daily ' +
    'reset does not delete them)')
  expect(kept.get('Phone numbers')).toBe('With the call record')
  // pass 16: ingredient lists hold quantities only — never how a dish is made
  expect(kept.get('Ingredient lists')).toBe('Kept with the kitchen until it is reset or deleted')
  const retentionHolds = screen.getByRole('table', { name: 'Retention by store' })
  expect(retentionHolds).toHaveTextContent('Never preparation steps, method or technique')
  expect(screen.getByText(/Your recipes stay yours: we draft ingredient lists from general knowledge/))
    .toBeInTheDocument()
  expect(screen.getByText(/Your cooking process — how a dish is prepared/)).toBeInTheDocument()
  // shelf photos (P38) are never stored: only their hash stays, with the stock change a person confirmed
  expect(kept.get('Shelf photos')).toBe('Never stored: processed in memory and sent to Meta (Muse Spark and SAM ' +
    '3.1); only its SHA-256 hash is kept, with the stock change a person confirmed')
  expect(kept.get('Your browser')).toBe('The cookie: 24 hours after your last visit. The tab\'s own storage: until ' +
    'the tab closes')
  // what a store holds: events and logs never carry what a guest said, numbers are hashed
  const retention = screen.getByRole('table', { name: 'Retention by store' })
  expect(retention).toHaveTextContent('never what a guest said, never a phone number')
  expect(retention).toHaveTextContent('no transcripts, phone numbers or Twilio request bodies')
  expect(retention).toHaveTextContent('a keyed hash (HMAC) and the last 4 digits instead')
  expect(retention).toHaveTextContent('When a call started and ended, its lane and outcome, the caller hash and last ' +
    '4 digits')
  expect(retention).toHaveTextContent('In this tab only: a judge passcode until Start, the kiosk\'s session and cart ' +
    '(to resume within 15 minutes), an approval\'s result and the video-mode flag')
  // at phone width the columns wrap (no minimum width); the wrapper is a named region a keyboard can scroll
  expect(retention.className).not.toMatch(/min-w/)
  expect(screen.getByRole('region', { name: 'Retention by store' })).toHaveAttribute('tabindex', '0')

  const processors = new Map(rows('Processors'))
  expect([...processors.keys()]).toEqual(['Meta (Muse models)', 'ElevenLabs', 'Twilio', 'Hosting', 'Google Fonts',
    'Open-weight model provider'])
  expect(processors.get('Meta (Muse models)')).toMatch(/^Standard models with storage off \(store: false\)/)
  expect(processors.get('Hosting')).toMatch(/Cloudflare quick tunnel/)
  expect(screen.getByRole('table', { name: 'Processors' })).toHaveTextContent('Open-weight model providerNot enabled')
  // "In short" agrees with the table
  expect(screen.getByText(/The phone greeting says you are talking to an AI, that the call is transcribed/))
    .toBeInTheDocument()
  expect(screen.getByText('No recording of your voice is kept, except the founders\' own consented test calls and ' +
    'consented clips from the founders and friends.')).toBeInTheDocument()
  expect(screen.getByText('What you say is kept as text for 7 days in the database (24 hours in a sandbox kitchen); ' +
    'backups of the live and demo kitchens keep it longer (see below). Events and logs never contain it.'))
    .toBeInTheDocument()
  expect(screen.getByText(/contributor-tier model only writes synthetic test sentences/)).toBeInTheDocument()
  expect(fetchSpy).not.toHaveBeenCalled() // a static page
})
