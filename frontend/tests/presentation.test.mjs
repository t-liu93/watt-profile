import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/lib/presentation.ts', import.meta.url), 'utf8')
const { measurement, timestamp, chartTooltip } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpile(source, { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext })).toString('base64')}`)
test('decimal presentation rounds the original digits, including carries and negative ties', () => {
 for (const [input, expected] of [['1.005', '1.01'], ['-1.005', '-1.01'], ['9.999', '10.00'], ['123456789012345678901234567890.125', '123456789012345678901234567890.13'], ['0.000000000000000000001', '0.00'], ['-0.0049', '0.00'], ['0', '0.00'], ['0.00', '0.00'], ['1.005e2', '100.50'], ['1e-999', '0.00']]) {
  assert.equal(measurement(input), expected)
  assert.equal(input, String(input))
 }
 assert.equal(measurement(null, 'Missing'), 'Missing')
 assert.equal(measurement(undefined, 'Missing'), 'Missing')
 assert.equal(measurement('1e999'), '1' + '0'.repeat(999) + '.00')
})
test('detected zones include positive, negative, non-whole-hour, UTC, DST and date rollover', () => {
 const original = Intl.DateTimeFormat
 const input = '2026-03-01T00:34:56.999999Z'
 try {
  for (const [zone, expected] of [['Asia/Tokyo', '2026-03-01 09:34:56'], ['America/New_York', '2026-02-28 19:34:56'], ['Asia/Kathmandu', '2026-03-01 06:19:56'], ['UTC', '2026-03-01 00:34:56']]) {
   Intl.DateTimeFormat = function(locale, options) { return new original(locale, options ?? { timeZone: zone }) }
   assert.equal(timestamp(input), expected)
   assert.equal(input, '2026-03-01T00:34:56.999999Z')
  }
  Intl.DateTimeFormat = function(locale, options) { return new original(locale, options ?? { timeZone: 'America/New_York' }) }
  assert.equal(timestamp('2026-03-08T06:59:59Z'), '2026-03-08 01:59:59')
  assert.equal(timestamp('2026-03-08T07:00:00Z'), '2026-03-08 03:00:00')
  const points = [{ axisValue: Date.parse(input), seriesName: 'Import', value: [0, 1.005] }]
  assert.equal(chartTooltip(points), '2026-02-28 19:34:56\nImport: 1.01')
  assert.equal(points[0].value[1], 1.005)
 } finally { Intl.DateTimeFormat = original }
 assert.equal(timestamp('2026-03-01'), '2026-03-01')
 assert.equal(timestamp(null, 'Missing'), 'Missing')
 assert.equal(timestamp(undefined), '—')
 assert.equal(timestamp('invalid'), 'invalid')
 assert.equal(timestamp('2026-03-01T12:34:56'), '2026-03-01T12:34:56')
})
test('missing, throwing and invalid detection/formatting normalize whole seconds to UTC', () => {
 const original = Intl.DateTimeFormat
 const cases = [undefined, () => { throw Error('unavailable') }, ...[undefined, '', 'Invalid/Zone'].map(timeZone => function(locale, options) { return options ? new original(locale, options) : { resolvedOptions: () => ({ timeZone }) } }), function(locale, options) { return options ? { formatToParts: () => { throw Error('failed') } } : new original(locale, { timeZone: 'UTC' }) }, function(locale, options) { return options ? { formatToParts: () => [] } : new original(locale, { timeZone: 'UTC' }) }]
 try {
  for (const replacement of cases) {
   Intl.DateTimeFormat = replacement
   const input = '2026-03-01T00:34:56.999999+02:00'
   assert.equal(timestamp(input), '2026-02-28T22:34:56Z')
   assert.equal(chartTooltip([{ axisValue: Date.parse(input), value: [0, 0] }]), '2026-02-28T22:34:56Z\n: 0.00')
  }
 } finally { Intl.DateTimeFormat = original }
})
