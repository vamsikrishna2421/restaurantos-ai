// @vitest-environment jsdom
// The `shelf-count` slot (P38): REST #43 through the real api client to a mocked fetch, the Spice Route stock in the
// real tenant store, and the in-browser shrink mocked (jsdom has no image decoding or canvas).
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { InventoryItemView, ShelfCount } from '../../../contracts/generated'
import { resetTenantFeedForTests, useTenant } from '../../../stores'
import { fixtureState } from '../../../test/fixtures'
import ShelfCountSlot from '.'
import { HAND_REASON } from './HandCount'
import { fitWithin, shrinkPhoto } from './photo'
import { countProblem, parseCount, problemText, proposedCount, shapesOf, stockText } from './model'
import { ApiError } from '../../../lib/api'

vi.mock('./photo', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./photo')>()),
  shrinkPhoto: vi.fn(async () => ({ blob: new Blob(['small jpeg'], { type: 'image/jpeg' }), width: 640, height: 480 })),
}))

const PANEER_SVG = '<path d="M101 200.5L108 201.5L100 205Z"/><rect x="120" y="200" width="8" height="6"/>'
const COUNT: ShelfCount = {
  id: 'shc_01JTEST',
  items: [
    { ingredient_id: 'ING_PANEER', concept: 'paneer packs', spark_count: 2, sam_count: 2, agreed: true,
      mean_conf_pct: 90, masks_svg: PANEER_SVG, points: [{ x: 160, y: 420 }, { x: 190, y: 420 }] },
    { ingredient_id: 'ING_EGG', concept: 'eggs', spark_count: 8, sam_count: 5, agreed: false, mean_conf_pct: 77,
      masks_svg: '', points: [] },
  ],
}

interface Sent {
  method: string
  path: string
  body: unknown
}

let sent: Sent[] = []
let answer: (call: Sent) => Response | Promise<Response> = () => new Response(null, { status: 404 })

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const refuse = (status: number, code: string, message = code) =>
  json(status, { error: { code, message, details: {} } })

function view(id: string, onHand: number, band: 'ok' | 'watch' | 'act'): InventoryItemView {
  const base = fixtureState().inventory.find((v) => v.ingredient.id === id)
  if (!base) throw new Error(`no ${id} in the fixture`)
  return { ingredient: { ...base.ingredient, on_hand: onHand }, forecast: { ...base.forecast, band } }
}

