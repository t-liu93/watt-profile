/** Presentation only: never feed rounded values back into queries or calculations. */
export function measurement(value: string | number | null | undefined, missing = '—'): string {
 if (value === null || value === undefined) return missing
 const source = String(value), match = /^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(source)
 if (!match) return source
 const digits = match[2] + (match[3] ?? ''), places = match[2].length + Number(match[4] ?? 0) + 2
 // Decimal strings are rounded half away from zero, without a Number conversion.
 let cents: bigint
 if (places < 0) cents = 0n
 else if (places >= digits.length) cents = BigInt(digits) * 10n ** BigInt(places - digits.length)
 else cents = BigInt(digits.slice(0, places) || '0') + (digits[places] >= '5' ? 1n : 0n)
 const rounded = cents.toString().padStart(3, '0')
 return (match[1] === '-' && cents !== 0n ? '-' : '') + rounded.slice(0, -2) + '.' + rounded.slice(-2)
}
/** Browser-local presentation only; callers retain original precise instants. */
export function timestamp(value: string | null | undefined, missing = '—'): string {
 if (value == null) return missing
 if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
 // Only aware instants are converted; never assign a timezone to wall-clock input.
 if (!/T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return value
 return instantLabel(value)
}
let formatter: Intl.DateTimeFormat | null | undefined
let intlConstructor: typeof Intl.DateTimeFormat | undefined
function browserFormatter(): Intl.DateTimeFormat | null {
 // Reuse detection and formatting for large bucket tables. A new document detects anew.
 const current = typeof Intl === 'undefined' ? undefined : Intl.DateTimeFormat
 if (formatter !== undefined && current === intlConstructor) return formatter
 intlConstructor = current
 formatter = null
 try {
  const zone = current?.().resolvedOptions().timeZone
  if (typeof zone === 'string' && zone.trim() && current) formatter = new current('en-GB', { timeZone: zone, calendar: 'iso8601', numberingSystem: 'latn', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })
 } catch { /* UTC fallback when detection or construction fails. */ }
 return formatter
}
function instantLabel(value: string | number): string {
 const date = new Date(value)
 if (!Number.isFinite(date.getTime())) return String(value)
 const fallback = () => date.toISOString().replace(/\.\d{3}Z$/, 'Z')
 try {
  const local = browserFormatter()
  if (!local) return fallback()
  const parts = local.formatToParts(date)
  const part = (type: Intl.DateTimeFormatPartTypes, pattern: RegExp) => {
   const result = parts.find(item => item.type === type)?.value
   if (!result || !pattern.test(result)) throw new Error('Unavailable time component')
   return result
  }
  return `${part('year', /^\d{4}$/)}-${part('month', /^(0[1-9]|1[0-2])$/)}-${part('day', /^(0[1-9]|[12]\d|3[01])$/)} ${part('hour', /^([01]\d|2[0-3])$/)}:${part('minute', /^[0-5]\d$/)}:${part('second', /^[0-5]\d$/)}`
 } catch { return fallback() }
}
/** Chart coordinates stay numeric UTC epochs; only their visible labels change. */
export const chartTimeLabel = (value: number) => {
 const label = instantLabel(value)
 return label.includes('T') ? label : label.slice(5, 16)
}
export const numericLabel = (value: number) => measurement(value)
export function chartTooltip(value: unknown): string {
 const rows = (Array.isArray(value) ? value : [value]) as { seriesName?: string; axisValue?: string | number; value?: unknown; data?: { display?: string } }[]
 const heading = rows[0]?.axisValue
 const time = typeof heading === 'number' ? instantLabel(heading) : timestamp(String(heading ?? ''))
 return [time, ...rows.map(row => { const point = Array.isArray(row.value) ? row.value[1] : row.value; return `${row.seriesName ?? ''}: ${row.data?.display ?? measurement(point as string | number | null)}` })].join('\n')
}
