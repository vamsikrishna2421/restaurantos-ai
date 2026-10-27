// Kitchen i18n: the KDS reads exactly what domain/i18n_kitchen.py renders (expected strings below were produced by
// the Python renderer on the same fixture), in each station's language, with safe fallbacks.
import { describe, expect, test } from 'vitest'
import type { Ticket } from '../contracts/generated'
import { fixtureState, storyState, ticketFor } from '../test/fixtures'
import {
  itemName,
  KITCHEN_LABEL_KEYS,
  kitchenLabel,
  kitchenLang,
  LANGUAGE_NAMES,
  modifierLabel,
  pickLabel,
  renderLines,
  renderTicket,
  soldOutNames,
  stationLang,
  stationName,
  stationShort,
} from './i18n'

const menu = fixtureState().menu

describe('kitchen labels and station names from TenantState.menu', () => {
  test('kitchen_labels_in_every_language', () => {
    expect(['en', 'te', 'ta', 'es'].map((lang) => kitchenLabel(menu, 'eighty_six', lang)))
      .toEqual(['86 — SOLD OUT', 'అయిపోయింది', 'தீர்ந்துவிட்டது', 'AGOTADO'])
    for (const key of KITCHEN_LABEL_KEYS) expect(kitchenLabel(menu, key, 'es')).not.toBe(key)
    expect(kitchenLabel(menu, 'bump', 'es')).toBe('LISTO')
    expect(kitchenLabel(menu, 'no_such_word', 'te')).toBe('no_such_word')
  })

  test('code-mixed languages read as their base, anything else as English', () => {
    expect(kitchenLang('te-en')).toBe('te')
    expect(kitchenLang('ta-en')).toBe('ta')
    expect(kitchenLang('hi')).toBe('en')
    expect(kitchenLang(null)).toBe('en')
    expect(kitchenLabel(menu, 'new', 'ta-en')).toBe('புதியது')
    expect(LANGUAGE_NAMES.te).toBe('తెలుగు')
  })

  test('station names and default languages', () => {
    expect(stationName(menu, 'STN_TIFFIN', 'ta')).toBe('டிபன்')
    expect(stationName(menu, 'STN_FRY', 'es')).toBe('Freidora')
    expect(stationName(menu, 'STN_BAR')).toBe('Drinks & desserts')
    expect(stationLang(menu, 'STN_CURRY')).toBe('es')
    expect(stationLang(menu, 'STN_TANDOOR')).toBe('te')
    expect(stationLang(menu, 'STN_NOWHERE')).toBe('en')
    expect(stationShort('STN_CURRY')).toBe('CURRY')
    expect(stationName(menu, 'STN_NOWHERE', 'es')).toBe('NOWHERE')
    // a station missing from menu.stations falls back to the kitchen label keyed by its id
    expect(stationName({ ...menu, stations: [] }, 'STN_CURRY', 'te')).toBe('కూర')
  })

  test('dish names in native script, first letter capitalized; modifiers in CAPS', () => {
    expect(itemName(menu, 'PNR_TIKKA', 'te')).toBe('పనీర్ టిక్కా')
    expect(itemName(menu, 'CHK_BIRY', 'es')).toBe('Biryani de pollo')
    expect(itemName(menu, 'CHK_BIRY', 'ta')).toBe('சிக்கன் பிரியாணி')
    expect(itemName(menu, 'UNKNOWN_DISH', 'es')).toBe('UNKNOWN_DISH')
    expect(modifierLabel(menu, 'NO_ONION', 'es')).toBe('SIN CEBOLLA')
    expect(modifierLabel(menu, 'NO_ONION', 'en')).toBe('NO ONION')
    expect(modifierLabel(menu, 'NO_ONION', 'te')).toBe('ఉల్లిపాయ లేకుండా')
    expect(modifierLabel(menu, 'NOPE', 'es')).toBe('NOPE')
  })

  test('a missing translation falls back to English, then to the code', () => {
    expect(pickLabel({ en: 'Paneer' }, 'te', 'ING_PANEER')).toBe('Paneer')
    expect(pickLabel({ en: '', te: '' }, 'te', 'ING_PANEER')).toBe('ING_PANEER')
    expect(pickLabel(null, 'es', 'X')).toBe('X')
    const gap = { ...menu, items: menu.items.map((i) => (i.code === 'IDLI' ? { ...i, names: { ...i.names, ta: '' } } : i)) }
    expect(itemName(gap, 'IDLI', 'ta')).toBe('Idli')
  })
})

