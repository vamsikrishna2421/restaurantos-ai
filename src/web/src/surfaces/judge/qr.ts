/**
 * A small QR Code encoder (ISO/IEC 18004) for the join links: byte mode, error correction level M, versions 1–40,
 * the eight masks scored by the standard penalty rules. The join API returns a URL only (no image) and the web build
 * has no QR dependency, so the codes are drawn here. Pure and deterministic; `qr.test.ts` checks it module for module
 * against segno (the server's QR library).
 *
 * The structure follows Project Nayuki's reference implementation (MIT): function patterns, Reed–Solomon blocks
 * interleaved, the zigzag data placement, then the mask with the lowest penalty.
 */

/** ECC codewords per block and the number of blocks, level M, by version (index 0 unused). */
const ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28,
  28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
const BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26,
  28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]
const FORMAT_LEVEL_M = 0 // the two ECC-level bits of the format word (L=1, M=0, Q=3, H=2)

export interface QrMatrix {
  version: number
  mask: number
  size: number
  /** `modules[y][x]`: true is a dark module. No quiet zone. */
  modules: boolean[][]
}

const bit = (value: number, i: number) => ((value >>> i) & 1) !== 0

/** Data-capable modules of a version (everything but the function patterns and the format/version areas). */
function rawModules(ver: number): number {
  let result = (16 * ver + 128) * ver + 64
  if (ver >= 2) {
    const align = Math.floor(ver / 7) + 2
    result -= (25 * align - 10) * align - 55
    if (ver >= 7) result -= 36
  }
  return result
}

const dataCodewords = (ver: number) => Math.floor(rawModules(ver) / 8) - ECC_PER_BLOCK[ver] * BLOCKS[ver]

// ----- Reed–Solomon over GF(2^8), x^8 + x^4 + x^3 + x^2 + 1 ---------------------------------------------------------

function gfMul(x: number, y: number): number {
  let z = 0
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d)
    z ^= ((y >>> i) & 1) * x
  }
  return z
}

function rsDivisor(degree: number): number[] {
  const result = new Array<number>(degree).fill(0)
  result[degree - 1] = 1
  let root = 1
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      result[j] = gfMul(result[j], root)
      if (j + 1 < degree) result[j] ^= result[j + 1]
    }
    root = gfMul(root, 0x02)
  }
  return result
}

function rsRemainder(data: readonly number[], divisor: readonly number[]): number[] {
  const result = divisor.map(() => 0)
  for (const b of data) {
    const factor = b ^ (result.shift() as number)
    result.push(0)
    divisor.forEach((coef, i) => {
      result[i] ^= gfMul(coef, factor)
    })
  }
  return result
}

/** The data codewords split into blocks, each with its ECC, interleaved as the symbol stores them. */
function withEcc(data: readonly number[], ver: number): number[] {
  const blocks = BLOCKS[ver]
  const eccLen = ECC_PER_BLOCK[ver]
  const raw = Math.floor(rawModules(ver) / 8)
  const shortBlocks = blocks - (raw % blocks)
  const shortLen = Math.floor(raw / blocks)
  const divisor = rsDivisor(eccLen)
  const all: number[][] = []
  for (let i = 0, k = 0; i < blocks; i++) {
    const dat = data.slice(k, k + shortLen - eccLen + (i < shortBlocks ? 0 : 1))
    k += dat.length
    const ecc = rsRemainder(dat, divisor)
    if (i < shortBlocks) dat.push(0)
    all.push([...dat, ...ecc])
  }
  const out: number[] = []
  for (let i = 0; i < all[0].length; i++) {
    all.forEach((block, j) => {
      if (i !== shortLen - eccLen || j >= shortBlocks) out.push(block[i]) // skip the short blocks' padding byte
    })
  }
  return out
}

// ----- the symbol ---------------------------------------------------------------------------------------------------

