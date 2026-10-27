/**
 * Spice Route, the SAMPLE restaurant (CONTRACTS_SEED §2–§9, §14), as test data: the menu snapshot and the stock
 * rows exactly as the seed loader builds them from `config/tenants/spice_route/*.yaml` and `config/i18n_kitchen.yaml`
 * (allergens derived from recipes × ingredients, `menu_hash` from `seed.loader.menu_hash`, items in menu order).
 * Test-only: app code never imports `src/test/**`. The names are unreviewed drafts, like the seed they copy.
 */

import type {
  Allergen,
  Ingredient,
  LocalizedLabel,
  MenuItem,
  MenuSnapshot,
  Modifier,
  SpeakLang,
  Station,
  Unit,
} from '../../contracts/generated'

export const SPICE_ROUTE_NAME = 'Spice Route (sample)'
export const SPICE_ROUTE_TAX_BP = 700
/** `seed.loader.menu_hash` of the seed menu (prompt-visible fields only; prices and availability excluded). */
export const SPICE_ROUTE_MENU_HASH = '5cf899b9f1d91cd20a04b85111e34a74a3523fbd08917d43b94e64f64f6eead0'

/** `modifiers.yaml` sets: what each item may carry. */
const SETS = {
  RICE: ['NO_ONION', 'NO_GARLIC', 'NO_CILANTRO', 'MILD', 'EXTRA_SPICY', 'EXTRA_RAITA', 'LESS_OIL'],
  CURRY: ['NO_ONION', 'NO_GARLIC', 'NO_CILANTRO', 'MILD', 'EXTRA_SPICY', 'LESS_OIL', 'EXTRA_BUTTER'],
  DRY: ['NO_ONION', 'NO_GARLIC', 'NO_CILANTRO', 'MILD', 'EXTRA_SPICY', 'LESS_OIL'],
  TIFFIN: ['NO_ONION', 'MILD', 'EXTRA_SPICY', 'LESS_OIL', 'EXTRA_BUTTER'],
  BREAD: ['EXTRA_BUTTER'],
  NONE: [],
} as const satisfies Record<string, readonly string[]>

/** [en, te, te_roman, ta, ta_roman, es] */
type Names = readonly [string, string, string, string, string, string]
/** [en, te, ta, es] */
type Label4 = readonly [string, string, string, string]
type ItemRow = readonly [code: string, category: string, station: string, price_cents: number, is_veg: boolean,
  spice_level: number, prep_min: number, set: keyof typeof SETS, names: Names, aliases: readonly string[],
  allergens: readonly Allergen[]]

