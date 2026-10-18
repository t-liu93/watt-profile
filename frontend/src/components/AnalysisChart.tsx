import { useEffect, useRef } from 'react'
import { init } from '../lib/charts'
import type { EChartsCoreOption } from '../lib/charts'
export function AnalysisChart({ option, label, onZoom }: { option: EChartsCoreOption; label: string; onZoom?: (start: number, end: number) => void }) {
 const container = useRef<HTMLDivElement>(null), handler = useRef(onZoom)
 handler.current = onZoom
 useEffect(() => {
  const element = container.current!
  const chart = init(element)
  chart.setOption(option)
  chart.on('datazoom', (event: unknown) => { const data = event as { start?: number; end?: number; batch?: { start: number; end: number }[] }; const item = data.batch?.[0] ?? data; if (item.start !== undefined && item.end !== undefined) handler.current?.(item.start, item.end) })
  const resize = new ResizeObserver(() => chart.resize()); resize.observe(element)
  return () => { resize.disconnect(); chart.dispose() }
 }, [option])
 return <div ref={container} className="analysis-chart" role="img" aria-label={label}/>
}
