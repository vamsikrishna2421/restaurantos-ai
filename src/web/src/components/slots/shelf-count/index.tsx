/**
 * The `shelf-count` slot (P38; ARCHITECTURE_VOICE_MEDIA §10 "Shelf count: two-model consensus", CONTRACTS_API #43)
 * on the GM inventory panel: "Count stock from a photo" ("Count stock" on a phone, so the panel title keeps its room).
 *
 * The phone's camera (or a file) gives a photo; the browser shrinks it to a ≤1280 px JPEG without metadata
 * (`photo.ts`) and posts it to `POST /api/perception/shelf`. While Muse Spark points at and counts the items and
 * SAM 3.1 outlines each one, the dialog says so. Then it shows the photo with SAM's outlines and Spark's points and,
 * per item, the name, both counts, Agree or Check, a number (SAM's count when the two agree, else empty with both
 * counts one tap away) and what it means in stock ("3 packs = 3,000 g"). A number that is not a whole number from 0
 * to 999 is said in plain words and holds Confirm back. Confirm posts the numbers (`…/{id}/confirm`) and shows the
 * new stock and band: the AI GM re-plans if needed (the server's `inventory.adjusted` drives it). Nothing changes
 * until a person confirms. A failed Confirm keeps the review and its numbers so it can be tried again, unless the
 * count no longer waits (404). Every other problem (503, 429, 413, 400, …) is said in plain words next to the count
 * by hand (`HandCount.tsx`).
 *
 * Accessibility: one polite live region says each stage (counting, counts ready, saving, the new stock); focus moves
 * to the stage's main button when a stage change would leave it outside the dialog, and to Done once saved.
 *
 * Props: `ingredientId?: string | null`, the console's focus ingredient (first in the count-by-hand list).
 */

import { Camera, CheckCircle2, LoaderCircle, RotateCcw } from 'lucide-react'
import { useEffect, useId, useRef, useState, type ChangeEvent, type Ref } from 'react'
import { Badge, Button, Chip, Modal, cx } from '../../ui'
import type { Ingredient, InventoryItemView, ShelfCount, ShelfCountItem } from '../../../contracts/generated'
import { api, isApiError } from '../../../lib/api'
import { formatNumber } from '../../../lib/format'
import { useTenantList } from '../../../stores'
import type { SlotProps } from '../../../slots'
import { HandCount } from './HandCount'
import { BAND_TONE, countProblem, countUnit, parseCount, problemText, proposedCount, stockText } from './model'
import { ITEM_TONES, Overlay } from './Overlay'
import { shrinkPhoto, type ShrunkPhoto } from './photo'

type Stage = 'counting' | 'review' | 'saving' | 'done' | 'problem'

interface Shot extends ShrunkPhoto {
  url: string
}

const previewUrl = (blob: Blob) => (typeof URL.createObjectURL === 'function' ? URL.createObjectURL(blob) : '')
const dropUrl = (url: string | undefined) => {
  if (url && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url)
}