const ITEMS: readonly ItemRow[] = [
  ['CHK_BIRY', 'biryani', 'STN_BIRYANI', 1699, false, 2, 14, 'RICE',
    ['Chicken Dum Biryani', 'చికెన్ దమ్ బిర్యానీ', 'chicken dum biryani',
     'சிக்கன் பிரியாணி', 'chicken biryani', 'biryani de pollo'],
    ['chicken biryani', 'kodi biryani', 'కోడి బిర్యానీ', 'kozhi biryani', 'கோழி பிரியாணி'], ['dairy', 'tree_nuts']],
  ['GOAT_BIRY', 'biryani', 'STN_BIRYANI', 1999, false, 2, 14, 'RICE',
    ['Goat Biryani', 'మటన్ బిర్యానీ', 'mutton biryani', 'மட்டன் பிரியாணி', 'mutton biryani', 'biryani de chivo'],
    ['goat biryani', 'mamsam biryani', 'మాంసం బిర్యానీ', 'aattukari biryani', 'ஆட்டுக்கறி பிரியாணி'],
    ['dairy', 'tree_nuts']],
  ['VEG_BIRY', 'biryani', 'STN_BIRYANI', 1499, true, 1, 12, 'RICE',
    ['Veg Biryani', 'వెజ్ బిర్యానీ', 'veg biryani', 'காய்கறி பிரியாணி', 'kaaikari biryani', 'biryani de verduras'],
    ['vegetable biryani', 'kooragayala biryani', 'కూరగాయల బిర్యానీ'], ['dairy', 'tree_nuts']],
  ['EGG_BIRY', 'biryani', 'STN_BIRYANI', 1549, false, 2, 12, 'RICE',
    ['Egg Biryani', 'గుడ్డు బిర్యానీ', 'guddu biryani', 'முட்டை பிரியாணி', 'muttai biryani', 'biryani de huevo'],
    ['egg biryani'], ['dairy', 'egg']],
  ['PNR_BM', 'curry', 'STN_CURRY', 1549, true, 1, 10, 'CURRY',
    ['Paneer Butter Masala', 'పనీర్ బటర్ మసాలా', 'paneer butter masala',
     'பனீர் பட்டர் மசாலா', 'paneer butter masala', 'paneer en salsa de mantequilla'],
    ['paneer curry', 'PBM', 'paneer kura', 'పనీర్ కూర', 'paneer kari', 'பனீர் கறி', 'curry de paneer'],
    ['dairy', 'tree_nuts']],
  ['PALAK_PNR', 'curry', 'STN_CURRY', 1549, true, 1, 10, 'CURRY',
    ['Palak Paneer', 'పాలక్ పనీర్', 'palak paneer', 'பாலக் பனீர்', 'palak paneer', 'paneer con espinacas'],
    ['palakura paneer', 'పాలకూర పనీర్', 'keerai paneer', 'கீரை பனீர்'], ['dairy']],
  ['CHK_TIKKA_MASALA', 'curry', 'STN_CURRY', 1649, false, 1, 10, 'CURRY',
    ['Chicken Tikka Masala', 'చికెన్ టిక్కా మసాలా', 'chicken tikka masala',
     'சிக்கன் டிக்கா மசாலா', 'chicken tikka masala', 'pollo tikka masala'],
    ['CTM'], ['dairy', 'tree_nuts']],
  ['VEG_KORMA', 'curry', 'STN_CURRY', 1449, true, 1, 10, 'CURRY',
    ['Vegetable Korma', 'వెజిటబుల్ కుర్మా', 'vegetable kurma',
     'காய்கறி குருமா', 'kaaikari kurma', 'korma de verduras'],
    ['veg korma', 'veg kurma'], ['dairy', 'tree_nuts']],
  ['DAL_TADKA', 'curry', 'STN_CURRY', 1249, true, 1, 8, 'CURRY',
    ['Dal Tadka', 'పప్పు', 'pappu', 'பருப்பு', 'paruppu', 'lentejas (dal)'],
    ['dal', 'dal tadka', 'దాల్ తడ్కా'], ['dairy', 'mustard']],
  ['CHANA_MASALA', 'curry', 'STN_CURRY', 1299, true, 2, 8, 'CURRY',
    ['Chana Masala', 'శనగల కూర', 'senagala kura', 'சன்னா மசாலா', 'channa masala', 'garbanzos al curry'],
    ['chole', 'kondakadalai', 'கொண்டைக்கடலை'], []],
  ['GONGURA_MUTTON', 'curry', 'STN_CURRY', 1949, false, 3, 12, 'CURRY',
    ['Gongura Mutton', 'గోంగూర మటన్', 'gongura mutton', 'கோங்குரா மட்டன்', 'gongura mutton', 'chivo con gongura'],
    ['gongura mamsam'], []],
  ['CHK_CHETTINAD', 'curry', 'STN_CURRY', 1699, false, 3, 12, 'CURRY',
    ['Chicken Chettinad', 'చికెన్ చెట్టినాడ్', 'chicken chettinad',
     'செட்டிநாடு சிக்கன்', 'chettinad chicken', 'pollo chettinad'],
    ['chettinad kozhi', 'செட்டிநாடு கோழி'], ['tree_nuts']],
  ['CHK_65', 'starter', 'STN_FRY', 1299, false, 3, 9, 'DRY',
    ['Chicken 65', 'చికెన్ 65', 'chicken sixty five', 'சிக்கன் 65', 'chicken sixty five', 'pollo 65'],
    ['chicken 65'], ['gluten', 'egg']],
  ['GOBI_MAN', 'starter', 'STN_FRY', 1199, true, 2, 9, 'DRY',
    ['Gobi Manchurian', 'గోబీ మంచూరియా', 'gobi manchuria',
     'கோபி மஞ்சூரியன்', 'gobi manchurian', 'coliflor manchuriana'],
    ['cauliflower manchurian'], ['gluten', 'soy']],
  ['PNR_TIKKA', 'starter', 'STN_TANDOOR', 1399, true, 2, 14, 'DRY',
    ['Paneer Tikka', 'పనీర్ టిక్కా', 'paneer tikka', 'பனீர் டிக்கா', 'paneer tikka', 'paneer tikka'],
    [], ['dairy']],
  ['CHK_TIKKA', 'starter', 'STN_TANDOOR', 1499, false, 2, 14, 'DRY',
    ['Chicken Tikka', 'చికెన్ టిక్కా', 'chicken tikka', 'சிக்கன் டிக்கா', 'chicken tikka', 'pollo tikka'],
    [], ['dairy']],
  ['SAMOSA', 'starter', 'STN_FRY', 699, true, 1, 6, 'NONE',
    ['Samosa', 'సమోసా', 'samosa', 'சமோசா', 'samosa', 'samosas'],
    ['veg samosa'], ['gluten']],
  ['MASALA_DOSA', 'tiffin', 'STN_TIFFIN', 1149, true, 1, 8, 'TIFFIN',
    ['Masala Dosa', 'మసాలా దోశ', 'masala dosa', 'மசாலா தோசை', 'masala dosai', 'dosa masala'],
    ['dosa', 'dosai'], ['mustard']],
  ['IDLI', 'tiffin', 'STN_TIFFIN', 799, true, 0, 6, 'TIFFIN',
    ['Idli', 'ఇడ్లీ', 'idli', 'இட்லி', 'idli', 'idli'],
    ['idly'], ['mustard']],
  ['SAMBAR_VADA', 'tiffin', 'STN_TIFFIN', 849, true, 1, 6, 'TIFFIN',
    ['Sambar Vada', 'సాంబార్ వడ', 'sambar vada', 'சாம்பார் வடை', 'sambar vadai', 'vada con sambar'],
    ['vada sambar', 'medu vada'], ['mustard']],
  ['PESARATTU', 'tiffin', 'STN_TIFFIN', 1199, true, 1, 9, 'TIFFIN',
    ['Pesarattu', 'పెసరట్టు', 'pesarattu', 'பெசரட்டு', 'pesarattu', 'crepa de frijol mungo'],
    ['moong dosa'], []],
  ['GARLIC_NAAN', 'bread', 'STN_TANDOOR', 399, true, 0, 5, 'BREAD',
    ['Garlic Naan', 'గార్లిక్ నాన్', 'garlic naan', 'பூண்டு நான்', 'poondu naan', 'naan de ajo'],
    ['vellulli naan', 'వెల్లుల్లి నాన్'], ['dairy', 'gluten']],
  ['BUTTER_NAAN', 'bread', 'STN_TANDOOR', 349, true, 0, 5, 'BREAD',
    ['Butter Naan', 'బటర్ నాన్', 'butter naan', 'பட்டர் நான்', 'butter naan', 'naan con mantequilla'],
    ['naan'], ['dairy', 'gluten']],
  ['ROTI', 'bread', 'STN_TANDOOR', 299, true, 0, 4, 'BREAD',
    ['Tandoori Roti', 'తందూరి రోటీ', 'tandoori roti', 'தந்தூரி ரொட்டி', 'tandoori rotti', 'pan roti'],
    ['roti'], ['gluten']],
  ['CURD_RICE', 'rice', 'STN_TIFFIN', 899, true, 0, 4, 'NONE',
    ['Curd Rice', 'పెరుగు అన్నం', 'perugu annam', 'தயிர் சாதம்', 'thayir sadam', 'arroz con yogur'],
    ['curd rice', 'daddojanam'], ['dairy', 'mustard']],
  ['MANGO_LASSI', 'drink', 'STN_BAR', 499, true, 0, 2, 'NONE',
    ['Mango Lassi', 'మామిడి లస్సీ', 'mamidi lassi', 'மாம்பழ லஸ்ஸி', 'maambazha lassi', 'lassi de mango'],
    ['mango lassi'], ['dairy']],
  ['MASALA_CHAI', 'drink', 'STN_BAR', 299, true, 0, 3, 'NONE',
    ['Masala Chai', 'మసాలా చాయ్', 'masala chai', 'மசாலா டீ', 'masala tea', 'té chai'],
    ['chai', 'tea', 'టీ', 'டீ'], ['dairy']],
  ['FILTER_COFFEE', 'drink', 'STN_BAR', 349, true, 0, 3, 'NONE',
    ['Filter Coffee', 'ఫిల్టర్ కాఫీ', 'filter coffee', 'ஃபில்டர் காபி', 'filter kaapi', 'café de filtro'],
    ['kaapi'], ['dairy']],
  ['GULAB_JAMUN', 'dessert', 'STN_BAR', 549, true, 0, 2, 'NONE',
    ['Gulab Jamun', 'గులాబ్ జామూన్', 'gulab jamun', 'குலாப் ஜாமூன்', 'gulab jamun', 'gulab jamun'],
    [], ['dairy', 'gluten']],
  ['WATER', 'drink', 'STN_BAR', 199, true, 0, 1, 'NONE',
    ['Water', 'నీళ్ళు', 'neellu', 'தண்ணீர்', 'thanneer', 'agua'],
    ['water bottle', 'thanni', 'தண்ணி'], []],
]