function alignmentPositions(ver: number, size: number): number[] {
  if (ver === 1) return []
  const count = Math.floor(ver / 7) + 2
  const step = Math.floor((ver * 8 + count * 3 + 5) / (count * 4 - 4)) * 2
  const result = [6]
  for (let pos = size - 7; result.length < count; pos -= step) result.splice(1, 0, pos)
  return result
}

class QrSymbol {
  readonly ver: number
  readonly size: number
  readonly modules: boolean[][]
  readonly fn: boolean[][]

  constructor(ver: number) {
    this.ver = ver
    this.size = ver * 4 + 17
    this.modules = Array.from({ length: this.size }, () => new Array<boolean>(this.size).fill(false))
    this.fn = Array.from({ length: this.size }, () => new Array<boolean>(this.size).fill(false))
    this.drawFunctionPatterns()
  }

  private set(x: number, y: number, dark: boolean): void {
    this.modules[y][x] = dark
    this.fn[y][x] = true
  }

  private drawFunctionPatterns(): void {
    const { size } = this
    for (let i = 0; i < size; i++) {
      this.set(6, i, i % 2 === 0)
      this.set(i, 6, i % 2 === 0)
    }
    for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const dist = Math.max(Math.abs(dx), Math.abs(dy))
          const x = cx + dx
          const y = cy + dy
          if (x >= 0 && x < size && y >= 0 && y < size) this.set(x, y, dist !== 2 && dist !== 4)
        }
      }
    }
    const pos = alignmentPositions(this.ver, size)
    const n = pos.length
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue // the finders
        for (let dy = -2; dy <= 2; dy++) {
          for (let dx = -2; dx <= 2; dx++) this.set(pos[i] + dx, pos[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1)
        }
      }
    }
    this.drawFormat(0) // reserves the area; the real bits follow once the mask is chosen
    if (this.ver >= 7) {
      let rem = this.ver
      for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25)
      const bits = (this.ver << 12) | rem
      for (let i = 0; i < 18; i++) {
        const a = size - 11 + (i % 3)
        const b = Math.floor(i / 3)
        this.set(a, b, bit(bits, i))
        this.set(b, a, bit(bits, i))
      }
    }
  }

  drawFormat(mask: number): void {
    const data = (FORMAT_LEVEL_M << 3) | mask
    let rem = data
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537)
    const bits = ((data << 10) | rem) ^ 0x5412
    const { size } = this
    for (let i = 0; i <= 5; i++) this.set(8, i, bit(bits, i))
    this.set(8, 7, bit(bits, 6))
    this.set(8, 8, bit(bits, 7))
    this.set(7, 8, bit(bits, 8))
    for (let i = 9; i < 15; i++) this.set(14 - i, 8, bit(bits, i))
    for (let i = 0; i < 8; i++) this.set(size - 1 - i, 8, bit(bits, i))
    for (let i = 8; i < 15; i++) this.set(8, size - 15 + i, bit(bits, i))
    this.set(8, size - 8, true) // the dark module
  }

  drawCodewords(data: readonly number[]): void {
    const { size } = this
    let i = 0
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5 // skip the vertical timing column
      for (let vert = 0; vert < size; vert++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j
          const y = ((right + 1) & 2) === 0 ? size - 1 - vert : vert
          if (!this.fn[y][x] && i < data.length * 8) {
            this.modules[y][x] = bit(data[i >>> 3], 7 - (i & 7))
            i++
          }
        }
      }
    }
  }

  /** XOR the mask over the data modules (applying it twice undoes it). */
  applyMask(mask: number): void {
    for (let y = 0; y < this.size; y++) {
      for (let x = 0; x < this.size; x++) {
        if (!this.fn[y][x] && MASKS[mask](x, y)) this.modules[y][x] = !this.modules[y][x]
      }
    }
  }

  penalty(): number {
    const { size, modules } = this
    let result = 0
    const line = (get: (a: number, b: number) => boolean) => {
      for (let a = 0; a < size; a++) {
        let color = false
        let run = 0
        const history = [0, 0, 0, 0, 0, 0, 0]
        for (let b = 0; b < size; b++) {
          if (get(a, b) === color) {
            run++
            if (run === 5) result += 3
            else if (run > 5) result++
          } else {
            addHistory(run, history, size)
            if (!color) result += finderLike(history) * 40
            color = get(a, b)
            run = 1
          }
        }
        if (color) {
          addHistory(run, history, size)
          run = 0
        }
        addHistory(run + size, history, size)
        result += finderLike(history) * 40
      }
    }
    line((y, x) => modules[y][x])
    line((x, y) => modules[y][x])
    let dark = 0
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (modules[y][x]) dark++
        const c = modules[y][x]
        if (y < size - 1 && x < size - 1 && c === modules[y][x + 1] && c === modules[y + 1][x] &&
          c === modules[y + 1][x + 1]) result += 3
      }
    }
    const total = size * size
    return result + (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10
  }
}

