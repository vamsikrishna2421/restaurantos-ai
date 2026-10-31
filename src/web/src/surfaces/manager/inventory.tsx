/**
 * The Inventory section (pass 13): the stock workflow the founder specified, next to the counts the
 * Settings editor already keeps.
 *
 * - **Opening stock**: until the restaurant enters its real starting counts, the stock levels are
 *   the catalog's standard pars; the entry grid writes them as auditable `initial` adjustments.
 * - **Tonight's closing check**: the ingredients the forecast says will run low or out before
 *   tomorrow's service ends, cross-checked before closing. Counted differences apply as counts;
 *   completing records who finished the check and when.
 * - **Scan a purchase bill**: a photo or PDF of a supplier bill becomes a draft stock-in — lines
 *   matched to the catalog or created new — and confirming files it as a supplier receipt.
 *
 * Everything here rides the inventory API (`lib/inventory`); the server admits the manager, or the
 * person holding the Inventory Manager responsibility.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card, Chip } from '../../components/ui'
import type {
  BillConfirmLine,
  BillDraft,
  ClosingCheckView,
  InventoryItemView,
  InventorySetupView,
  SupplierReceipt,
} from '../../contracts/generated'
import { formatCents, formatNumber } from '../../lib/format'
import { inventoryApi } from '../../lib/inventory'
import { dayLabel, SectionState } from './shared'
import { filedWhen } from './support'

const INPUT = 'rounded-lg border border-white/10 bg-panel-2 px-2.5 py-1.5 font-medium tabular-nums ' +
  'text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15'
const BAND_TONE = { ok: 'ok', watch: 'warn', act: 'bad' } as const

/** "7:20 PM" from an ISO business datetime, in the tenant's own offset (never re-zoned). */
function bizTime(iso: string): string {
  const match = /T(\d{2}):(\d{2})/.exec(iso)
  if (!match) return iso
  const hour = Number(match[1])
  const suffix = hour >= 12 ? 'PM' : 'AM'
  return `${hour % 12 === 0 ? 12 : hour % 12}:${match[2]} ${suffix}`
}

function etaText(item: ClosingCheckView['items'][number]): string {
  const { eta_min, band } = item.forecast
  if (eta_min !== null) {
    return eta_min >= 60 ? `runs out in about ${Math.round(eta_min / 60)} h` : `runs out in about ${eta_min} min`
  }
  return band === 'ok' ? 'stock is fine' : 'running low'
}

// ----- opening stock ------------------------------------------------------------------------------------------------