beforeEach(() => {
  sent = []
  resetTenantFeedForTests()
  act(() => useTenant.getState().load(fixtureState()))
  vi.stubGlobal('fetch', vi.fn(async (path: string, init: RequestInit = {}) => {
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body
    const call = { method: init.method ?? 'GET', path: String(path), body }
    sent.push(call)
    return answer(call)
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.mocked(shrinkPhoto).mockClear()
})

function takePhoto() {
  const file = new File(['camera bytes'], 'IMG_0001.HEIC', { type: 'image/heic' })
  fireEvent.change(screen.getByLabelText('Shelf photo'), { target: { files: [file] } })
  return file
}

test('photo_counts_agree_check_and_confirm', async () => {
  let counted!: () => void
  const models = new Promise<void>((resolve) => { counted = resolve })  // the models answer when the test says so
  answer = async (c) => {
    if (c.path === '/api/perception/shelf') {
      await models
      return json(200, COUNT)
    }
    return c.path === '/api/perception/shelf/shc_01JTEST/confirm'
      ? json(200, [view('ING_PANEER', 2000, 'watch'), view('ING_EGG', 5, 'act')]) : refuse(404, 'not_found')
  }
  render(<ShelfCountSlot ingredientId="ING_PANEER" />)
  const input = screen.getByLabelText('Shelf photo')
  expect(input).toHaveAttribute('accept', 'image/*')
  expect(input).toHaveAttribute('capture', 'environment')
  const start = screen.getByRole('button', { name: 'Count stock from a photo' })
  expect(start.querySelector('.sr-only')).toHaveTextContent('from a photo') // "Count stock" on a phone
  expect(start.querySelector('.sr-only')).toHaveClass('sm:not-sr-only')
  fireEvent.click(start)
  const file = takePhoto()

  const dialog = await screen.findByRole('dialog', { name: 'Count stock from a photo' })
  await waitFor(() => expect(sent).toHaveLength(1))
  expect(within(dialog).getByRole('status')).toHaveTextContent('Counting with Muse Spark and SAM 3.1…')
  counted()
  const items = await within(dialog).findByRole('list', { name: 'Counted items' })
  expect(shrinkPhoto).toHaveBeenCalledWith(file)
  const [upload] = sent
  expect(upload.method).toBe('POST')
  const form = upload.body as FormData
  expect((form.get('image') as File).name).toBe('shelf.jpg')
  expect(await (form.get('image') as Blob).text()).toBe('small jpeg') // the shrunk JPEG, never the camera file

  expect(within(dialog).getByRole('status')).toHaveTextContent(
    'Counts ready. Paneer: the models agree. Egg: check it. Then confirm.')
  const paneer = within(items).getByRole('listitem', { name: 'Paneer' })
  expect(within(paneer).getByText('Agree')).toBeInTheDocument()
  expect(within(paneer).getByLabelText('Count for Paneer, packs of 1,000 g')).toHaveValue('2') // SAM's, agreed
  expect(paneer).toHaveTextContent('2 packs = 2,000 g')
  expect(paneer).toHaveTextContent('90% sure')
  const egg = within(items).getByRole('listitem', { name: 'Egg' })
  expect(within(egg).getByText('Check')).toBeInTheDocument()
  const eggInput = within(egg).getByLabelText('Count for Egg, each')
  expect(eggInput).toHaveValue('') // they disagree: the person chooses
  expect(screen.getByRole('button', { name: 'Confirm count' })).toBeEnabled()
  for (const bad of ['2.5', '1000', '1e2', '-1']) { // said in plain words and never dropped from Confirm silently
    fireEvent.change(eggInput, { target: { value: bad } })
    expect(eggInput).toHaveAttribute('aria-invalid', 'true')
    expect(eggInput).toHaveAccessibleDescription('Enter a whole number from 0 to 999')
    expect(screen.getByRole('button', { name: 'Confirm count' })).toBeDisabled()
  }
  fireEvent.change(eggInput, { target: { value: '' } }) // empty leaves the egg out
  expect(eggInput).not.toHaveAttribute('aria-invalid')
  expect(screen.getByRole('button', { name: 'Confirm count' })).toBeEnabled()
  fireEvent.click(within(egg).getByRole('button', { name: 'SAM 3.1: 5' }))
  expect(eggInput).toHaveValue('5')
  expect(egg).toHaveTextContent('5 each')
  expect(within(egg).getByRole('button', { name: 'Muse Spark: 8' })).toHaveAttribute('aria-pressed', 'false')

  const photo = screen.getByTestId('shelf-photo')
  expect(photo.parentElement).toHaveClass('items-start') // in two columns the photo keeps its own height
  const svg = within(photo).getByRole('img', { name: /Outlined by SAM 3.1: Paneer 2, Egg 5/ })
  expect(svg).toHaveAttribute('viewBox', '0 0 640 480')
  expect(svg.querySelectorAll('path')).toHaveLength(1)
  expect(svg.querySelectorAll('rect')).toHaveLength(1)
  expect(svg.querySelectorAll('[data-point]')).toHaveLength(2)

  fireEvent.click(screen.getByRole('button', { name: 'Confirm 2 counts' }))
  await within(dialog).findByText('Stock updated — the AI GM re-plans if needed')
  const confirm = sent.find((c) => c.path.endsWith('/confirm'))
  expect(confirm?.body).toEqual({ counts: { ING_PANEER: 2, ING_EGG: 5 } })
  expect(dialog).toHaveTextContent('2,000 g on hand')
  expect(within(dialog).getByText('act')).toBeInTheDocument() // the new band
  expect(within(dialog).getByRole('status')).toHaveTextContent(
    'Stock updated: Paneer 2,000 g, watch; Egg 5 ea, act. The AI GM re-plans if needed.')
  expect(screen.getByRole('button', { name: 'Done' })).toHaveFocus() // focus never falls out of the dialog
  fireEvent.click(screen.getByRole('button', { name: 'Done' }))
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('a_failed_confirm_keeps_the_numbers_and_tries_again', async () => {
  let confirms = 0
  let reconnect!: () => void
  const dropped = new Promise<void>((resolve) => { reconnect = resolve })
  answer = async (c) => {
    if (c.path === '/api/perception/shelf') return json(200, COUNT)
    if (!c.path.endsWith('/confirm')) return refuse(404, 'not_found')
    confirms += 1
    if (confirms > 1) return json(200, [view('ING_PANEER', 2000, 'watch'), view('ING_EGG', 7, 'act')])
    await dropped
    throw new TypeError('Failed to fetch') // the connection dropped
  }
  render(<ShelfCountSlot />)
  takePhoto()
  const egg = within(await screen.findByRole('list', { name: 'Counted items' })).getByRole('listitem', { name: 'Egg' })
  fireEvent.change(within(egg).getByLabelText('Count for Egg, each'), { target: { value: '7' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm 2 counts' }))
  expect(screen.getByRole('status')).toHaveTextContent('Saving the counts…')
  expect(screen.getByRole('button', { name: 'Take another photo' })).toBeDisabled() // its answer would be lost
  reconnect()
  expect(await screen.findByRole('alert')).toHaveTextContent('The server could not be reached. Check the connection')
  expect(screen.getByLabelText('Count for Egg, each')).toHaveValue('7') // the review and its numbers stay
  expect(screen.getByLabelText('Count for Paneer, packs of 1,000 g')).toHaveValue('2')
  expect(screen.getByRole('button', { name: 'Take another photo' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm 2 counts' }))
  await screen.findByText('Stock updated — the AI GM re-plans if needed')
  expect(confirms).toBe(2)
  expect(sent.filter((c) => c.path.endsWith('/confirm')).map((c) => c.body)).toEqual(
    [{ counts: { ING_PANEER: 2, ING_EGG: 7 } }, { counts: { ING_PANEER: 2, ING_EGG: 7 } }])
})

test('a_count_that_no_longer_waits_offers_a_new_photo', async () => {
  answer = (c) => c.path === '/api/perception/shelf' ? json(200, COUNT) : refuse(404, 'not_found')
  render(<ShelfCountSlot />)
  takePhoto()
  await screen.findByRole('list', { name: 'Counted items' })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm count' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('This count is no longer waiting: it was saved already')
  expect(screen.getByRole('form', { name: 'Count by hand' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Take another photo' })).toBeEnabled()
})

test('problems_in_plain_words_with_the_count_by_hand', async () => {
  const unavailable = 'Counting from a photo is unavailable right now (the AI budget is used up for now). ' +
    'Enter the count by hand.'
  answer = (c) => c.path === '/api/perception/shelf' ? refuse(503, 'ai_unavailable', unavailable)
    : c.path === '/api/inventory/adjust' ? json(200, view('ING_PANEER', 3000, 'watch')) : refuse(404, 'not_found')
  render(<ShelfCountSlot ingredientId="ING_PANEER" />)
  takePhoto()
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent(unavailable)
  const hand = screen.getByRole('form', { name: 'Count by hand' })
  expect(within(hand).getByLabelText('Ingredient')).toHaveValue('ING_PANEER') // the console's focus first
  expect(within(hand).getByRole('button', { name: 'Save count' })).toBeDisabled()
  const typed = within(hand).getByLabelText('Count (packs of 1,000 g)')
  fireEvent.change(typed, { target: { value: '3.5' } })
  expect(typed).toHaveAccessibleDescription('Enter a whole number from 0 to 999')
  expect(within(hand).getByRole('button', { name: 'Save count' })).toBeDisabled()
  fireEvent.change(typed, { target: { value: '3' } })
  expect(hand).toHaveTextContent('3 packs = 3,000 g')
  fireEvent.click(within(hand).getByRole('button', { name: 'Save count' }))
  await screen.findByText('Stock updated — the AI GM re-plans if needed')
  expect(sent.at(-1)).toEqual({ method: 'POST', path: '/api/inventory/adjust',
    body: { ingredient_id: 'ING_PANEER', to_qty: 3000, reason: HAND_REASON } })

  const problems = [[429, 'rate_limited', 'Too many photos'], [413, 'payload_too_large', 'over 6 MB'],
    [400, 'bad_request', 'could not be read'], [404, 'not_found', 'not available on this server']] as const
  for (const [status, code, words] of problems) {
    cleanup()
    answer = () => refuse(status, code)
    render(<ShelfCountSlot />)
    takePhoto()
    expect(await screen.findByRole('alert')).toHaveTextContent(words)
    expect(screen.getByRole('form', { name: 'Count by hand' })).toBeInTheDocument()
  }
})

test('nothing_counted_offers_the_count_by_hand', async () => {
  answer = () => json(200, { id: 'shc_EMPTY', items: [] })
  render(<ShelfCountSlot />)
  takePhoto()
  await waitFor(() => expect(screen.getByText(/Nothing this kitchen stocks was clearly visible/)).toBeInTheDocument())
  expect(screen.getByRole('form', { name: 'Count by hand' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirm count' })).toBeDisabled()
})

test('model_helpers', () => {
  expect(shapesOf(PANEER_SVG)).toEqual([{ kind: 'path', d: 'M101 200.5L108 201.5L100 205Z' },
    { kind: 'rect', x: 120, y: 200, width: 8, height: 6 }])
  // only numbers and path letters: markup, scripts and attributes from anywhere else are never drawn
  expect(shapesOf('<script>alert(1)</script><path d="M1 1Z" onload="x"/><image href="x"/><path d="javascript:1"/>'))
    .toEqual([])
  expect(proposedCount(COUNT.items[0])).toBe(2)
  expect(proposedCount(COUNT.items[1])).toBeNull()
  expect(stockText(3, { unit: 'g', pack_size: 1000 })).toBe('3 packs = 3,000 g')
  expect(stockText(1, { unit: 'ml', pack_size: 4000 })).toBe('1 pack = 4,000 ml')
  expect(stockText(30, { unit: 'ea', pack_size: 30 })).toBe('30 each')
  expect(['7', ' 0 ', '999', '1000', '-1', '2.5', ''].map(parseCount)).toEqual([7, 0, 999, null, null, null, null])
  expect(['', ' ', '12', '12a'].map(countProblem)).toEqual([null, null, null, 'Enter a whole number from 0 to 999'])
  expect(problemText(new ApiError('not_found', 404))).toMatch(/not available on this server/)
  expect(problemText(new ApiError('not_found', 404), 'confirm')).toMatch(/no longer waiting/)
  expect(problemText(new ApiError('internal_error', 500), 'confirm')).toMatch(/could not be saved. Try again/)
  expect(fitWithin(4032, 3024)).toEqual({ width: 1280, height: 960 })
  expect(fitWithin(3024, 4032)).toEqual({ width: 960, height: 1280 })
  expect(fitWithin(800, 600)).toEqual({ width: 800, height: 600 }) // never enlarged
  expect(problemText(new ApiError('network_error', 0))).toMatch(/could not be reached/)
  expect(problemText(new Error('decode failed'))).toMatch(/could not be opened on this device/)
})
