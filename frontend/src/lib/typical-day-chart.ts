import type { TypicalDaySlot } from './analysis-models'
import { plotNumber } from './analysis-time'
import type { EChartsCoreOption } from './charts'
/** Keep every quarter-hour, including empty/unrepresentable values as line breaks. */
export function typicalDayOption(slots: TypicalDaySlot[], labels: readonly [string, string], contributors: string, unavailable: string, color: string): EChartsCoreOption {
 const times = Array.from({ length: 96 }, (_, index) => `${String(Math.floor(index / 4)).padStart(2, '0')}:${String(index % 4 * 15).padStart(2, '0')}`)
 return {
  animation: false, textStyle: { color }, tooltip: { trigger: 'axis', renderMode: 'richText' }, legend: { top: 8, textStyle: { color } }, grid: { left: 65, right: 20, bottom: 55 },
  xAxis: { type: 'category', name: 'Europe/Amsterdam', nameLocation: 'middle', nameGap: 32, data: times, axisLabel: { color } }, yAxis: { type: 'value', name: 'kW', axisLabel: { color } },
  series: (['power_import_kw', 'power_export_kw'] as const).map((metric, index) => ({
   type: 'line', name: labels[index], connectNulls: false, showSymbol: true, symbol: index ? 'diamond' : 'circle', symbolSize: 5,
   lineStyle: { type: index ? 'dashed' : 'solid' }, itemStyle: { color: index ? '#5bd5ee' : '#6695ff' },
   data: times.map(time => { const slot = slots.find(item => item.metric === metric && item.local_time.slice(0, 5) === time); return { value: plotNumber(slot?.mean ?? null), exact: slot?.mean ?? null, contributing_date_count: slot?.contributing_date_count ?? 0 } }),
   tooltip: { valueFormatter: (_value: unknown, dataIndex: number) => { const slot = slots.find(item => item.metric === metric && item.local_time.slice(0, 5) === times[dataIndex]); return `${slot?.mean ?? unavailable} kW · ${contributors}: ${slot?.contributing_date_count ?? 0}` } },
  })),
 }
}
