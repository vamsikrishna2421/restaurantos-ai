// @vitest-environment jsdom
// The QR encoder against segno 1.6.6 (the server's QR library): the same text, version, level M and mask must give
// the same modules. Reference hex = the rows concatenated, dark = 1, most significant bit first, from
// segno.make_qr(text, error='m', mode='byte', boost_error=False, mask=m) with segno's padding-bit step set to the
// standard's (ISO 18004 7.4.10: zero bits only up to the next codeword boundary; segno adds a whole 0x00 codeword
// when the stream is already aligned, which byte mode always is). Mask *choice* may differ between libraries (the
// penalty rules are read differently), so only HELLO's, where both pick mask 4, is compared.
import { expect, test } from 'vitest'
import { encodeQr, qrPath, type QrMatrix } from './qr'

const HELLO = [
  'fe6bfc16506e82bb7465dbacaec12507faafe00700aa50952318a2fb7f63c12b38fa007a1bf8c6f04f21baf2a5d09daeaaab04612fedb38',
  'febbfc10d06eb6bb74c5dba1aec14d07faafe00d00a3012f89b237ae2bc96b8e6daf0050b3fb93b0458bba27f5d2372ebfff04cb8fe8e68',
  'fe0bfc12906ebabb7555dbaaaec16907faafe01400be33e30bfb2cc39ce9f9c8b6c2807423f948d05cafba912dd47e2eb2490459cfeb8b0',
  'fe8bfc14506e80bb7555dba72ec11d07faafe01f00b75a5b0bfb01f5475b22a8b6c2805997fafe105cafba4a45d713aeb24904ef1fed500',
  'fecbfc10906e94bb7525dbaeaec14907faafe013008bf7c885c3cfcda5f1880faab30057affbab504bb3bad635d246ee9c7104280feffa8',
  'fe3bfc14906ebabb7595dba2aec10907faafe01c0082b672cce76cc39cc97bce6daf0054a3f948d040deba112dd2762e9fff04dbcfeb8b0',
  'febbfc14906eb2bb7495dbabaec11107faafe00c009f94bacce765e70ed11a4e6daf0057affbda9050deba8365d7f02e9fff04d7ffe9c20',
  'fe6bfc13506e86bb7465dba6aec16d07faafe0030096c5052318b0b25b2ae5bb38fa006853f88fd05f21ba5635d40fee8aab04280fec970',
]
const URL_V10_MASK5 =
  'fe5f3eba2ef33fc14c1224d3a4906eb6eb1bd06fcbb759a6db53b725dba1217bf40712ec12360b1313b107faaaaaaaaa' +
  'aafe01464ac5ca500082ccfc7f785767148edbcf237bfda1b4b081940ffd15263042195b10d821bd19efadbce6879ccb' +
  'b2abac046b1af8d3a60ffc91fba28e8a6975ea3fb1023c01651089a45366f44f8da62d4bd292273a11617995149be774' +
  '89b011b33003008c5f237fbf44bef7346ae2bc4c43520cb00ff2cc8f6da5aa6dafa8b42c0fdb6efae5a3ef1a6ff8f1ff' +
  '53137fd464c2f1deaec482a0e01abbc4218f14e5a4623fe963fcd447e393e24402adf07e7ffa71d91e74d2aae2734d37' +
  'b32d4e8d3e7baf1716f92112d3865631cda68f0fabeaf7fdee2dede643aaac8869a279984f61c0d5d1a4fe0e1f0fcbf2' +
  '513b76be324beac8e2329bbf8af7409732ffb236db442d9d35ee792863a5a526dbaf934dffbf58f29a6698880fcd45f2' +
  '40afdeed406c034405becee9fa807a3ff1b676c77f9a726a94d8ead04d41347ce0d1eba40a0ff7822fbdd199eb28aba5' +
  '62e978d5b55c7237041d46d703839cfed22d183a15210'
