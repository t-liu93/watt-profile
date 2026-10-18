import type { AnalysisRange, SessionContext } from './analysis-models'
const zone = 'Europe/Amsterdam'
export function localDate(instant: string) { return new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(instant)) }
export function shiftDate(day: string, count: number) { const date = new Date(day + 'T12:00:00Z'); date.setUTCDate(date.getUTCDate() + count); return date.toISOString().slice(0, 10) }
export function midnight(day: string) {
  const utc = Date.parse(day + 'T00:00:00Z')
  let value = utc
  for (let i = 0; i < 3; i++) {
    const parts = new Intl.DateTimeFormat('en-GB', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(value)
    const p = Object.fromEntries(parts.map(part => [part.type, part.value]))
    value += utc - Date.parse(`${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}Z`)
  }
  return new Date(value).toISOString()
}
/** API instants have six fractional digits. Preserve the exclusive microsecond edge. */
export function micros(instant: string): bigint {
  const match = /^(.*T\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?(Z|[+-]\d\d:\d\d)$/.exec(instant)
  if (!match) throw new Error('invalid_analysis_range')
  return BigInt(Date.parse(match[1] + match[3])) * 1000n + BigInt((match[2] ?? '').padEnd(6, '0'))
}
export function fromMicros(value: bigint) { const fraction = ((value % 1000000n) + 1000000n) % 1000000n, seconds = (value - fraction) / 1000000n; return new Date(Number(seconds * 1000n)).toISOString().slice(0, 19) + '.' + fraction.toString().padStart(6, '0') + 'Z' }
export function recentRange(item: SessionContext): AnalysisRange | null {
  if (!item.latest_reading_received_at) return null
  const start = midnight(shiftDate(localDate(item.latest_reading_received_at), -6))
  return { received_from: micros(start) < micros(item.session.started_at) ? item.session.started_at : start, received_before: fromMicros(micros(item.latest_reading_received_at) + 1n) }
}
export function fullDays(first: string, last: string): AnalysisRange {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(first) || !/^\d{4}-\d{2}-\d{2}$/.test(last) || first > last || (Date.parse(last) - Date.parse(first)) / 86400000 > 30 || shiftDate(first, 0) !== first || shiftDate(last, 0) !== last) throw new Error('invalid_analysis_range')
  return { received_from: midnight(first), received_before: midnight(shiftDate(last, 1)) }
}
export function plotNumber(value: string | null): number | null { if (value === null) return null; const number = Number(value); return Number.isFinite(number) && !(number === 0 && /[1-9]/.test(value.split(/[eE]/)[0])) ? number : null }
export function zoomRange(range: AnalysisRange, start: number, end: number): AnalysisRange | null {
  const a = micros(range.received_from), b = micros(range.received_before), duration = b - a
  const lower = a + duration * BigInt(Math.round(Math.max(0, start) * 10000)) / 1000000n
  const upper = end >= 100 ? b : a + duration * BigInt(Math.round(Math.min(100, end) * 10000)) / 1000000n
  return lower < upper ? { received_from: start <= 0 ? range.received_from : fromMicros(lower), received_before: end >= 100 ? range.received_before : fromMicros(upper) } : null
}
