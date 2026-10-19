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
/** Keep timezone/offset and original datetime attributes; trim only visible fractions. */
export function timestamp(value: string | null | undefined, missing = '—'): string {
 return value == null ? missing : value.replace(/(T\d{2}:\d{2}:\d{2})\.\d+(?=Z|[+-]\d{2}:\d{2}$)/, '$1')
}
export const numericLabel = (value: number) => measurement(value)
export function chartTooltip(value: unknown): string {
 const rows = (Array.isArray(value) ? value : [value]) as { seriesName?: string; axisValue?: string | number; value?: unknown; data?: { display?: string } }[]
 const heading = rows[0]?.axisValue
 const time = typeof heading === 'number' ? new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Amsterdam', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(heading) + ' Europe/Amsterdam' : timestamp(String(heading ?? ''))
 return [time, ...rows.map(row => { const point = Array.isArray(row.value) ? row.value[1] : row.value; return `${row.seriesName ?? ''}: ${row.data?.display ?? measurement(point as string | number | null)}` })].join('\n')
}