describe('ticket lines (ARCHITECTURE §9 format)', () => {
  test('the canonical order reads "2× Biryani de pollo — 1 SIN CEBOLLA" at the Spanish station', () => {
    const story = storyState()
    const biryani = renderTicket(ticketFor(story, 1047, 'STN_BIRYANI'), story.menu)
    expect(biryani).toEqual({
      title: '#1047 · BIRYANI', lang: 'es', flags: [],
      lines: [{ text: '2× Biryani de pollo — 1 SIN CEBOLLA', item_code: 'CHK_BIRY', qty: 2, changed: false }],
    })
    expect(renderTicket(ticketFor(story, 1047, 'STN_CURRY'), story.menu).lines[0].text)
      .toBe('1× Paneer en salsa de mantequilla')
    // the KDS language toggle
    expect(renderTicket(ticketFor(story, 1047, 'STN_BIRYANI'), story.menu, 'te').lines[0].text)
      .toBe('2× చికెన్ దమ్ బిర్యానీ — 1 ఉల్లిపాయ లేకుండా')
  })

  test('each station renders in its own language by default', () => {
    const state = fixtureState()
    expect(renderTicket(ticketFor(state, 1036, 'STN_TANDOOR'), state.menu))
      .toMatchObject({ title: '#1036 · తందూర్', lang: 'te', lines: [{ text: '1× పనీర్ టిక్కా' }] })
    expect(renderTicket(ticketFor(state, 1039, 'STN_TIFFIN'), state.menu))
      .toMatchObject({ title: '#1039 · டிபன்', lang: 'ta', lines: [{ text: '1× இட்லி' }] })
    expect(renderTicket(ticketFor(state, 1034, 'STN_BAR'), state.menu).title).toBe('#1034 · DRINKS & DESSERTS')
  })

  test('variants merge by modifier set; a removed dish is qty 0 and changed; unknown codes show as codes', () => {
    const story = storyState()
    const corrected: Ticket = {
      ...ticketFor(story, 1031, 'STN_CURRY'),
      changed: true,
      lines: [
        { line_no: 3, item_code: 'CHK_BIRY', qty: 2, modifiers: ['MILD', 'NO_ONION'], changed: false },
        { line_no: 1, item_code: 'CHK_BIRY', qty: 1, modifiers: ['NO_ONION', 'MILD', 'NO_ONION'], changed: true },
        { line_no: 2, item_code: 'PNR_BM', qty: 0, modifiers: ['EXTRA_BUTTER'], changed: true },
        { line_no: 4, item_code: 'MYSTERY', qty: 1, modifiers: ['NOPE'], changed: false },
        { line_no: 5, item_code: 'CHK_BIRY', qty: 1, modifiers: ['EXTRA_RAITA'], changed: false },
      ],
    }
    expect(renderTicket(corrected, story.menu, 'es')).toEqual({
      title: '#1031 · CURRY', lang: 'es', flags: ['CAMBIADO'],
      lines: [
        { text: '4× Biryani de pollo — 3 SIN CEBOLLA, POCO PICANTE; 1 RAITA EXTRA', item_code: 'CHK_BIRY', qty: 4,
          changed: true },
        { text: '0× Paneer en salsa de mantequilla', item_code: 'PNR_BM', qty: 0, changed: true },
        { text: '1× MYSTERY — 1 NOPE', item_code: 'MYSTERY', qty: 1, changed: false },
      ],
    })
  })

  test('cart lines render the same way (the kiosk and the monitor)', () => {
    const lines = [{ line_no: 1, item_code: 'GOBI_MAN', qty: 2, modifiers: ['MILD'], unit_price_cents: 1199,
      line_total_cents: 2398 }]
    expect(renderLines(lines, menu, 'ta')).toEqual([
      { text: '2× கோபி மஞ்சூரியன் — 2 காரம் குறைவு', item_code: 'GOBI_MAN', qty: 2, changed: false },
    ])
  })

  test("86'd dishes for the banner, in menu order", () => {
    const story = storyState()
    expect(soldOutNames(story.menu)).toEqual(['Palak Paneer', 'Paneer Tikka'])
    expect(soldOutNames(story.menu, 'es')).toEqual(['Paneer con espinacas', 'Paneer tikka'])
    expect(soldOutNames(fixtureState().menu)).toEqual([])
  })
})