const MODIFIERS: readonly (readonly [code: string, labels: Label4, aliases: readonly string[]])[] = [
  ['NO_ONION', ['No onion', 'ఉల్లిపాయ లేకుండా', 'வெங்காயம் இல்லாமல்', 'Sin cebolla'],
    ['without onion', 'without onions', 'onion vaddu', 'ullipaya lekunda', 'vengayam illama', 'sin cebolla']],
  ['NO_GARLIC', ['No garlic', 'వెల్లుల్లి లేకుండా', 'பூண்டு இல்லாமல்', 'Sin ajo'],
    ['without garlic', 'vellulli lekunda', 'poondu illama', 'sin ajo']],
  ['NO_CILANTRO', ['No cilantro', 'కొత్తిమీర లేకుండా', 'கொத்தமல்லி இல்லாமல்', 'Sin cilantro'],
    ['no coriander', 'kothimeera lekunda', 'kothamalli illama']],
  ['MILD', ['Mild', 'కారం తక్కువ', 'காரம் குறைவு', 'Poco picante'],
    ['less spicy', 'not spicy', 'karam takkuva', 'kaaram kammi', 'sin picante']],
  ['EXTRA_SPICY', ['Extra spicy', 'కారం ఎక్కువ', 'காரம் அதிகம்', 'Extra picante'],
    ['extra hot', 'karam ekkuva', 'kaaram adhigam', 'muy picante']],
  ['EXTRA_RAITA', ['Extra raita', 'ఎక్స్‌ట్రా రైతా', 'கூடுதல் ரைத்தா', 'Raita extra'],
    ['more raita']],
  ['LESS_OIL', ['Less oil', 'నూనె తక్కువ', 'எண்ணெய் குறைவு', 'Menos aceite'],
    ['noone takkuva', 'ennai kammi']],
  ['EXTRA_BUTTER', ['Extra butter', 'ఎక్స్‌ట్రా బటర్', 'கூடுதல் வெண்ணெய்', 'Extra mantequilla'],
    ['more butter', 'más mantequilla']],
]