export default function ShelfCountSlot(props: SlotProps) {
  const focusId = typeof props.ingredientId === 'string' ? props.ingredientId : null
  const inventory = useTenantList('inventory')
  const input = useRef<HTMLInputElement>(null)
  const main = useRef<HTMLButtonElement>(null)  // the footer's main button for the stage
  const run = useRef(0)  // a newer photo, or closing the dialog, makes an older answer stale
  const [open, setOpen] = useState(false)
  const [stage, setStage] = useState<Stage>('counting')
  const [shot, setShot] = useState<Shot | null>(null)
  const [count, setCount] = useState<ShelfCount | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<InventoryItemView[]>([])
  const [problem, setProblem] = useState<string | null>(null)
  const [saveProblem, setSaveProblem] = useState<string | null>(null)
  useEffect(() => () => dropUrl(shot?.url), [shot])
  useEffect(() => {  // a stage change can unmount or disable the focused button: keep focus in the dialog
    if (!open) return
    const active = document.activeElement
    if (stage === 'done' || !active || active === document.body || !active.closest('[role="dialog"]')) {
      main.current?.focus()
    }
  }, [open, stage, saveProblem])

  const ingredientOf = (id: string) => inventory.find((v) => v.ingredient.id === id)?.ingredient
  const nameOf = (id: string) => ingredientOf(id)?.names.en ?? id
  const fail = (mine: number, err: unknown) => {
    if (mine !== run.current) return
    setProblem(problemText(err))
    setStage('problem')
  }

  const onFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''  // the same photo can be chosen again
    if (!file) return
    const mine = ++run.current
    setOpen(true)
    setStage('counting')
    setShot(null)
    setCount(null)
    setProblem(null)
    setSaveProblem(null)
    let photo: ShrunkPhoto
    try {
      photo = await shrinkPhoto(file)
    } catch (err) {
      fail(mine, err)
      return
    }
    if (mine !== run.current) return
    setShot({ ...photo, url: previewUrl(photo.blob) })
    const form = new FormData()
    form.append('image', photo.blob, 'shelf.jpg')
    try {
      const result = await api<ShelfCount>('POST', '/api/perception/shelf', form)
      if (mine !== run.current) return
      setCount(result)
      setValues(Object.fromEntries(result.items.map((item) => [item.ingredient_id, String(proposedCount(item) ?? '')])))
      setStage('review')
    } catch (err) {
      fail(mine, err)
    }
  }

  const typed = count ? count.items.map((item) => [item.ingredient_id, values[item.ingredient_id] ?? ''] as const) : []
  const chosen = typed.flatMap(([id, text]) => {
    const n = parseCount(text)
    return n === null ? [] : [[id, n] as const]
  })
  const ready = chosen.length > 0 && typed.every(([, text]) => countProblem(text) === null)

  const confirm = async () => {
    if (!count || !ready) return
    const mine = run.current
    setSaveProblem(null)
    setStage('saving')
    try {
      const views = await api<InventoryItemView[]>('POST',
        `/api/perception/shelf/${encodeURIComponent(count.id)}/confirm`, { counts: Object.fromEntries(chosen) })
      if (mine !== run.current) return
      setSaved(views)
      setStage('done')
    } catch (err) {
      if (mine !== run.current) return
      if (isApiError(err) && err.status === 404) {  // the count no longer waits: a new photo or the count by hand
        setProblem(problemText(err, 'confirm'))
        setStage('problem')
      } else {  // it still waits on the server: keep the review and its numbers, and let Confirm try again
        setSaveProblem(problemText(err, 'confirm'))
        setStage('review')
      }
    }
  }

  const handSaved = (view: InventoryItemView) => {
    setSaved([view])
    setStage('done')
  }

  const close = () => {
    run.current += 1
    setOpen(false)
    setShot(null)
  }

  const retake = (ref?: Ref<HTMLButtonElement>) => (  // not while saving: the save's answer would be lost
    <Button ref={ref} variant="secondary" icon={<RotateCcw />} disabled={stage === 'saving'}
      onClick={() => input.current?.click()}>Take another photo</Button>
  )
  const footer = stage === 'counting' ? <Button ref={main} variant="ghost" onClick={close}>Cancel</Button>
    : stage === 'review' || stage === 'saving' ? (
      <>
        {retake()}
        <Button ref={main} variant="primary" loading={stage === 'saving'} disabled={!ready}
          onClick={() => void confirm()}>
          {chosen.length > 1 ? `Confirm ${chosen.length} counts` : 'Confirm count'}
        </Button>
      </>
    ) : stage === 'problem' ? <>{retake(main)}<Button variant="ghost" onClick={close}>Close</Button></>
      : <Button ref={main} variant="primary" onClick={close}>Done</Button>

  const said = stage === 'counting' ? 'Counting with Muse Spark and SAM 3.1…'
    : stage === 'saving' ? 'Saving the counts…'
    : stage === 'done' ? savedText(saved)
    : stage === 'review' && count && !saveProblem ? readyText(count.items, nameOf) : ''

  return (
    <>
      <Button size="sm" variant="secondary" icon={<Camera />} onClick={() => input.current?.click()}>
        <span>Count stock <span className="sr-only sm:not-sr-only">from a photo</span></span>
      </Button>
      <input ref={input} type="file" accept="image/*" capture="environment" aria-label="Shelf photo"
        className="hidden" onChange={(e) => void onFile(e)} />
      <Modal open={open} onClose={close} title="Count stock from a photo" size="lg" footer={footer}
        dismissible={stage !== 'saving'}>
        <p role="status" data-announce="" className="sr-only">{said}</p>
        {stage === 'counting' && (
          <div className="flex flex-col items-center gap-3 py-10 text-center">
            <LoaderCircle aria-hidden className="size-8 text-ai motion-safe:animate-spin" />
            <p className="text-lg font-semibold">Counting with Muse Spark and SAM 3.1…</p>
            <p className="max-w-md text-sm text-muted">Muse Spark points at each item it can see; SAM 3.1 outlines
              them. Nothing changes until you confirm.</p>
          </div>
        )}
        {(stage === 'review' || stage === 'saving') && count && (
          <div className="flex flex-col gap-3">
            {saveProblem && <p role="alert" className="text-warn">{saveProblem}</p>}
            <div className="grid items-start gap-4 md:grid-cols-2">
              {shot && (
                <Overlay url={shot.url} width={shot.width} height={shot.height} items={count.items}
                  names={count.items.map((item) => nameOf(item.ingredient_id))} />
              )}
              {count.items.length > 0 ? (
                <ul aria-label="Counted items" className="flex min-w-0 flex-col gap-3">
                  {count.items.map((item, i) => (
                    <ItemRow key={item.ingredient_id} item={item} index={i}
                      ingredient={ingredientOf(item.ingredient_id)} value={values[item.ingredient_id] ?? ''}
                      onChange={(v) => setValues((s) => ({ ...s, [item.ingredient_id]: v }))} />
                  ))}
                </ul>
              ) : (
                <div className="flex flex-col gap-3">
                  <p>Nothing this kitchen stocks was clearly visible. Try a closer, brighter photo of packed items, or
                    count by hand.</p>
                  <HandCount inventory={inventory} focusId={focusId} onSaved={handSaved} />
                </div>
              )}
            </div>
          </div>
        )}
        {stage === 'problem' && (
          <div className="flex flex-col gap-3">
            <p role="alert" className="text-warn">{problem}</p>
            <HandCount inventory={inventory} focusId={focusId} onSaved={handSaved} />
          </div>
        )}
        {stage === 'done' && <Saved views={saved} />}
      </Modal>
    </>
  )
}

