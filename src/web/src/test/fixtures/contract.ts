/**
 * A runtime check of a value against the generated contract types, read from the source text of
 * `src/contracts/generated/*.ts` (scripts/gen_ts_contracts.py writes one field per line, so a small reader suffices).
 *
 * TypeScript already checks typed fixtures at compile time. This catches what a cast or a JSON file hides: a missing
 * field, `undefined` where the API sends `null`, an unknown enum value, an extra key, a non-finite number. It
 * understands what the generator emits: primitives, literals and their unions, named interfaces and aliases,
 * `T[]`, `(A | B)[]`, `Record<string, T>`, `Partial<Record<K, T>>` and tuples. Test-only.
 *
 *     expect(contractErrors(payload, 'TenantState')).toEqual([])
 */

const SOURCES = import.meta.glob<string>('../../contracts/generated/*.ts', {
  query: '?raw',
  import: 'default',
  eager: true,
})

interface Registry {
  interfaces: Map<string, Map<string, string>>
  aliases: Map<string, string>
}

let registry: Registry | null = null

function load(): Registry {
  if (registry) return registry
  const interfaces = new Map<string, Map<string, string>>()
  const aliases = new Map<string, string>()
  for (const text of Object.values(SOURCES)) {
    const lines = text.split('\n')
    for (let i = 0; i < lines.length; i++) {
      const iface = /^export interface (\w+) \{$/.exec(lines[i])
      if (iface) {
        const fields = new Map<string, string>()
        for (i++; i < lines.length && lines[i] !== '}'; i++) {
          const field = /^ {2}([\w$]+): (.+)$/.exec(lines[i])
          if (field) fields.set(field[1], field[2].trim())
        }
        interfaces.set(iface[1], fields)
        continue
      }
      const one = /^export type (\w+) = (.+)$/.exec(lines[i])
      if (one && !one[2].startsWith('{')) {
        aliases.set(one[1], one[2].trim())
        continue
      }
      const multi = /^export type (\w+) =$/.exec(lines[i])
      if (multi) {
        const parts: string[] = []
        while (i + 1 < lines.length && /^ {2}\| /.test(lines[i + 1])) parts.push(lines[++i].slice(4).trim())
        aliases.set(multi[1], parts.join(' | '))
      }
    }
  }
  return (registry = { interfaces, aliases })
}

/** Split at top-level `sep` (outside <>, (), [] and quotes). */
function splitTop(type: string, sep: string): string[] {
  const parts: string[] = []
  let depth = 0
  let quote = false
  let start = 0
  for (let i = 0; i < type.length; i++) {
    const ch = type[i]
    if (ch === "'" && type[i - 1] !== '\\') quote = !quote
    if (quote) continue
    if ('<([{'.includes(ch)) depth++
    else if ('>)]}'.includes(ch)) depth--
    else if (depth === 0 && type.startsWith(sep, i)) {
      parts.push(type.slice(start, i).trim())
      start = i + sep.length
      i += sep.length - 1
    }
  }
  parts.push(type.slice(start).trim())
  return parts
}

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

function show(v: unknown): string {
  if (v === undefined || (typeof v === 'number' && !Number.isFinite(v))) return String(v)
  const text = JSON.stringify(v)
  return text.length > 60 ? `${text.slice(0, 57)}...` : text
}

/** Problems with `value` as `type` at `path`, appended to `out`. */
function check(value: unknown, type: string, path: string, out: string[]): void {
  const reg = load()
  type = type.trim()
  const union = splitTop(type, ' | ')
  if (union.length > 1) {
    if (value === null && union.includes('null')) return
    const options = union.filter((t) => t !== 'null')
    // `Model | null` holding an object: report its inner problems, not just "expected Model | null"
    if (options.length === 1 && typeof value === 'object' && value !== null) return check(value, options[0], path, out)
    if (options.some((t) => errorsOf(value, t, path).length === 0)) return
    out.push(`${path}: expected ${type}, got ${show(value)}`)
    return
  }
  const fail = () => out.push(`${path}: expected ${type}, got ${show(value)}`)
  if (type.startsWith('(') && type.endsWith(')')) return check(value, type.slice(1, -1), path, out)
  if (type.endsWith('[]')) {
    if (!Array.isArray(value)) return void fail()
    value.forEach((item, i) => check(item, type.slice(0, -2), `${path}[${i}]`, out))
    return
  }
  if (type.startsWith('[') && type.endsWith(']')) {
    const items = splitTop(type.slice(1, -1), ', ')
    if (!Array.isArray(value) || value.length !== items.length) return void fail()
    items.forEach((t, i) => check(value[i], t, `${path}[${i}]`, out))
    return
  }
  if (type.startsWith("'")) return void (value === type.slice(1, -1).replace(/\\'/g, "'") || fail())
  if (/^-?\d+(\.\d+)?$/.test(type)) return void (value === Number(type) || fail())
  switch (type) {
    case 'null': return void (value === null || fail())
    case 'string': return void (typeof value === 'string' || fail())
    case 'number': return void ((typeof value === 'number' && Number.isFinite(value)) || fail())
    case 'boolean': return void (typeof value === 'boolean' || fail())
    case 'true': return void (value === true || fail())
    case 'false': return void (value === false || fail())
    case 'unknown': return void (value !== undefined || fail())
    case 'never': return void fail()
  }
  const partial = /^Partial<Record<(.+), (.+)>>$/.exec(type)
  const record = partial ?? /^Record<string, (.+)>$/.exec(type)
  if (record) {
    if (!isPlainObject(value)) return void fail()
    const [keyType, valueType] = partial ? [partial[1], partial[2]] : ['string', record[1]]
    for (const [k, v] of Object.entries(value)) {
      if (keyType !== 'string') check(k, keyType, `${path} key`, out)
      check(v, valueType, `${path}.${k}`, out)
    }
    return
  }
  const fields = reg.interfaces.get(type)
  if (fields) {
    if (!isPlainObject(value)) return void fail()
    for (const [name, fieldType] of fields) {
      if (!(name in value)) out.push(`${path}.${name}: missing (${type})`)
      else check(value[name], fieldType, `${path}.${name}`, out)
    }
    for (const key of Object.keys(value)) {
      if (!fields.has(key)) out.push(`${path}.${key}: not a field of ${type}`)
    }
    return
  }
  const alias = reg.aliases.get(type)
  if (alias === undefined) return void out.push(`${path}: unknown contract type ${type}`)
  const inner = errorsOf(value, alias, path)
  // a value outside an enum reads better by the enum's name than by its expansion
  if (inner.length === 1 && inner[0].startsWith(`${path}: expected `)) fail()
  else out.push(...inner)
}

function errorsOf(value: unknown, type: string, path: string): string[] {
  const out: string[] = []
  check(value, type, path, out)
  return out
}

/** Every way `value` breaks the generated type `typeName` (empty when it conforms). */
export function contractErrors(value: unknown, typeName: string): string[] {
  return errorsOf(value, typeName, typeName)
}

/** Throws the first problems when `value` does not conform to `typeName`. */
export function assertContract(value: unknown, typeName: string): void {
  const errors = contractErrors(value, typeName)
  if (errors.length) throw new Error(`${typeName} breaks the contract:\n  ${errors.slice(0, 20).join('\n  ')}`)
}

/** The fields of a generated interface, in declaration order (for tests of the checker itself). */
export function contractFields(typeName: string): string[] {
  return [...(load().interfaces.get(typeName)?.keys() ?? [])]
}