const STATIONS: readonly (readonly [id: string, names: Label4, slots: number, lang: SpeakLang])[] = [
  ['STN_BIRYANI', ['Biryani', 'బిర్యానీ', 'பிரியாணி', 'Biryani'], 2, 'es'],
  ['STN_CURRY', ['Curry', 'కూర', 'கறி', 'Curry'], 3, 'es'],
  ['STN_TANDOOR', ['Tandoor', 'తందూర్', 'தந்தூர்', 'Tandoor'], 2, 'te'],
  ['STN_TIFFIN', ['Tiffin (dosa/idli)', 'టిఫిన్', 'டிபன்', 'Tiffin (dosa/idli)'], 2, 'ta'],
  ['STN_FRY', ['Fry', 'ఫ్రై', 'பொரியல்', 'Freidora'], 2, 'te'],
  ['STN_BAR', ['Drinks & desserts', 'పానీయాలు', 'பானங்கள்', 'Bebidas y postres'], 1, 'en'],
]

/** `config/i18n_kitchen.yaml`: the fixed KDS words, then the station names keyed by station id. */
const KITCHEN_LABELS: readonly (readonly [key: string, label: Label4])[] = [
  ['new', ['NEW', 'కొత్తది', 'புதியது', 'NUEVO']],
  ['changed', ['CHANGED', 'మార్పు', 'மாற்றம்', 'CAMBIADO']],
  ['eighty_six', ['86 — SOLD OUT', 'అయిపోయింది', 'தீர்ந்துவிட்டது', 'AGOTADO']],
  ['bump', ['BUMP', 'అయింది', 'முடிந்தது', 'LISTO']],
  ['recall', ['RECALL', 'వెనక్కి', 'திரும்ப', 'REGRESAR']],
  ['undo', ['UNDO', 'రద్దు చేయి', 'செயல்தவிர்', 'DESHACER']],
  ['seen', ['SEEN', 'చూశాను', 'பார்த்தேன்', 'VISTO']],
  ['notice', ['NOTICE', 'సూచన', 'அறிவிப்பு', 'AVISO']],
  ['quote', ['QUOTE', 'అంచనా సమయం', 'காத்திருப்பு நேரம்', 'TIEMPO ESTIMADO']],
  ['minutes', ['MIN', 'నిమిషాలు', 'நிமிடங்கள்', 'MIN']],
  ['STN_BIRYANI', ['Biryani', 'బిర్యానీ', 'பிரியாணி', 'Biryani']],
  ['STN_CURRY', ['Curry', 'కూర', 'கறி', 'Curry']],
  ['STN_TANDOOR', ['Tandoor', 'తందూర్', 'தந்தூர்', 'Tandoor']],
  ['STN_TIFFIN', ['Tiffin (dosa/idli)', 'టిఫిన్', 'டிபன்', 'Tiffin (dosa/idli)']],
  ['STN_FRY', ['Fry', 'ఫ్రై', 'பொரியல்', 'Freidora']],
  ['STN_BAR', ['Drinks & desserts', 'పానీయాలు', 'பானங்கள்', 'Bebidas y postres']],
]

