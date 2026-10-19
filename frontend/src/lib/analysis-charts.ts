import { measurement, numericLabel, chartTooltip, chartTimeLabel } from './presentation'
import type { EChartsCoreOption } from './charts'
import type { MetricCurve, PowerResult } from './analysis-models'
import { plotNumber } from './analysis-time'
export function curveSeries(curve: MetricCurve, label: string, sign: number, color: string, extrema: [string, string, string]) {
 return (['minimum', 'maximum', 'mean'] as const).map(kind => ({ type: 'line', name: label + ' ' + extrema[['minimum', 'maximum', 'mean'].indexOf(kind)], showSymbol: true, symbolSize: 5, connectNulls: false, lineStyle: { width: kind === 'mean' ? 2 : 1, type: kind === 'mean' ? 'solid' : 'dashed' }, itemStyle: { color }, data: curve.buckets.flatMap(bucket => {
  const value = plotNumber(bucket[kind]), point = { display: measurement(bucket[kind] === null ? null : (sign < 0 ? '-' : '') + bucket[kind]), value: [Date.parse(bucket.received_from), value === null ? null : sign * value], symbol: bucket.has_gap ? 'diamond' : kind === 'mean' && value !== null ? 'circle' : 'none' }
  return bucket.break_before ? [[Date.parse(bucket.received_from) - 0.001, null], point] : [point]
 }) }))
}
export function powerOption(power: PowerResult, labels: [string, string], foreground: string, global: { received_from: string; received_before: string }, zoom: [number, number], extrema: [string, string, string]): EChartsCoreOption {
 return { animation: false, textStyle: { color: foreground }, tooltip: { trigger: 'axis', renderMode: 'richText', formatter: chartTooltip }, legend: { type: 'scroll', top: 8, left: 20, right: 20, textStyle: { color: foreground, fontSize: 11 } }, grid: { left: 65, right: 20, top: 65, bottom: 80 }, xAxis: { type: 'time', min: Date.parse(global.received_from), max: Date.parse(global.received_before), axisLabel: { color: foreground, formatter: chartTimeLabel, hideOverlap: true } }, yAxis: { type: 'value', name: 'kW', axisLabel: { color: foreground, formatter: numericLabel } }, dataZoom: [{ type: 'slider', left: 65, right: 20, bottom: 15, height: 25, start: zoom[0], end: zoom[1], filterMode: 'none' }], series: [...curveSeries(power.import_power, labels[0], 1, '#6695ff', extrema), ...curveSeries(power.export_power, labels[1], -1, '#5bd5ee', extrema)] }
}