function addHistory(run: number, history: number[], size: number): void {
  if (history[0] === 0) run += size // the light border before the first run
  history.pop()
  history.unshift(run)
}

/** 1:1:3:1:1 runs with 4 light modules on one side: a pattern a reader could take for a finder. */
function finderLike(h: readonly number[]): number {
  const n = h[1]
  const core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n
  return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0)
}

const MASKS: readonly ((x: number, y: number) => boolean)[] = [
  (x, y) => (x + y) % 2 === 0,
  (_x, y) => y % 2 === 0,
  (x) => x % 3 === 0,
  (x, y) => (x + y) % 3 === 0,
  (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
  (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
  (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
  (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
]

/** Encode `text` (UTF-8, byte mode, level M) in the smallest version that holds it; `mask` forces a mask (tests). */
export function encodeQr(text: string, opts: { mask?: number; version?: number } = {}): QrMatrix {
  const bytes = new TextEncoder().encode(text)
  const fits = (v: number) => 4 + (v <= 9 ? 8 : 16) + bytes.length * 8 <= dataCodewords(v) * 8
  let ver = opts.version ?? 1
  while (ver <= 40 && !fits(ver)) ver++
  if (ver > 40) throw new RangeError('Text too long for a QR code')

  const bits: number[] = []
  const put = (value: number, len: number) => {
    for (let i = len - 1; i >= 0; i--) bits.push((value >>> i) & 1)
  }
  put(0b0100, 4)
  put(bytes.length, ver <= 9 ? 8 : 16)
  for (const b of bytes) put(b, 8)
  const capacity = dataCodewords(ver) * 8
  put(0, Math.min(4, capacity - bits.length))
  put(0, (8 - (bits.length % 8)) % 8)
  for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) put(pad, 8)
  const data: number[] = []
  for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((acc, b) => (acc << 1) | b, 0))

  const sym = new QrSymbol(ver)
  sym.drawCodewords(withEcc(data, ver))
  let mask = opts.mask ?? -1
  if (mask < 0) {
    let best = Infinity
    for (let m = 0; m < 8; m++) {
      sym.applyMask(m)
      sym.drawFormat(m)
      const score = sym.penalty()
      if (score < best) {
        best = score
        mask = m
      }
      sym.applyMask(m)
    }
  }
  sym.applyMask(mask)
  sym.drawFormat(mask)
  return { version: ver, mask, size: sym.size, modules: sym.modules }
}

/** The symbol as one SVG path (dark modules, row runs), inside a 4-module quiet zone. */
export function qrPath(qr: QrMatrix, quiet = 4): string {
  const parts: string[] = []
  qr.modules.forEach((row, y) => {
    for (let x = 0; x < qr.size; x++) {
      if (!row[x]) continue
      let run = 1
      while (x + run < qr.size && row[x + run]) run++
      parts.push(`M${x + quiet} ${y + quiet}h${run}v1h-${run}z`)
      x += run - 1
    }
  })
  return parts.join('')
}