function readyText(items: readonly ShelfCountItem[], nameOf: (id: string) => string): string {
  if (items.length === 0) return 'Nothing was counted in this photo; the count by hand is below.'
  const each = items.map((item) => `${nameOf(item.ingredient_id)}: ${item.agreed ? 'the models agree' : 'check it'}`)
  return `Counts ready. ${each.join('. ')}. Then confirm.`
}

function savedText(views: readonly InventoryItemView[]): string {
  const each = views.map(({ ingredient, forecast }) =>
    `${ingredient.names.en ?? ingredient.id} ${formatNumber(ingredient.on_hand)} ${ingredient.unit}, ${forecast.band}`)
  return `Stock updated${each.length > 0 ? `: ${each.join('; ')}` : ''}. The AI GM re-plans if needed.`
}

interface ItemRowProps {
  item: ShelfCountItem
  index: number
  ingredient: Ingredient | undefined
  value: string
  onChange: (value: string) => void
}

function ItemRow({ item, index, ingredient, value, onChange }: ItemRowProps) {
  const inputId = useId()
  const ruleId = useId()
  const name = ingredient?.names.en ?? item.ingredient_id
  const n = parseCount(value)
  const rule = countProblem(value)
  const picks = item.agreed ? [] : [
    ...(item.sam_count !== null ? [{ label: `SAM 3.1: ${item.sam_count}`, n: item.sam_count }] : []),
    ...(item.spark_count !== null ? [{ label: `Muse Spark: ${item.spark_count}`, n: item.spark_count }] : []),
  ]
  return (
    <li aria-label={name} data-ingredient={item.ingredient_id}
      className="flex flex-col gap-2 rounded-lg border border-line bg-panel-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span aria-hidden className={cx('size-3 shrink-0 rounded-full', ITEM_TONES[index % ITEM_TONES.length].dot)} />
        <h3 className="min-w-0 font-semibold">{name}</h3>
        <Badge tone={item.agreed ? 'ok' : 'warn'} className="ml-auto">{item.agreed ? 'Agree' : 'Check'}</Badge>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 text-sm">
        <div>
          <dt className="text-muted">SAM 3.1</dt>
          <dd className="font-semibold tabular-nums" data-model="sam">
            {item.sam_count ?? 'no count'}
            {item.sam_count !== null && item.mean_conf_pct > 0 && (
              <span className="font-normal text-muted">{` · ${item.mean_conf_pct}% sure`}</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Muse Spark</dt>
          <dd className="font-semibold tabular-nums" data-model="spark">{item.spark_count ?? 'no count'}</dd>
        </div>
      </dl>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={inputId} className="text-sm text-muted">Count</label>
        <input id={inputId} type="text" inputMode="numeric" autoComplete="off" value={value}
          aria-label={`Count for ${name}${ingredient ? `, ${countUnit(ingredient)}` : ''}`}
          aria-invalid={rule !== null || undefined} aria-describedby={rule ? ruleId : undefined}
          onChange={(e) => onChange(e.target.value)}
          className="w-24 rounded-md border border-line bg-ink px-2 py-1 text-lg tabular-nums text-text" />
        {ingredient && (
          <span className="text-sm text-muted">{n === null ? countUnit(ingredient) : stockText(n, ingredient)}</span>
        )}
      </div>
      {rule && <p id={ruleId} className="text-sm text-bad">{rule}</p>}
      {picks.length > 0 && (
        <div role="group" aria-label={`Pick a count for ${name}`} className="flex flex-wrap gap-2">
          {picks.map((p) => (
            <Chip key={p.label} onClick={() => onChange(String(p.n))} selected={value === String(p.n)}>{p.label}</Chip>
          ))}
        </div>
      )}
    </li>
  )
}

function Saved({ views }: { views: readonly InventoryItemView[] }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="flex items-center gap-2 text-lg font-semibold text-ok">
        <CheckCircle2 aria-hidden className="size-6" />Stock updated — the AI GM re-plans if needed
      </p>
      <ul className="flex flex-col gap-2">
        {views.map(({ ingredient, forecast }) => (
          <li key={ingredient.id} className="flex flex-wrap items-center gap-2 rounded-lg bg-panel-2 px-3 py-2">
            <span className="font-semibold">{ingredient.names.en ?? ingredient.id}</span>
            <span className="tabular-nums text-muted">
              {`${formatNumber(ingredient.on_hand)} ${ingredient.unit} on hand`}
            </span>
            <Badge tone={BAND_TONE[forecast.band]} className="ml-auto">{forecast.band}</Badge>
          </li>
        ))}
      </ul>
    </div>
  )
}