type IngredientRow = readonly [id: string, unit: Unit, allergens: readonly Allergen[], par: number,
  reorder_point: number, pack_size: number, names: Partial<Record<SpeakLang, string>>]

const INGREDIENTS: readonly IngredientRow[] = [
  ['ING_PANEER', 'g', ['dairy'], 6000, 2000, 1000,
    { en: 'Paneer', te: 'పనీర్', ta: 'பனீர்', es: 'paneer (queso fresco)' }],
  ['ING_CHICKEN', 'g', [], 15000, 5000, 5000, { en: 'Chicken' }],
  ['ING_GOAT', 'g', [], 8000, 3000, 5000, { en: 'Goat' }],
  ['ING_EGG', 'ea', ['egg'], 90, 30, 30, { en: 'Egg' }],
  ['ING_BIRY_RICE', 'portion', [], 40, 12, 20, { en: 'Biry Rice' }],
  ['ING_MAKHANI_BASE', 'portion', ['dairy', 'tree_nuts'], 30, 10, 15, { en: 'Makhani Base' }],
  ['ING_DOSA_BATTER', 'portion', [], 50, 15, 25, { en: 'Dosa Batter' }],
  ['ING_SAMBAR', 'portion', ['mustard'], 60, 20, 30, { en: 'Sambar' }],
  ['ING_MIXED_VEG', 'g', [], 8000, 2500, 2000, { en: 'Mixed Veg' }],
  ['ING_SPINACH', 'g', [], 4000, 1200, 1000, { en: 'Spinach' }],
  ['ING_CAULIFLOWER', 'g', [], 5000, 1500, 2000, { en: 'Cauliflower' }],
  ['ING_POTATO', 'g', [], 8000, 2500, 5000, { en: 'Potato' }],
  ['ING_ONION', 'g', [], 10000, 3000, 5000, { en: 'Onion' }],
  ['ING_GARLIC', 'g', [], 1500, 500, 500, { en: 'Garlic' }],
  ['ING_GONGURA', 'g', [], 1500, 500, 500, { en: 'Gongura' }],
  ['ING_COCONUT', 'g', ['tree_nuts'], 1500, 500, 500, { en: 'Coconut' }],
  ['ING_YOGURT', 'g', ['dairy'], 8000, 2500, 2000, { en: 'Yogurt' }],
  ['ING_BUTTER', 'g', ['dairy'], 3000, 1000, 1000, { en: 'Butter' }],
  ['ING_CREAM', 'g', ['dairy'], 3000, 1000, 1000, { en: 'Cream' }],
  ['ING_MILK', 'ml', ['dairy'], 8000, 2500, 4000, { en: 'Milk' }],
  ['ING_CASHEW', 'g', ['tree_nuts'], 2000, 600, 500, { en: 'Cashew' }],
  ['ING_FLOUR', 'g', ['gluten'], 15000, 5000, 10000, { en: 'Flour' }],
  ['ING_LENTIL', 'g', [], 6000, 2000, 5000, { en: 'Lentil' }],
  ['ING_CHICKPEA', 'g', [], 4000, 1200, 2000, { en: 'Chickpea' }],
  ['ING_MOONG', 'g', [], 3000, 1000, 2000, { en: 'Moong' }],
  ['ING_RICE', 'g', [], 6000, 2000, 5000, { en: 'Rice' }],
  ['ING_SOY_SAUCE', 'ml', ['soy', 'gluten'], 1000, 300, 1000, { en: 'Soy Sauce' }],
  ['ING_MUSTARD', 'g', ['mustard'], 500, 150, 250, { en: 'Mustard' }],
  ['ING_MANGO_PULP', 'ml', [], 4000, 1200, 3000, { en: 'Mango Pulp' }],
  ['ING_TEA', 'g', [], 500, 150, 250, { en: 'Tea' }],
  ['ING_COFFEE', 'g', [], 1000, 300, 500, { en: 'Coffee' }],
  ['ING_GULAB_JAMUN', 'ea', ['dairy', 'gluten'], 60, 20, 30, { en: 'Gulab Jamun' }],
  ['ING_WATER_BOTTLE', 'ea', [], 96, 24, 24, { en: 'Water Bottle' }],
]

