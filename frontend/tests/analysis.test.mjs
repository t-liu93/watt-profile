import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/lib/analysis-time.ts', import.meta.url), 'utf8')
const { micros, fromMicros, fullDays, recentRange, plotNumber, zoomRange } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpile(source, { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext })).toString('base64')}`)
test('Amsterdam full days preserve spring/autumn durations and 31-date bound', () => {
 for (const [day, hours] of [['2026-03-29', 23], ['2026-10-25', 25]]) { const range = fullDays(day, day); assert.equal(micros(range.received_before) - micros(range.received_from), BigInt(hours) * 3600000000n) }
 assert.doesNotThrow(() => fullDays('2026-03-01', '2026-03-31'))
 for (const args of [['2026-03-01', '2026-04-01'], ['2026-03-30', '2026-03-29'], ['2026-02-30', '2026-03-01']]) assert.throws(() => fullDays(...args))
})
test('recent range clips session start and includes exact latest + one microsecond; zoom right edge remains exact', () => {
 const item = { session: { started_at: '2026-03-01T12:34:56.123456Z' }, latest_reading_received_at: '2026-03-02T23:59:59.999999Z' }
 const range = recentRange(item)
 assert.equal(range.received_from, item.session.started_at)
 assert.equal(range.received_before, '2026-03-03T00:00:00.000000Z')
 const latest = '2026-03-02T12:00:00.123456Z'
 const exact = recentRange({ ...item, latest_reading_received_at: latest })
 assert.equal(exact.received_before, '2026-03-02T12:00:00.123457Z')
 assert.equal(zoomRange(exact, 80, 100).received_before, exact.received_before)
 assert.equal(fromMicros(micros(latest)), latest)
 assert.equal(fromMicros(-1n), '1969-12-31T23:59:59.999999Z')
 assert.equal(recentRange({ ...item, latest_reading_received_at: null }), null)
})
test('chart conversion distinguishes missing, real zero, overflow and underflow while preserving decimal source', () => {
 assert.equal(plotNumber(null), null); assert.equal(plotNumber('0'), 0); assert.equal(plotNumber('0.00'), 0)
 assert.equal(plotNumber('1e999'), null); assert.equal(plotNumber('1e-999'), null); assert.equal(plotNumber('0e-999'), 0); assert.equal(plotNumber('0.000001'), 0.000001)
})
