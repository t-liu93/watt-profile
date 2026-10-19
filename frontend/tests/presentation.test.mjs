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
test('visible time truncates fractions without altering UTC/offset or machine instant', () => {
 const input = '2026-03-01T12:34:56.999999Z'
 assert.equal(timestamp(input), '2026-03-01T12:34:56Z')
 assert.equal(input, '2026-03-01T12:34:56.999999Z')
 assert.equal(timestamp('2026-03-01T12:34:56.123456+02:00'), '2026-03-01T12:34:56+02:00')
 assert.equal(timestamp('2026-03-01'), '2026-03-01')
 assert.equal(timestamp(null, 'Missing'), 'Missing')
})
test('chart tooltip uses seconds and fixed decimals while leaving numeric chart points intact', () => {
 const points = [{ axisValue: Date.parse('2026-03-01T12:34:56.123Z'), seriesName: 'Import', value: [0, 1.005] }]
 assert.match(chartTooltip(points), /13:34:56 Europe\/Amsterdam\nImport: 1\.01$/)
 assert.equal(points[0].value[1], 1.005)
})