const LONG_V13_MASK2 =
  'fe7f1c3134b5cf7bfc11fd5833d8a742106eafea37caaf0bb8bb75817436886b8415dbac57dc0f870d72aec14864f545' +
  '3f5c7107faaaaaaaaaaaaaafe016b096713688d900be70e06efc75df53e00362483225209ee7f4f3fdc17ed9ac1cdcd2' +
  'f802ce1b58379b6f2353e9f7126f35d1a895f182e23863be185b1cbebd69da9cb8c8b0584ee1398bb93c4f145a7a6c75' +
  'c6c063e65eac81030630516ab074c8b775bf07e93201ff4402d615b524e3c8dbf6ef5e734508ce8e13b2d8a48e4668b3' +
  '498b2a1aa8ead8b3a33ba155f5da9c1edce1ce2c25c25c75ee638b290384b8ca72b98a52ae9aae3afd8ac8c6d61344bd' +
  '93ef3d1bd0f2c5ae7464724259803a74f9e6bd997d86526d6ea1eb892fee8b02b5e37bb6dfb3f7f5f921e36f8044880f' +
  '54638eb8c562a820ffaa85cddeaf31f1e11f1b55b9b1c5f80759afc36474fe12095d68f411efca153c029b8a6e53d1b5' +
  '1d19f528631563d7eea7dcb21ec815f641d188c20b3f678a983551c2d580b721ac594cf85425395d9f55e4c9fca026e1' +
  'c05db4293e1c946276a84b8217cdb8502d1eaadac2ca13b03d71b54378562e3b528f4c52c54885e2515e0c4bcebf6156' +
  'fa51aca8b0eb6b4152ce1e747f7eb5b7e7ea8167a0a3183e58c06dc0d097b8eb40161ad5384bc45cc1b4caa50b010385' +
  '3b5a535facae1f1531c7498303c6580f41e4ec21ebf96c04a6c1ac1ad501bdf56631583d62e9a92c77ef802964fd004f' +
  '0330c678e98c57f9a5a8f6b25f45aad05bd6ba71d6035d1fbaf0de9efc21834f85d64e798a6b85b80d2ea2bcecdbe0fd' +
  'ab89047f9eed8b98177b4feaf45c32c33c52310'

const URL = 'https://restaurantos.fly.dev/join/eyJuIjoiN3FXcTNfa1pGdkJ0IiwidGlkIjoic2J4XzAxTTVaODlSODBHMko1M0pEV0UwRjdYOT' +
  'BTIiwiZXhwIjoxNzkzMTI0NjAwMDAwfQ.q3J0Yk1mX2h4b0lQZ2dJcDh3c0V4a1VnVnB1cWJ0c2I?next=%2Fkds'
const LONG = 'Spice Route (sample) '.repeat(14) + 'తెలుగు'

function hex(qr: QrMatrix): string {
  const bits = qr.modules.flat().map((dark) => (dark ? '1' : '0')).join('')
  const padded = bits + '0'.repeat((4 - (bits.length % 4)) % 4)
  let out = ''
  for (let i = 0; i < padded.length; i += 4) out += parseInt(padded.slice(i, i + 4), 2).toString(16)
  return out
}

test('qr_matches_segno_v1_every_mask', () => {
  HELLO.forEach((ref, mask) => {
    const qr = encodeQr('HELLO WORLD', { mask })
    expect(qr.version).toBe(1)
    expect(qr.size).toBe(21)
    expect(hex(qr)).toBe(ref)
  })
})

test('qr_matches_segno_join_url_v10', () => {
  const qr = encodeQr(URL, { mask: 5 })
  expect([qr.version, qr.size]).toEqual([10, 57]) // 196 bytes: a 16-bit count, 5 blocks, version info
  expect(hex(qr)).toBe(URL_V10_MASK5)
})

test('qr_matches_segno_utf8_short_and_long_blocks_v13', () => {
  const qr = encodeQr(LONG, { mask: 2 })
  expect([qr.version, qr.size]).toEqual([13, 69]) // 312 bytes: 8 short blocks and 1 long one
  expect(hex(qr)).toBe(LONG_V13_MASK2)
})

test('qr_picks_a_mask_and_draws_a_path', () => {
  const qr = encodeQr('HELLO WORLD')
  expect(qr.mask).toBe(4) // the lowest penalty, as segno also finds
  expect(hex(qr)).toBe(HELLO[4])
  expect(qrPath(qr)).toMatch(/^M4 4h7v1h-7z/) // the top-left finder starts inside the quiet zone
  expect(() => encodeQr('x'.repeat(2400))).toThrow(RangeError)
})
