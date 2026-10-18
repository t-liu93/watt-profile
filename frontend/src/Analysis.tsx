import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Button } from './components/ui'
import { AnalysisChart } from './components/AnalysisChart'
import { analysisResources, metricLabel } from './analysis-i18n'
import { resources } from './i18n'
import type { Language } from './i18n'
import type { APIClient } from './lib/api'
import { useAnalysisRead } from './lib/analysis'
import type { AnalysisRange, DirectionEnergy, Latest, Overview, PowerResult, SessionContext, SessionPage } from './lib/analysis-models'
import { fromMicros, fullDays, localDate, micros, plotNumber, recentRange, zoomRange } from './lib/analysis-time'
import { powerOption } from './lib/analysis-charts'
import type { EChartsCoreOption } from './lib/charts'
function Panel({ title, children }: { title: string; children: ReactNode }) { return <section className="neon-panel analysis-panel"><h3>{title}</h3>{children}</section> }
function ReadStatus({ read, language }: { read: { loading: boolean; error: string; updatedAt: string }; language: Language }) {
 const t = analysisResources[language], errors = resources[language].errors
 return <>{read.loading && <p role="status">{t.loading}</p>}{read.updatedAt && <p>{t.updated} <time dateTime={read.updatedAt}>{read.updatedAt}</time></p>}{read.error && <p role="alert">{t.failure} {errors[read.error as keyof typeof errors] ?? errors.unknown}</p>}</>
}
export function rawLink(params: URLSearchParams, device: string, record: string, session: string) { const next = new URLSearchParams(params); next.set('tab', 'raw'); next.set('raw_device_id', device); next.set('raw_record_id', record); next.set('raw_session_id', session); return '?' + next }
export function SiteOverview({ client, language, id }: { client: APIClient; language: Language; id: string }) {
 const t = analysisResources[language], [params, setParams] = useSearchParams()
 const lifetime = useRef(''); lifetime.current = id + '?' + params.toString()
 useEffect(() => () => { lifetime.current = '' }, [])
 const offset = Math.max(0, Number(params.get('session_offset')) || 0), base = `/sites/${id}/analysis/`
 const sessions = useAnalysisRead<SessionPage>(client, base + `sessions?offset=${offset}&limit=20`)
 const session = params.get('session_id'), mode = params.get('range_mode') === 'custom' ? 'custom' : 'recent'
 const first = params.get('received_from'), before = params.get('received_before')
 const range = useMemo(() => first && before ? { received_from: first, received_before: before } : null, [first, before])
 const query = session && range ? new URLSearchParams({ session_id: session, ...range }).toString() : null
 const overview = useAnalysisRead<Overview>(client, query ? base + 'overview?' + query : null)
 const latest = useAnalysisRead<Latest>(client, session ? base + 'latest?' + new URLSearchParams({ session_id: session }) : null, true)
 const [zoom, setZoom] = useState<{ query: string | null; bounds: AnalysisRange | null; percentages: [number, number] }>({ query, bounds: null, percentages: [0, 100] })
 const activeZoom = zoom.query === query ? zoom : { query, bounds: null, percentages: [0, 100] as [number, number] }
 const power = useAnalysisRead<PowerResult>(client, query && activeZoom.bounds ? base + 'power?' + query + '&' + new URLSearchParams({ zoom_from: activeZoom.bounds.received_from, zoom_before: activeZoom.bounds.received_before }) : null)
 const [invalid, setInvalid] = useState(false)
 function selection(nextSession: string | null, nextRange: AnalysisRange | null, nextMode = mode, replace = false) {
  const next = new URLSearchParams(params)
  next.set('range_mode', nextMode)
  for (const key of ['session_id', 'received_from', 'received_before']) next.delete(key)
  if (nextSession) next.set('session_id', nextSession)
  if (nextRange) { next.set('received_from', nextRange.received_from); next.set('received_before', nextRange.received_before) }
  setParams(next, { replace })
 }
 useEffect(() => {
  if (!sessions.result || session || range) return
  selection(sessions.result.default_session_id, sessions.result.initial_range, mode, true)
 }, [sessions.result, session, range])
 function choose(item: SessionContext) { setInvalid(false); selection(item.session.session_id, mode === 'custom' ? range : recentRange(item)) }
 async function refresh() {
  if (mode === 'custom') { void overview.load(); if (activeZoom.bounds) void power.load(); return }
  // Refresh resolves the backend's most-recent-reading context again, independent of pagination.
  const context = lifetime.current
  const page = await sessions.load()
  if (!page || lifetime.current !== context) return
  const next = page.initial_range
  if (page.default_session_id === session && next?.received_from === first && next?.received_before === before) { void overview.load(); if (activeZoom.bounds) void power.load() }
  else selection(page.default_session_id, next, 'recent')
 }
 const data = overview.result, plottedPower = power.result ?? data?.power
 const [theme, setTheme] = useState(document.documentElement.dataset.theme)
 useEffect(() => { const observer = new MutationObserver(() => setTheme(document.documentElement.dataset.theme)); observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] }); return () => observer.disconnect() }, [])
 const option = useMemo(() => plottedPower && range ? powerOption(plottedPower, [t.import, t.export], theme === 'light' ? '#19263d' : '#e5edff', range, activeZoom.percentages, [t.minimum, t.maximum, t.mean]) : null, [plottedPower, range, t, theme, activeZoom.percentages])
 const display = (value: string | null | undefined) => value ?? t.unavailable
 const peak = (value: PowerResult['import_sampled_peak'], label: string) => <article><h4>{label}</h4>{value ? <><p>{value.value_kw} kW · {value.received_at}</p><Link to={rawLink(params, value.device_id, value.record_id, session!)}>{t.raw}</Link></> : <p>{t.unavailable}</p>}</article>
 let dateFirst = '', dateLast = ''
 try { if (range) { dateFirst = localDate(range.received_from); dateLast = localDate(fromMicros(micros(range.received_before) - 1n)) } } catch { /* Server validates malformed copied boundaries. */ }
 return <>
 <Panel title={t.selection}>
 <ReadStatus read={sessions} language={language}/>
 <label>{t.session}<select aria-label={t.session} value={session ?? ''} onChange={event => { const item = sessions.result?.items.find(item => item.session.session_id === event.target.value); if (item) choose(item) }}><option value="" disabled>{t.session}</option>{session && !sessions.result?.items.some(item => item.session.session_id === session) && <option value={session}>{session}</option>}{sessions.result?.items.map(item => <option key={item.session.session_id} value={item.session.session_id}>{item.session.started_at} · {item.session.session_id} · {item.reading_count}</option>)}</select></label>
 <div className="action-row"><Button disabled={!offset} onClick={() => { const next = new URLSearchParams(params); next.set('session_offset', String(Math.max(0, offset - 20))); setParams(next) }}>{t.previous}</Button><span>{t.sessions}: {sessions.result?.total ?? '—'}</span><Button disabled={!sessions.result || offset + 20 >= sessions.result.total} onClick={() => { const next = new URLSearchParams(params); next.set('session_offset', String(offset + 20)); setParams(next) }}>{t.next}</Button></div>
 <p>{mode === 'recent' ? t.recent : t.custom}</p><Button onClick={async () => { const context = lifetime.current; const page = await sessions.load(); if (page && lifetime.current === context) selection(page.default_session_id, page.initial_range, 'recent') }}>{t.recent}</Button>
 <form noValidate key={String(range?.received_from) + range?.received_before} onSubmit={event => { event.preventDefault(); const values = new FormData(event.currentTarget); try { const next = fullDays(String(values.get('first')), String(values.get('last'))); selection(session, next, 'custom'); setInvalid(false) } catch { setInvalid(true) } }}><label>{t.first}<input aria-label={t.first} type="date" name="first" defaultValue={dateFirst} required/></label><label>{t.last}<input aria-label={t.last} type="date" name="last" defaultValue={dateLast} required/></label><Button type="submit" disabled={!session}>{t.apply}</Button></form>
 <p>{t.dates}</p>{invalid && <p role="alert">{t.invalid}</p>}{range && <p className="identifier">{t.range}: {range.received_from} → {range.received_before}</p>}
 <Button id="refresh-analysis" onClick={() => void refresh()}>{t.refresh}</Button>
 {!session && sessions.result && <p>{t.empty}</p>}
 </Panel>
 <ReadStatus read={overview} language={language}/>
 {data && <>
 <Panel title={t.statistics}><p>{t.count}: {data.context.reading_count} · {t.calculated}: {data.context.calculated_at}</p><p>{t.actual}: {display(data.context.first_reading_received_at)} → {display(data.context.latest_reading_received_at)}</p>{!data.context.reading_count && <p>{t.empty}</p>}<div className="table-scroll"><table id="period-statistics"><thead><tr>{['kW', t.mean, t.minimum, t.maximum, t.valid].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{data.statistics.map(row => <tr key={row.metric}><th>{row.metric === 'power_import_kw' ? t.import : t.export}</th><td>{display(row.mean)}</td><td>{display(row.minimum)}</td><td>{display(row.maximum)}</td><td>{row.valid_count}</td></tr>)}</tbody></table></div></Panel>
 <Panel title={t.power}><p>{t.powerHelp}</p><p>{t.gap}</p><Button onClick={() => setZoom({ query, bounds: null, percentages: [0, 100] })}>{t.resetZoom}</Button><ReadStatus read={power} language={language}/>{option && <AnalysisChart option={option} label={t.power} onZoom={(start, end) => { if (range) setZoom({ query, bounds: start <= 0 && end >= 100 ? null : zoomRange(range, start, end), percentages: [start, end] }) }}/>}<p>{t.resolution}: {plottedPower?.resolution_seconds}</p>{plottedPower && <><Unplottable values={[...plottedPower.import_power.buckets, ...plottedPower.export_power.buckets].flatMap(bucket => [bucket.mean, bucket.minimum, bucket.maximum])} language={language}/><details><summary>{t.numeric}</summary><div className="table-scroll"><table><thead><tr>{['UTC', 'kW', t.mean, t.minimum, t.maximum, t.valid, t.flags].map((label, index) => <th key={index}>{label}</th>)}</tr></thead><tbody>{[plottedPower.import_power, plottedPower.export_power].flatMap((curve, direction) => curve.buckets.map(bucket => <tr key={direction + bucket.received_from}><td>{bucket.received_from} → {bucket.received_before}</td><td>{direction ? t.export : t.import}</td><td>{display(bucket.mean)}</td><td>{display(bucket.minimum)}</td><td>{display(bucket.maximum)}</td><td>{bucket.valid_count}</td><td>{bucket.has_gap ? t.yes : t.no} / {bucket.break_before ? t.yes : t.no}</td></tr>))}</tbody></table></div></details></>}</Panel>
 <Panel title={t.daily}><p>{t.energyHelp}</p><DailyChart energy={[data.energy.import_energy, data.energy.export_energy]} language={language} theme={theme}/><div className="analysis-grid">{([['import_energy', t.import], ['export_energy', t.export]] as const).map(([key, label]) => <Energy key={key} energy={data.energy[key]} label={label} language={language}/>)}</div></Panel>
 <Panel title={t.peaks}><p>{t.peakHelp}</p><div className="analysis-grid">{peak(data.power.import_sampled_peak, t.import)}{peak(data.power.export_sampled_peak, t.export)}</div></Panel>
 <Panel title={t.quality}><p>{data.quality.actual_count} / {data.quality.expected_count} · {data.quality.coverage_percent}%</p><p>{t.excess}: {data.quality.excess_count} · {t.gaps}: {data.quality.gap_total} · {t.redacted}: {data.quality.redacted_field_count}</p><p>{t.qualityHelp}</p>{data.quality.issues.map(issue => <p key={issue.reason}>{issue.reason}: {issue.count}</p>)}</Panel>
 </>}
 {session && <Panel title={t.latest}><p>{t.latestHelp}</p><Button aria-pressed={latest.paused} onClick={() => latest.setPaused(!latest.paused)}>{latest.paused ? t.resume : t.pause}</Button><ReadStatus read={latest} language={language}/>{latest.result && <><dl>{([[t.received, latest.result.snapshot?.received_at], [t.ingested, latest.result.ingested_at], [t.meter, latest.result.snapshot?.meter_time_utc], [t.contact, latest.result.device_last_authenticated_contact_at], [t.siteLatest, latest.result.site_latest_reading_received_at]] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{display(value)}</dd></div>)}</dl>{latest.result.snapshot ? <><Link to={rawLink(params, latest.result.snapshot.device_id, latest.result.snapshot.record_id, session)}>{t.raw}</Link><div className="table-scroll"><table><tbody>{Object.entries(latest.result.snapshot.metrics).map(([metric, value]) => <tr key={metric}><th>{metricLabel(metric, language)}</th><td>{value ?? t.notProvided}</td></tr>)}</tbody></table></div><details><summary>{t.complete}</summary><pre>{JSON.stringify(latest.result.snapshot, null, 2)}</pre></details></> : <p>{t.empty}</p>}</>}</Panel>}
 </>
}
function Unplottable({ values, language }: { values: (string | null)[]; language: Language }) { return values.some(value => value !== null && plotNumber(value) === null) ? <p role="note">{analysisResources[language].unplottable}</p> : null }
function Energy({ energy, label, language }: { energy: DirectionEnergy; label: string; language: Language }) {
 const t = analysisResources[language], value = (number: string | null) => number ?? t.unavailable
 return <article><h4>{label}</h4><p>{t.energy}: {value(energy.energy_kwh)} kWh</p><p>{t.unallocated}: {value(energy.cross_day_unallocated_kwh)} kWh</p><p>{t.endpoints}: {value(energy.first_endpoint_received_at)} → {value(energy.last_endpoint_received_at)}</p><p>{t.intervals}: {energy.valid_interval_count} · {t.resets}: {energy.reset_count} · {t.bridged}: {energy.bridged_gap_count} · {t.missing}: {energy.missing_endpoint_count}</p><p>{t.reasons}: {energy.incompleteness_reasons.map(reason => t[reason as keyof typeof t] ?? reason).join(', ') || t.noReasons}</p><Unplottable values={energy.daily.map(day => day.energy_kwh)} language={language}/><div className="table-scroll"><table><thead><tr><th>{t.date}</th><th>kWh</th><th>{t.intervals}</th><th>{t.endpoints}</th></tr></thead><tbody>{energy.daily.map(day => <tr key={day.date}><td>{day.date}</td><td>{day.energy_kwh}</td><td>{day.valid_interval_count}</td><td>{day.first_endpoint_received_at} → {day.last_endpoint_received_at}</td></tr>)}</tbody></table></div>{!energy.daily.length && <p>{t.noDaily}</p>}</article>
}
function DailyChart({ energy, language, theme }: { energy: [DirectionEnergy, DirectionEnergy]; language: Language; theme?: string }) {
 const t = analysisResources[language], [incoming, outgoing] = energy
 const option = useMemo<EChartsCoreOption>(() => { const dates = [...new Set([...incoming.daily, ...outgoing.daily].map(day => day.date))].sort(), color = theme === 'light' ? '#19263d' : '#e5edff'; return { animation: false, textStyle: { color }, tooltip: { trigger: 'axis' }, legend: { top: 8, textStyle: { color } }, grid: { left: 65, right: 20, bottom: 50 }, xAxis: { type: 'category', data: dates, axisLabel: { color } }, yAxis: { type: 'value', name: 'kWh', axisLabel: { color } }, series: [incoming, outgoing].map((direction, i) => ({ type: 'bar', name: i ? t.export : t.import, itemStyle: { color: i ? '#5bd5ee' : '#6695ff' }, data: dates.map(date => plotNumber(direction.daily.find(day => day.date === date)?.energy_kwh ?? null)) })) } }, [incoming, outgoing, theme, t])
 return <AnalysisChart option={option} label={t.daily}/>
}
