import { useEffect, useMemo, useState } from 'react'
import type { RefObject } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { AnalysisChart } from './components/AnalysisChart'
import { Panel, ReadStatus, Unplottable, rawLink } from './components/AnalysisUI'
import { analysisResources, metricLabel } from './analysis-i18n'
import type { Language } from './i18n'
import type { APIClient } from './lib/api'
import { useAnalysisRead } from './lib/analysis'
import type { DateGroup, TypicalDayResult } from './lib/analysis-models'
import { typicalDayOption } from './lib/typical-day-chart'
export function SiteUsage({ client, language, base, query, params, refreshRef }: { client: APIClient; language: Language; base: string; query: string | null; params: URLSearchParams; refreshRef: RefObject<(() => void) | null> }) {
 const t = analysisResources[language], [, setParams] = useSearchParams()
 const selected = params.get('date_group'), group: DateGroup = selected === 'weekday' || selected === 'weekend' ? selected : 'all'
 const read = useAnalysisRead<TypicalDayResult>(client, query ? base + 'typical-day?' + query + '&group=' + group : null)
 useEffect(() => { const refresh = () => { void read.load() }; refreshRef.current = refresh; return () => { if (refreshRef.current === refresh) refreshRef.current = null } }, [read.load, refreshRef])
 const [theme, setTheme] = useState(document.documentElement.dataset.theme)
 useEffect(() => { const observer = new MutationObserver(() => setTheme(document.documentElement.dataset.theme)); observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] }); return () => observer.disconnect() }, [])
 const slots = read.result?.groups.find(item => item.group === group)?.slots
 const option = useMemo(() => slots ? typicalDayOption(slots, [t.import, t.export], t.contributors, t.unavailable, theme === 'light' ? '#19263d' : '#e5edff') : null, [slots, t, theme])
 return <Panel title={t.typicalDay}>
 <label>{t.dateGroup}<select aria-label={t.dateGroup} value={group} onChange={event => { const next = new URLSearchParams(params); next.set('date_group', event.target.value); setParams(next) }}>{(['all', 'weekday', 'weekend'] as const).map(key => <option key={key} value={key}>{t[key]}</option>)}</select></label>
 <p>{t.typicalHelp}</p><p>{t.eligibilityHelp}</p><p>{t.dstHelp}</p><p>{t.gridHelp}</p>
 <ReadStatus read={read} language={language}/>
 {read.result && <><p>{t.count}: {read.result.context.reading_count} · {t.calculated}: {read.result.context.calculated_at}</p><p>{t.actual}: {read.result.context.first_reading_received_at ?? t.unavailable} → {read.result.context.latest_reading_received_at ?? t.unavailable}</p></>}
 {slots && <>
 {!slots.some(slot => slot.contributing_date_count > 0) && <p role="status">{t.noEligible}</p>}
 {option && <AnalysisChart option={option} label={t.typicalDay}/>}
 <Unplottable values={slots.map(slot => slot.mean)} language={language}/>
 <div className="analysis-grid">{(['power_import_kw', 'power_export_kw'] as const).map(metric => { const counts = slots.filter(slot => slot.metric === metric).map(slot => slot.contributing_date_count), eligible = counts.filter(count => count > 0); return <article key={metric}><h4>{metricLabel(metric, language)}</h4><p>{t.eligibleSlots}: {eligible.length} / 96</p><p>{t.contributors}: {eligible.length ? `${Math.min(...eligible)}–${Math.max(...eligible)}` : '0'}</p></article> })}</div><p>{t.contributorsHelp}</p>
 <details><summary>{t.typicalNumeric}</summary><div className="table-scroll"><table id="typical-day-values"><thead><tr><th>{t.localTime}</th><th>{t.metric}</th><th>{t.mean} (kW)</th><th>{t.contributors}</th></tr></thead><tbody>{slots.map(slot => <tr key={slot.local_time + slot.metric}><td>{slot.local_time}</td><th>{metricLabel(slot.metric, language)}</th><td>{slot.mean ?? t.unavailable}</td><td>{slot.contributing_date_count}</td></tr>)}</tbody></table></div></details>
 </>}
 {params.get('session_id') && <p><Link to={rawLink(params, null, null, params.get('session_id')!)}>{t.rangeRaw}</Link> · {t.aggregateHelp}</p>}
 </Panel>
}
