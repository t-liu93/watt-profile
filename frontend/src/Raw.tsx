import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/generated/ui/dialog'
import { Button } from './components/ui'
import { Panel, ReadStatus } from './components/AnalysisUI'
import { analysisResources, metricLabel } from './analysis-i18n'
import { rawResources } from './raw-i18n'
import { resources } from './i18n'
import type { Language } from './i18n'
import type { APIClient } from './lib/api'
import { useAnalysisRead } from './lib/analysis'
import { aborted, errorCode, prefix } from './lib/management'
import type { RawPage, RawReading } from './lib/analysis-models'
export const rawKeys = ['device_id', 'session_id', 'received_from', 'received_before', 'limit'] as const
export function rawQuery(params: URLSearchParams, paginate = true) { const query = new URLSearchParams(); for (const key of rawKeys) if ((paginate || key !== 'limit') && params.has('raw_' + key)) query.set(key, params.get('raw_' + key)!); if (paginate && params.has('raw_offset')) query.set('offset', params.get('raw_offset')!); return query }
export function SiteRaw({ client, language, id }: { client: APIClient; language: Language; id: string }) {
 const [params, setParams] = useSearchParams(), t = rawResources[language], a = analysisResources[language], errors = resources[language].errors
 const base = `/sites/${id}/readings`, query = rawQuery(params).toString(), [owner] = useState(() => client.owner())
 const sessionOffset = Number(params.get('raw_session_offset')) || 0
 const sessionQuery = new URLSearchParams({ offset: String(sessionOffset), limit: '50' }); if (params.get('raw_device_id')) sessionQuery.set('device_id', params.get('raw_device_id')!)
 const sessions = useAnalysisRead<{ items: Record<string, unknown>[]; total: number }>(client, `/sites/${id}/sessions?` + sessionQuery)
 const read = useAnalysisRead<RawPage>(client, base + '?' + query)
 const [detail, setDetail] = useState<RawReading | null>(null), [detailError, setDetailError] = useState(''), [detailBusy, setDetailBusy] = useState(false), [exportBusy, setExportBusy] = useState(false), [exportError, setExportError] = useState('')
 const version = useRef(0), returnFocus = useRef<HTMLElement | null>(null)
 const offset = Number(params.get('raw_offset')) || 0, limit = Number(params.get('raw_limit')) || 50
 const context = params.toString()
 function close() { version.current++; owner.cancel(); setDetail(null); setDetailBusy(false); setDetailError('') }
 function dismiss() { close(); if (params.has('raw_record_id')) { const next = new URLSearchParams(params); next.delete('raw_record_id'); setParams(next, { replace: true }) } }
 async function open(device: string, record: string) {
  owner.cancel(); const current = ++version.current; returnFocus.current = document.activeElement as HTMLElement; setDetail(null); setDetailError(''); setDetailBusy(true)
  try { const result = await owner.run(signal => client.request<RawReading>(prefix + base + '/' + encodeURIComponent(device) + '/' + encodeURIComponent(record), { signal })); if (current === version.current) setDetail(result) }
  catch (error) { if (current === version.current && !aborted(error)) setDetailError(errorCode(error)) }
  finally { if (current === version.current) setDetailBusy(false) }
 }
 useEffect(() => { close(); const device = params.get('raw_device_id'), record = params.get('raw_record_id'); if (device && record) void open(device, record); return () => { version.current++; owner.cancel() } }, [context])
 useEffect(() => () => client.release(owner), [client, owner])
 const [exportOwner] = useState(() => client.owner())
 useEffect(() => { exportOwner.cancel(); setExportBusy(false); setExportError(''); return () => exportOwner.cancel() }, [query, exportOwner])
 useEffect(() => () => client.release(exportOwner), [client, exportOwner])
 const message = (error: string) => errors[error as keyof typeof errors] ?? errors.unknown
 function page(nextOffset: number) { const next = new URLSearchParams(params); next.set('raw_offset', String(nextOffset)); next.delete('raw_record_id'); setParams(next) }
 return <Panel title={t.title}><p>{t.independent}</p>
 <form id="reading-filters" key={query} noValidate onSubmit={event => { event.preventDefault(); const values = new FormData(event.currentTarget), next = new URLSearchParams(params); for (const key of rawKeys) { next.delete('raw_' + key); const value = String(values.get(key) ?? '').trim(); if (value) next.set('raw_' + key, value) } next.delete('raw_offset'); next.delete('raw_session_offset'); next.delete('raw_record_id'); setParams(next) }}>
 {rawKeys.map(key => <label key={key}>{t[({ device_id: 'device', session_id: 'session', received_from: 'from', received_before: 'before', limit: 'limit' } as const)[key]]}<input name={key} type={key === 'limit' ? 'number' : 'text'} min={key === 'limit' ? 1 : undefined} max={key === 'limit' ? 200 : undefined} defaultValue={params.get('raw_' + key) ?? (key === 'limit' ? '50' : '')} placeholder={key.startsWith('received') ? '2026-01-01T00:00:00.000001Z' : undefined}/></label>)}<Button type="submit">{t.apply}</Button></form>
 <ReadStatus read={read} language={language}/>{read.error === 'invalid_input' || read.error === 'invalid_query' ? <p id="raw-error" role="alert">{t.invalid}</p> : null}
 <Button onClick={() => void read.load()}>{t.refresh}</Button>
 <Button disabled={exportBusy} onClick={async () => { if (exportBusy) return; setExportBusy(true); setExportError(''); try { const validation = rawQuery(params, false); validation.set('limit', '1'); await exportOwner.run(signal => client.request<RawPage>(prefix + base + '?' + validation, { signal })); const link = document.createElement('a'); link.href = prefix + base + '.csv?' + rawQuery(params, false); link.target = '_blank'; link.rel = 'noopener'; link.click() } catch (error) { if (!aborted(error)) setExportError(errorCode(error)) } finally { setExportBusy(false) } }}>{t.export}</Button><p>{t.exportHelp}</p>{exportError && <p role="alert">{message(exportError)}</p>}
 {read.result && <><p id="reading-count">{t.count}: {read.result.total} · {t.offset} {offset} · {t.limit}: {limit}</p><div className="table-scroll"><table id="readings"><thead><tr><th>{a.received}</th><th>{t.device}</th><th>{t.session}</th><th>{metricLabel("energy_import_t1_kwh", language)}</th><th>{metricLabel("power_import_kw", language)}</th><th>{t.detail}</th></tr></thead><tbody>{read.result.items.map(item => <tr key={item.device_id + item.record_id}><td><code>{item.received_at}</code></td><td><code>{item.device_id}</code></td><td><code>{item.session_id}</code></td><td><code>{item.metrics.energy_import_t1_kwh ?? a.notProvided}</code></td><td><code>{item.metrics.power_import_kw ?? a.notProvided}</code></td><td><Button onClick={() => void open(item.device_id, item.record_id)}>{t.view}</Button></td></tr>)}</tbody></table>{!read.result.items.length && <p>{t.empty}</p>}</div><div className="action-row"><Button disabled={!offset} onClick={() => page(Math.max(0, offset - limit))}>{t.previous}</Button><Button disabled={offset + limit >= read.result.total} onClick={() => page(offset + limit)}>{t.next}</Button></div></>}
 <details><summary>{t.descriptors}</summary><ReadStatus read={sessions} language={language}/>{sessions.result && <><pre id="raw-sessions">{sessions.result.items.length ? JSON.stringify(sessions.result.items, null, 2) : t.noSessions}</pre><div className="action-row"><Button disabled={!sessionOffset} onClick={() => { const next = new URLSearchParams(params); next.set("raw_session_offset", String(Math.max(0, sessionOffset - 50))); setParams(next) }}>{t.previousPage}</Button><span>{sessions.result.total} · {t.offset} {sessionOffset}</span><Button disabled={sessionOffset + 50 >= sessions.result.total} onClick={() => { const next = new URLSearchParams(params); next.set("raw_session_offset", String(sessionOffset + 50)); setParams(next) }}>{t.nextPage}</Button></div></>}</details>
 <Dialog open={!!detail || detailBusy || !!detailError} onOpenChange={value => { if (!value) dismiss() }}><DialogContent id="detail-dialog" className="reading-dialog" onCloseAutoFocus={event => { event.preventDefault(); (returnFocus.current?.isConnected && returnFocus.current.matches('button, a[href], input, select, textarea, [tabindex]') ? returnFocus.current : document.querySelector<HTMLElement>('#reading-filters button'))?.focus() }}><DialogTitle>{t.detail}</DialogTitle><DialogDescription>{t.fields}</DialogDescription>{detailBusy && <p role="status">{a.loading}</p>}{detailError && <p role="alert">{message(detailError)}</p>}{detail && <><dl id="metrics">{Object.entries(detail.metrics).map(([metric, value]) => <div key={metric}><dt>{metricLabel(metric, language)}</dt><dd><code>{value ?? a.notProvided}</code></dd></div>)}</dl><pre id="fields-quality">{JSON.stringify(detail, null, 2)}</pre></>}<Button onClick={dismiss}>{t.close}</Button></DialogContent></Dialog>
 </Panel>
}
