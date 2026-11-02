/**
 * The Settings section (P42): the active tenant's profile, menu and stations as onboarding set them up —
 * read-only, because those are config changes (made through onboarding and applied on activation), not
 * live edits. The one live edit here is stock counts, which the inventory endpoints already own
 * (`GET /api/inventory`, `POST /api/inventory/adjust`): counting the shelf is a manager's daily act, so
 * it lives where the manager works.
 */

import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Chip } from '../../components/ui'
import type {
  ConsoleSettings,
  ExternalOrderView,
  IngestionTokenCreated,
  IngestionTokenView,
  InventoryItemView,
} from '../../contracts/generated'
import { api } from '../../lib/api'
import { formatCents } from '../../lib/format'
import { managerApi } from '../../lib/manager'
import { SectionState } from './shared'

function StockEditor({ items, onSaved }: {
  items: InventoryItemView[]
  onSaved: (view: InventoryItemView) => void
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  const save = async (item: InventoryItemView) => {
    const raw = drafts[item.ingredient.id]
    const toQty = raw === undefined ? NaN : Number(raw)
    if (!Number.isInteger(toQty) || toQty < 0) {
      setFailed(item.ingredient.id)
      return
    }
    setBusy(item.ingredient.id)
    setFailed(null)
    try {
      const view = await api<InventoryItemView>('POST', '/api/inventory/adjust', {
        ingredient_id: item.ingredient.id, to_qty: toQty, reason: 'Counted from the manager console',
      })
      onSaved(view)
      setDrafts((d) => {
        const next = { ...d }
        delete next[item.ingredient.id]
        return next
      })
    } catch {
      setFailed(item.ingredient.id)
    } finally {
      setBusy(null)
    }
  }

  return (
    <ul className="flex flex-col gap-1.5">
      {items.map((item) => {
        const { ingredient, forecast } = item
        const draft = drafts[ingredient.id]
        const dirty = draft !== undefined && draft !== String(ingredient.on_hand)
        return (
          <li key={ingredient.id}
            className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
            <span className="flex min-w-0 items-center gap-2.5">
              <span className={`size-2 shrink-0 rounded-full ${forecast.band === 'act'
                ? 'bg-bad shadow-glow-bad-sm'
                : forecast.band === 'watch' ? 'bg-warn shadow-glow-warn-sm' : 'bg-ok'}`} />
              <span className="truncate">
                <span className="font-semibold">{ingredient.names.en}</span>
                <span className="block text-sm text-muted">
                  {ingredient.reserved} {ingredient.unit} reserved · par {ingredient.par} {ingredient.unit}
                </span>
              </span>
            </span>
            <span className="flex items-center gap-2">
              <label className="flex items-center gap-2 text-sm text-muted">
                On hand
                <input type="number" min={0} inputMode="numeric" aria-label={`On hand for ${ingredient.names.en}`}
                  value={draft ?? String(ingredient.on_hand)}
                  onChange={(e) => setDrafts((d) => ({ ...d, [ingredient.id]: e.target.value }))}
                  className="w-24 rounded-lg border border-white/10 bg-panel-2 px-2.5 py-1.5 font-medium tabular-nums text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15" />
                <span className="text-muted">{ingredient.unit}</span>
              </label>
              <Button size="sm" variant="primary" disabled={!dirty} loading={busy === ingredient.id}
                onClick={() => void save(item)}>
                Save count
              </Button>
            </span>
            {failed === ingredient.id && (
              <p role="alert" className="w-full text-sm text-bad">
                That count could not be saved — check it is a whole number, then try again.
              </p>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/**
 * Outside orders (pass 18): the restaurant's ingestion token and what arrived through it.
 * The token is the door for orders placed elsewhere (a POS, an online ordering page): the outside
 * system sends them to POST /api/ingest/orders with this token, and they join the same kitchen
 * queue as every other order. The value is shown once, when created — only its hash is kept — so
 * rotating is also how a lost token is replaced. Fails soft: on an older server the card hides.
 */
function IngestionCard() {
  const [status, setStatus] = useState<IngestionTokenView | null>(null)
  const [arrivals, setArrivals] = useState<ExternalOrderView[] | null>(null)
  const [created, setCreated] = useState<IngestionTokenCreated | null>(null)
  const [busy, setBusy] = useState(false)
  const [hidden, setHidden] = useState(false)

  const load = useCallback(async () => {
    try {
      const [token, recent] = await Promise.all([
        api<IngestionTokenView>('GET', '/api/ingest/token'),
        api<ExternalOrderView[]>('GET', '/api/ingest/orders'),
      ])
      setStatus(token)
      setArrivals(recent)
    } catch {
      setHidden(true)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (hidden || status === null || arrivals === null) return null

  const rotate = async () => {
    setBusy(true)
    try {
      const fresh = await api<IngestionTokenCreated>('POST', '/api/ingest/token/rotate')
      setCreated(fresh)
      setStatus({ configured: true, created_at: fresh.created_at })
    } catch {
      setHidden(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Outside orders" className="motion-safe:animate-rise">
      <p className="text-sm text-muted">
        Orders placed outside this app — a point-of-sale system, an online ordering page — can be
        sent here and join the same kitchen queue, stock counts and reports as every other order.
        The outside system posts them to <span className="font-mono text-text">POST /api/ingest/orders</span> with
        this restaurant’s ingestion token. Nothing is ever sent back the other way.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {status.configured
          ? <Chip tone="ok">Token set up</Chip>
          : <Chip tone="warn">No token yet</Chip>}
        <Button size="sm" variant="primary" loading={busy} onClick={() => void rotate()}>
          {status.configured ? 'Rotate token' : 'Create token'}
        </Button>
        {status.configured && (
          <span className="text-sm text-muted">Rotating replaces the old token at once.</span>
        )}
      </div>
      {created && (
        <div role="status" className="mt-3 rounded-xl bg-white/2 px-3 py-2.5 ring-1 ring-white/5">
          <p className="text-sm font-semibold">Your new ingestion token — shown only this once:</p>
          <p className="mt-1 break-all font-mono text-sm text-text">{created.token}</p>
          <p className="mt-1 text-sm text-muted">
            Paste it into the outside system now. If it is lost, rotate again — the old one stops working.
          </p>
        </div>
      )}
      {arrivals.length > 0 && (
        <ul className="mt-4 flex flex-col gap-1.5">
          {arrivals.slice(0, 8).map((order) => (
            <li key={order.order_id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
              <span className="min-w-0">
                <span className="font-medium">
                  #{order.order_no} · {order.source}{order.channel ? ` · ${order.channel}` : ''}
                </span>
                <span className="block truncate text-sm text-muted">
                  Their order {order.external_order_id}
                  {order.customer_name ? ` · ${order.customer_name}` : ''}
                  {order.customer_phone_masked ? ` · ${order.customer_phone_masked}` : ''}
                </span>
                {order.unmapped.length > 0 && (
                  <span className="mt-1 flex flex-wrap gap-1.5">
                    {order.unmapped.map((line) => (
                      <Chip key={line.label} tone="warn">
                        {line.label} — {line.reason === 'unavailable' ? '86’d that day' : 'not on your menu'}
                      </Chip>
                    ))}
                  </span>
                )}
              </span>
              <span className="shrink-0 font-semibold tabular-nums">{formatCents(order.total_cents)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

export default function SettingsSection() {
  const [settings, setSettings] = useState<ConsoleSettings | null>(null)
  const [stock, setStock] = useState<InventoryItemView[] | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    try {
      const [profile, items] = await Promise.all([
        managerApi.settings(),
        api<InventoryItemView[]>('GET', '/api/inventory'),
      ])
      setSettings(profile)
      setStock(items)
      setError(false)
    } catch {
      setError(true)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const loading = (settings === null || stock === null) && !error

  return (
    <SectionState loading={loading} error={error}>
      {settings && stock && (
        <div className="flex flex-col gap-6">
          <Card title="Restaurant" className="motion-safe:animate-rise">
            <dl className="grid gap-x-8 gap-y-2 sm:grid-cols-2">
              <div className="flex justify-between gap-3"><dt className="text-muted">Name</dt><dd className="font-medium">{settings.name}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">Hours</dt><dd className="font-medium tabular-nums">{settings.open_local} – {settings.close_local}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">Timezone</dt><dd className="font-medium">{settings.timezone}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">Tax</dt><dd className="font-medium tabular-nums">{(settings.tax_bp / 100).toFixed(2)}%</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">Address</dt><dd className="font-medium">{settings.address || '—'}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted">Phone</dt><dd className="font-medium">{settings.phone_display ?? '—'}</dd></div>
            </dl>
            <p className="mt-3 text-sm text-muted">
              The profile, menu and stations below are this restaurant’s setup, exactly as onboarding
              created it. Changing them is a setup change — it goes through onboarding and takes effect
              when the new setup is activated; it is not edited live here.
            </p>
          </Card>

          <Card title="Stations" className="motion-safe:animate-rise">
            <ul className="flex flex-col gap-1.5">
              {settings.stations.map((station) => (
                <li key={station.id}
                  className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                  <span className="font-medium">{station.name}</span>
                  <span className="text-sm text-muted">
                    {station.parallel_slots} cook{station.parallel_slots === 1 ? '' : 's'} at a time
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-sm text-muted">
              Planning assumption used by Analytics: one cook finishes about {settings.tickets_per_cook_hour} tickets
              an hour.
            </p>
          </Card>

          <Card title="Menu" className="motion-safe:animate-rise">
            <ul className="flex flex-col gap-1.5">
              {settings.menu.map((item) => (
                <li key={item.code}
                  className="flex items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                  <span className="min-w-0 truncate">
                    <span className="font-medium">{item.name}</span>
                    <span className="block text-sm text-muted">{item.category} · {item.station_name}{item.is_veg ? ' · veg' : ''}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <span className="font-semibold tabular-nums">{formatCents(item.price_cents)}</span>
                    {item.available ? <Chip tone="ok">On the menu</Chip> : <Chip tone="bad">86’d</Chip>}
                  </span>
                </li>
              ))}
            </ul>
          </Card>

          <Card title="Stock counts" className="motion-safe:animate-rise">
            <p className="mb-3 text-sm text-muted">
              Counted the shelf? Put the real number in — forecasts and the stock watch use it at once.
            </p>
            <StockEditor items={stock} onSaved={(view) => setStock((items) =>
              (items ?? []).map((item) => (item.ingredient.id === view.ingredient.id ? view : item)))} />
          </Card>

          <IngestionCard />
        </div>
      )}
    </SectionState>
  )
}
