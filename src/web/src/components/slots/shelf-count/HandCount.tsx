/**
 * The count by hand (P38's fallback; REST #18 `POST /api/inventory/adjust`): when a photo cannot be counted (the AI
 * is down or out of budget, too many photos, a photo that cannot be read) or the models found nothing, the manager
 * types the count for one ingredient, in the same units as a photo count (each, or packs × pack size). A count that
 * is not a whole number from 0 to 999 is said in plain words and keeps Save disabled.
 */

import { Save } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Button } from '../../ui'
import type { InventoryItemView } from '../../../contracts/generated'
import { api, isApiError } from '../../../lib/api'
import { countProblem, countUnit, isCountable, parseCount, perCount, stockText } from './model'

export const HAND_REASON = 'Counted by hand (shelf photo count unavailable)'

export interface HandCountProps {
  inventory: readonly InventoryItemView[]
  focusId: string | null
  onSaved: (view: InventoryItemView) => void
}

export function HandCount({ inventory, focusId, onSaved }: HandCountProps) {
  const options = inventory.filter((v) => isCountable(v.ingredient))
    .sort((a, b) => Number(b.ingredient.id === focusId) - Number(a.ingredient.id === focusId))
  const [picked, setChosen] = useState<string>('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const pickId = useId()
  const countId = useId()
  const ruleId = useId()
  // the focus ingredient (or the first) until the manager picks another; the list may load after the first render
  const chosen = options.some((v) => v.ingredient.id === picked) ? picked : options[0]?.ingredient.id ?? ''
  const ingredient = options.find((v) => v.ingredient.id === chosen)?.ingredient
  const n = parseCount(text)
  const rule = countProblem(text)
  if (options.length === 0) return null

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!ingredient || n === null) return
    setBusy(true)
    setNote(null)
    try {
      onSaved(await api<InventoryItemView>('POST', '/api/inventory/adjust',
        { ingredient_id: ingredient.id, to_qty: n * perCount(ingredient), reason: HAND_REASON }))
    } catch (err) {
      setNote(isApiError(err) && (err.status === 401 || err.status === 403) ? 'Only a manager can change stock.'
        : 'The count could not be saved. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void save(e)} aria-label="Count by hand"
      className="flex flex-col gap-2 rounded-lg border border-line bg-panel-2 p-3">
      <h3 className="font-semibold">Count by hand</h3>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor={pickId} className="text-sm text-muted">Ingredient</label>
          <select id={pickId} value={chosen} onChange={(e) => setChosen(e.target.value)}
            className="rounded-md border border-line bg-ink px-2 py-1.5 text-base text-text">
            {options.map((v) => (
              <option key={v.ingredient.id} value={v.ingredient.id}>{v.ingredient.names.en ?? v.ingredient.id}</option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={countId} className="text-sm text-muted">
            {ingredient ? `Count (${countUnit(ingredient)})` : 'Count'}
          </label>
          <input id={countId} type="text" inputMode="numeric" autoComplete="off" value={text}
            aria-invalid={rule !== null || undefined} aria-describedby={rule ? ruleId : undefined}
            onChange={(e) => setText(e.target.value)}
            className="w-28 rounded-md border border-line bg-ink px-2 py-1.5 text-base tabular-nums text-text" />
        </div>
        <Button type="submit" size="md" variant="primary" icon={<Save />} loading={busy}
          disabled={!ingredient || n === null}>Save count</Button>
      </div>
      {ingredient && n !== null && <p className="text-sm text-muted">{stockText(n, ingredient)}</p>}
      {rule && <p id={ruleId} className="text-sm text-bad">{rule}</p>}
      {note && <p role="alert" className="text-sm text-bad">{note}</p>}
    </form>
  )
}