const label = ([en, te, ta, es]: Label4): LocalizedLabel => ({ en, te, ta, es })

export function spiceRouteItems(): MenuItem[] {
  return ITEMS.map(([code, category, station_id, price_cents, is_veg, spice_level, prep_min, set, n, aliases,
    allergens]) => ({
    code, category, station_id, price_cents, is_veg, spice_level,
    names: { en: n[0], te: n[1], te_roman: n[2], ta: n[3], ta_roman: n[4], es: n[5] },
    aliases: [...aliases], allowed_modifiers: [...SETS[set]], prep_min, available: true, unavailable_reason: null,
    alternatives: [], allergens: [...allergens],
  }))
}

export function spiceRouteModifiers(): Modifier[] {
  return MODIFIERS.map(([code, labels, aliases]) => ({ code, labels: label(labels), aliases: [...aliases] }))
}

export function spiceRouteStations(): Station[] {
  return STATIONS.map(([id, names, parallel_slots, default_language]) => ({
    id, names: label(names), parallel_slots, default_language,
  }))
}

export function spiceRouteKitchenLabels(): Record<string, LocalizedLabel> {
  return Object.fromEntries(KITCHEN_LABELS.map(([key, l]) => [key, label(l)]))
}

/** The tenant's menu as `repo.menu_snapshot` returns it for a fresh clone of the template (every item available). */
export function spiceRouteMenu(tenantId: string, menuVersion = 1): MenuSnapshot {
  return {
    tenant_id: tenantId, menu_version: menuVersion, menu_hash: SPICE_ROUTE_MENU_HASH, items: spiceRouteItems(),
    modifiers: spiceRouteModifiers(), stations: spiceRouteStations(), kitchen_labels: spiceRouteKitchenLabels(),
    tax_bp: SPICE_ROUTE_TAX_BP,
  }
}

/** The template's stock rows: on_hand = par, nothing reserved; a scenario then sets on_hand and reserved. */
export function spiceRouteIngredients(): Ingredient[] {
  return INGREDIENTS.map(([id, unit, allergens, par, reorder_point, pack_size, names]) => ({
    id, names: { ...names }, unit, allergens: [...allergens], on_hand: par, reserved: 0, par, reorder_point, pack_size,
  }))
}
