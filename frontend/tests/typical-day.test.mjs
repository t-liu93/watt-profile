import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const moduleURL = code => `data:text/javascript;base64,${Buffer.from(ts.transpile(code, { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext })).toString('base64')}`
const timeURL = moduleURL(await readFile(new URL('../src/lib/analysis-time.ts', import.meta.url), 'utf8'))
const presentationURL = moduleURL(await readFile(new URL('../src/lib/presentation.ts', import.meta.url), 'utf8'))
const source = (await readFile(new URL('../src/lib/typical-day-chart.ts', import.meta.url), 'utf8')).replace("'./analysis-time'", JSON.stringify(timeURL)).replace("'./presentation'", JSON.stringify(presentationURL))
const { typicalDayOption } = await import(moduleURL(source))
test('each date group preserves exact mean/contributors, zero, null breaks and full 96 slots', () => {
 for (const count of [1, 2, 3]) {
  const option = typicalDayOption([
   { local_time: '00:00:00', metric: 'power_import_kw', mean: '0', contributing_date_count: count },
   { local_time: '00:30:00', metric: 'power_import_kw', mean: '1.1234567890123456789', contributing_date_count: count + 1 },
   { local_time: '00:00:00', metric: 'power_export_kw', mean: '1e999', contributing_date_count: 1 },
  ], ['Import', 'Export'], 'Contributing dates', 'Unavailable', '#fff')
  assert.equal(option.xAxis.data.length, 96)
  const [incoming, outgoing] = option.series
  assert.equal(incoming.connectNulls, false)
  assert.equal(incoming.data.length, 96)
  assert.equal(outgoing.data.length, 96)
  assert.equal(incoming.data[0].value, 0)
  assert.equal(incoming.data[1].value, null)
  assert.equal(incoming.data[2].exact, '1.1234567890123456789')
  assert.equal(incoming.data[2].contributing_date_count, count + 1)
  assert.match(incoming.tooltip.valueFormatter(0, 0), new RegExp(`Contributing dates: ${count}`))
  assert.equal(outgoing.data[0].value, null)
  assert.equal(outgoing.data[0].exact, '1e999')
 }
})
test('theme and translated labels redraw without changing plotted or exact values; empty group stays null', () => {
 const slots = [{ local_time: '23:45:00', metric: 'power_export_kw', mean: '2.34', contributing_date_count: 2 }]
 const english = typicalDayOption(slots, ['Import', 'Export'], 'Dates', 'Unavailable', '#fff')
 const chinese = typicalDayOption(slots, ['输入', '输出'], '日期', '不可用', '#000')
 assert.deepEqual(english.series.map(series => series.data), chinese.series.map(series => series.data))
 assert.equal(chinese.series[1].data[95].value, 2.34)
 for (const series of typicalDayOption([], ['I', 'E'], 'Dates', 'Unavailable', '#fff').series) assert.ok(series.data.every(slot => slot.value === null && slot.contributing_date_count === 0))
})