function OpeningStock({ setup, onSaved }: {
  setup: InventorySetupView
  onSaved: (view: InventorySetupView) => void
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [open, setOpen] = useState(!setup.completed)
  const [saving, setSaving] = useState(false)
  const [failed, setFailed] = useState(false)

  const save = async () => {
    const counts: Record<string, number> = {}
    for (const { ingredient } of setup.items) {
      const raw = drafts[ingredient.id]
      const value = raw === undefined ? ingredient.par : Number(raw)
      if (!Number.isInteger(value) || value < 0) {
        setFailed(true)
        return
      }
      counts[ingredient.id] = value
    }
    setSaving(true)
    setFailed(false)
    try {
      onSaved(await inventoryApi.saveSetup(counts))
      setDrafts({})
      setOpen(false)
    } catch {
      setFailed(true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card title="Opening stock" tone={setup.completed ? undefined : 'warn'}
      className="motion-safe:animate-rise"
      actions={setup.completed ? <Chip tone="ok">Entered</Chip> : <Chip tone="warn">Setup step</Chip>}>
      {setup.completed && !open ? (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-muted">
            The opening counts are in — forecasts and the closing check work from them.
          </p>
          <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>Review or re-enter counts</Button>
        </div>
      ) : (
        <>
          {!setup.completed && (
            <p className="mb-3 text-sm text-muted">
              Enter the stock you actually have on the shelves. Until this is saved, the levels are
              the catalog’s standard pars — the forecasts get honest the moment the real counts land.
            </p>
          )}
          <ul className="flex flex-col gap-1.5">
            {setup.items.map(({ ingredient }) => (
              <li key={ingredient.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-semibold">{ingredient.names.en}</span>
                  <span className="block text-sm text-muted">
                    standard par {formatNumber(ingredient.par)} {ingredient.unit}
                    {ingredient.pack_size > 1 ? ` · packs of ${formatNumber(ingredient.pack_size)}` : ''}
                  </span>
                </span>
                <label className="flex shrink-0 items-center gap-2 text-sm text-muted">
                  Opening count
                  <input type="number" min={0} inputMode="numeric"
                    aria-label={`Opening count for ${ingredient.names.en}`}
                    value={drafts[ingredient.id] ?? String(ingredient.par)}
                    onChange={(e) => setDrafts((d) => ({ ...d, [ingredient.id]: e.target.value }))}
                    className={`w-28 ${INPUT}`} />
                  <span>{ingredient.unit}</span>
                </label>
              </li>
            ))}
          </ul>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button variant="primary" loading={saving} onClick={() => void save()}>
              Save opening stock
            </Button>
            {failed && (
              <p role="alert" className="text-sm text-bad">
                Those counts could not be saved — check each one is a whole number, then try again.
              </p>
            )}
          </div>
        </>
      )}
    </Card>
  )
}

// ----- the closing check ----------------------------------------------------------------------------------------------

function ClosingCheck({ check, onDone }: {
  check: ClosingCheckView
  onDone: (view: ClosingCheckView) => void
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const done = check.completion

  const complete = async () => {
    const counts: Record<string, number> = {}
    for (const item of check.items) {
      const raw = drafts[item.ingredient.id]
      if (raw === undefined || raw.trim() === '') continue // left out = matches the books
      const value = Number(raw)
      if (!Number.isInteger(value) || value < 0) {
        setFailed(true)
        return
      }
      if (value !== item.ingredient.on_hand) counts[item.ingredient.id] = value
    }
    setBusy(true)
    setFailed(false)
    try {
      onDone(await inventoryApi.completeClosingCheck(counts))
      setDrafts({})
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Tonight’s closing check" className="motion-safe:animate-rise"
      actions={done ? <Chip tone="ok">Done</Chip> : check.items.length > 0
        ? <Chip tone="warn">{check.items.length} to check</Chip> : <Chip tone="ok">All matching</Chip>}>
      <p className="mb-3 text-sm text-muted">
        {dayLabel(check.biz_date)} — the ingredients the forecast says will run low or out before
        tomorrow’s service ends ({bizTime(check.service_ends_at)}). Count them on the shelf before
        closing; anything you leave blank counts as matching the books.
      </p>
      {done ? (
        <div className="flex flex-col gap-2">
          <p className="font-medium text-ok">
            Completed by {done.completed_by_name} · {filedWhen(done.completed_at)}
          </p>
          {done.variances.length === 0 ? (
            <p className="text-sm text-muted">Everything matched the books.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {done.variances.map((v) => (
                <li key={v.ingredient_id} className="rounded-xl bg-white/2 px-3 py-2 text-sm ring-1 ring-white/5">
                  <span className="font-semibold">{v.ingredient_id}</span>
                  {' — books said '}{formatNumber(v.expected_qty)}{', shelf had '}{formatNumber(v.counted_qty)}
                  {' — the count is the stock now.'}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <>
          {check.items.length === 0 ? (
            <p className="text-muted">Nothing is forecast to run low before tomorrow’s close.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {check.items.map((item) => (
                <li key={item.ingredient.id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold">{item.ingredient.names.en}</span>
                      <Chip tone={BAND_TONE[item.forecast.band]}>{item.forecast.band}</Chip>
                    </span>
                    <span className="block text-sm text-muted">
                      books say {formatNumber(item.ingredient.on_hand)} {item.ingredient.unit} on hand ·{' '}
                      {etaText(item)} · about {formatNumber(item.projected_free)} {item.ingredient.unit} left
                      at tomorrow’s close
                    </span>
                  </span>
                  <label className="flex shrink-0 items-center gap-2 text-sm text-muted">
                    Counted
                    <input type="number" min={0} inputMode="numeric"
                      aria-label={`Counted quantity for ${item.ingredient.names.en}`}
                      placeholder={String(item.ingredient.on_hand)}
                      value={drafts[item.ingredient.id] ?? ''}
                      onChange={(e) => setDrafts((d) => ({ ...d, [item.ingredient.id]: e.target.value }))}
                      className={`w-24 ${INPUT}`} />
                    <span>{item.ingredient.unit}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <Button variant="primary" loading={busy} onClick={() => void complete()}>
              Complete closing check
            </Button>
            {failed && (
              <p role="alert" className="text-sm text-bad">
                The check could not be completed — check the counts are whole numbers, then try again.
              </p>
            )}
          </div>
        </>
      )}
    </Card>
  )
}

// ----- the bill scan --------------------------------------------------------------------------------------------------

interface LineState {
  target: string // an ingredient id, or 'new'
  stockQty: string
  name: string
  unit: string
  packSize: string
  par: string
  reorderPoint: string
}

function BillScan({ stock, onReceived }: {
  stock: InventoryItemView[]
  onReceived: (receipt: SupplierReceipt) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const [draft, setDraft] = useState<BillDraft | null>(null)
  const [lines, setLines] = useState<Record<string, LineState>>({})
  const [scanning, setScanning] = useState(false)
  const [scanFailed, setScanFailed] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [confirmFailed, setConfirmFailed] = useState(false)
  const [received, setReceived] = useState<SupplierReceipt | null>(null)

  const scan = async (file: File) => {
    setScanning(true)
    setScanFailed(null)
    setReceived(null)
    try {
      const result = await inventoryApi.scanBill(file)
      setDraft(result)
      setLines(Object.fromEntries(result.lines.map((line) => [line.line_id, {
        target: line.matched_ingredient_id ?? 'new',
        stockQty: String(line.suggested_stock_qty),
        name: line.raw_name,
        unit: line.unit === 'ea' || line.unit === 'g' || line.unit === 'ml' ? line.unit : 'ea',
        packSize: '1',
        par: '',
        reorderPoint: '',
      }])))
    } catch {
      setScanFailed('That bill could not be read. Try a clearer photo, or a text-based PDF.')
    } finally {
      setScanning(false)
    }
  }

  const confirm = async () => {
    if (!draft) return
    const body: BillConfirmLine[] = []
    for (const line of draft.lines) {
      const state = lines[line.line_id]
      if (!state) continue
      const stockQty = Number(state.stockQty)
      if (!Number.isInteger(stockQty) || stockQty < 0) {
        setConfirmFailed(true)
        return
      }
      body.push(state.target === 'new'
        ? {
          line_id: line.line_id, stock_qty: stockQty, ingredient_id: null,
          create: {
            name: state.name.trim(), unit: state.unit as 'ea' | 'g' | 'ml' | 'portion',
            pack_size: Math.max(1, Number(state.packSize) || 1),
            par: Math.max(0, Number(state.par) || 0), reorder_point: Math.max(0, Number(state.reorderPoint) || 0),
          },
        }
        : { line_id: line.line_id, ingredient_id: state.target, stock_qty: stockQty, create: null })
    }
    setConfirming(true)
    setConfirmFailed(false)
    try {
      const receipt = await inventoryApi.confirmBill(draft.id, body)
      setReceived(receipt)
      setDraft(null)
      onReceived(receipt)
    } catch {
      setConfirmFailed(true)
    } finally {
      setConfirming(false)
    }
  }

  const setLine = (lineId: string, patch: Partial<LineState>) =>
    setLines((prev) => ({ ...prev, [lineId]: { ...prev[lineId], ...patch } }))

  return (
    <Card title="Scan a purchase bill" className="motion-safe:animate-rise">
      <p className="mb-3 text-sm text-muted">
        Photograph a supplier bill (or upload its PDF): the lines are read into a draft stock-in,
        matched to your ingredients or added as new ones. Nothing is received until you confirm.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" loading={scanning} onClick={() => input.current?.click()}>
          Scan a bill
        </Button>
        <input ref={input} type="file" accept="image/*,application/pdf" aria-label="Purchase bill"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            e.target.value = ''
            if (file) void scan(file)
          }} />
        {scanFailed && <p role="alert" className="text-sm text-bad">{scanFailed}</p>}
        {received && (
          <p className="text-sm text-ok">
            Received from {received.supplier_name ?? 'the bill'} — {received.lines.length}{' '}
            {received.lines.length === 1 ? 'line' : 'lines'} into stock.
          </p>
        )}
      </div>

      {draft && (
        <div className="mt-4 flex flex-col gap-2">
          <p className="flex flex-wrap items-center gap-2 font-medium">
            {draft.supplier_name ?? 'Supplier not shown on the bill'}
            {draft.simulated && <Chip tone="warn">Simulated scan — offline mode</Chip>}
          </p>
          <ul className="flex flex-col gap-1.5">
            {draft.lines.map((line) => {
              const state = lines[line.line_id]
              if (!state) return null
              return (
                <li key={line.line_id}
                  className="flex flex-col gap-2 rounded-xl bg-white/2 px-3 py-2.5 ring-1 ring-white/5">
                  <span className="flex flex-wrap items-center justify-between gap-3">
                    <span className="min-w-0">
                      <span className="font-semibold">{line.raw_name}</span>
                      <span className="block text-sm text-muted">
                        bill says {line.qty}{line.unit ? ` ${line.unit}` : ''}
                        {line.price_cents !== null ? ` · ${formatCents(line.price_cents)}` : ''}
                        {line.matched_ingredient_id ? ' · matched' : ' · not in your catalog yet'}
                      </span>
                    </span>
                    <span className="flex flex-wrap items-center gap-2">
                      <select aria-label={`Ingredient for ${line.raw_name}`} value={state.target}
                        onChange={(e) => setLine(line.line_id, { target: e.target.value })}
                        className="rounded-lg border border-white/10 bg-panel-2 px-2.5 py-2 font-medium text-text outline-none focus:border-accent/60 focus:ring-4 focus:ring-accent/15">
                        {stock.map(({ ingredient }) => (
                          <option key={ingredient.id} value={ingredient.id}>{ingredient.names.en}</option>
                        ))}
                        <option value="new">New ingredient…</option>
                      </select>
                      <label className="flex items-center gap-2 text-sm text-muted">
                        Stock in
                        <input type="number" min={0} inputMode="numeric"
                          aria-label={`Stock quantity for ${line.raw_name}`} value={state.stockQty}
                          onChange={(e) => setLine(line.line_id, { stockQty: e.target.value })}
                          className={`w-24 ${INPUT}`} />
                      </label>
                    </span>
                  </span>
                  {state.target === 'new' && (
                    <span className="flex flex-wrap items-center gap-2 text-sm text-muted">
                      <label className="flex items-center gap-1.5">
                        Name
                        <input type="text" value={state.name} aria-label="New ingredient name"
                          onChange={(e) => setLine(line.line_id, { name: e.target.value })}
                          className={INPUT} />
                      </label>
                      <label className="flex items-center gap-1.5">
                        Unit
                        <select value={state.unit} aria-label="New ingredient unit"
                          onChange={(e) => setLine(line.line_id, { unit: e.target.value })}
                          className="rounded-lg border border-white/10 bg-panel-2 px-2 py-1.5 font-medium text-text outline-none focus:border-accent/60">
                          <option value="ea">each</option>
                          <option value="g">grams</option>
                          <option value="ml">ml</option>
                          <option value="portion">portion</option>
                        </select>
                      </label>
                      <label className="flex items-center gap-1.5">
                        Pack size
                        <input type="number" min={1} value={state.packSize} aria-label="Pack size"
                          onChange={(e) => setLine(line.line_id, { packSize: e.target.value })}
                          className={`w-20 ${INPUT}`} />
                      </label>
                      <label className="flex items-center gap-1.5">
                        Par
                        <input type="number" min={0} value={state.par} aria-label="Par level"
                          onChange={(e) => setLine(line.line_id, { par: e.target.value })}
                          className={`w-20 ${INPUT}`} />
                      </label>
                      <label className="flex items-center gap-1.5">
                        Reorder at
                        <input type="number" min={0} value={state.reorderPoint} aria-label="Reorder point"
                          onChange={(e) => setLine(line.line_id, { reorderPoint: e.target.value })}
                          className={`w-20 ${INPUT}`} />
                      </label>
                    </span>
                  )}
                </li>
              )
            })}
          </ul>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" loading={confirming} onClick={() => void confirm()}>
              Receive into stock
            </Button>
            <Button variant="ghost" onClick={() => setDraft(null)}>Discard draft</Button>
            {confirmFailed && (
              <p role="alert" className="text-sm text-bad">
                That could not be received — check the quantities, then try again.
              </p>
            )}
          </div>
        </div>
      )}
    </Card>
  )
}

// ----- the section ---------------------------------------------------------------------------------------------------

export default function InventorySection() {
  const [setup, setSetup] = useState<InventorySetupView | null>(null)
  const [check, setCheck] = useState<ClosingCheckView | null>(null)
  const [receipts, setReceipts] = useState<SupplierReceipt[] | null>(null)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    try {
      const [setupView, checkView, receiptRows] = await Promise.all([
        inventoryApi.setup(), inventoryApi.closingCheck(), inventoryApi.receipts(),
      ])
      setSetup(setupView)
      setCheck(checkView)
      setReceipts(receiptRows)
      setError(false)
    } catch {
      setError(true)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const loading = (setup === null || check === null || receipts === null) && !error

  return (
    <SectionState loading={loading} error={error}>
      {setup && check && receipts && (
        <div className="flex flex-col gap-6">
          <OpeningStock setup={setup} onSaved={(view) => { setSetup(view); void load() }} />
          <ClosingCheck check={check} onDone={setCheck} />
          <BillScan stock={setup.items}
            onReceived={(receipt) => setReceipts((rows) => [receipt, ...(rows ?? [])])} />
          <Card title="Bills received" className="motion-safe:animate-rise">
            {receipts.length === 0 ? (
              <p className="text-muted">No purchase bills have been scanned in yet.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {receipts.map((receipt) => (
                  <li key={receipt.id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white/2 px-3 py-2 ring-1 ring-white/5">
                    <span className="min-w-0">
                      <span className="flex flex-wrap items-center gap-2 font-semibold">
                        {receipt.supplier_name ?? 'Supplier not shown'}
                        {receipt.simulated && <Chip tone="warn">Simulated</Chip>}
                      </span>
                      <span className="block text-sm text-muted">
                        {receipt.lines.map((line) => `${line.name} ×${formatNumber(line.qty)}`).join(' · ')}
                      </span>
                    </span>
                    <span className="text-sm text-muted">
                      {receipt.total_cents !== null ? `${formatCents(receipt.total_cents)} · ` : ''}
                      received by {receipt.received_by} · {filedWhen(receipt.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </SectionState>
  )
}
