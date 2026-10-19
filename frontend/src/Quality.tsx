import { measurement, timestamp } from './lib/presentation'
import { Link, useSearchParams } from 'react-router-dom'
import { Button } from './components/ui'
import { Panel, ReadStatus, rawLink } from './components/AnalysisUI'
import { analysisResources, metricLabel } from './analysis-i18n'
import { rawResources, issueLabel } from './raw-i18n'
import type { Language } from './i18n'
import type { useAnalysisRead } from './lib/analysis'
import type { QualityResult } from './lib/analysis-models'
import { micros, fromMicros } from './lib/analysis-time'
export function SiteQuality({ read, language }: { read: ReturnType<typeof useAnalysisRead<QualityResult>>; language: Language }) {
 const [params, setParams] = useSearchParams(), t = analysisResources[language], r = rawResources[language], data = read.result
 function page(kind: string, offset: number) { const next = new URLSearchParams(params); next.set(kind + '_offset', String(offset)); setParams(next) }
 const paging = (kind: 'gap' | 'problem', total: number, offset: number, limit: number) => <div className="action-row"><Button disabled={!offset} onClick={() => page(kind, Math.max(0, offset - limit))}>{r.previousPage}</Button><span>{total} · {r.offset} {offset}</span><Button disabled={offset + limit >= total} onClick={() => page(kind, offset + limit)}>{r.nextPage}</Button></div>
 return <><ReadStatus read={read} language={language}/>{data && <>
 <Panel title={t.quality}><p>{data.actual_count} / {data.expected_count} · {measurement(data.coverage_percent)}%</p><p>{t.excess}: {data.excess_count}</p><p>{t.qualityHelp}</p><p>{r.audit}</p><p>{t.calculated}: {timestamp(data.context.calculated_at)}</p><p>{t.actual}: {timestamp(data.context.first_reading_received_at, t.unavailable)} → {timestamp(data.context.latest_reading_received_at, t.unavailable)}</p><Link to={rawLink(params, null, null, data.context.session_id)}>{t.rangeRaw}</Link></Panel>
 <Panel title={r.endpoints}><table><thead><tr><th>{t.direction}</th><th>{r.valid}</th><th>{r.missing}</th></tr></thead><tbody><tr><th>{t.import}</th><td>{data.import_endpoint_valid_count}</td><td>{data.import_endpoint_missing_count}</td></tr><tr><th>{t.export}</th><td>{data.export_endpoint_valid_count}</td><td>{data.export_endpoint_missing_count}</td></tr></tbody></table><div className="table-scroll"><table id="metric-validity"><thead><tr><th>{t.metric}</th><th>{r.valid}</th><th>{r.missing}</th></tr></thead><tbody>{data.metrics.map(item => <tr key={item.metric}><th>{metricLabel(item.metric, language)}</th><td>{item.valid_count}</td><td>{item.missing_count}</td></tr>)}</tbody></table></div></Panel>
 <Panel title={t.gaps}><p>{r.semantics}</p><div className="table-scroll"><table id="quality-gaps"><tbody>{data.gaps.map(gap => <tr key={gap.kind + gap.received_from}><th>{r[gap.kind]}</th><td>{timestamp(gap.received_from)} → {timestamp(gap.received_before)}</td><td><Link to={rawLink(params, null, null, data.context.session_id, { received_from: fromMicros(micros(gap.received_from) - 10000000n), received_before: fromMicros(micros(gap.received_before) + 1n) })}>{r.around}</Link></td></tr>)}</tbody></table></div>{!data.gaps.length && <p>{r.none}</p>}{paging('gap', data.gap_total, data.gap_offset, data.gap_limit)}</Panel>
 <Panel title={r.issues}>{data.issues.map(issue => <p key={issue.reason}>{issueLabel(issue.reason, language)} (<code>{issue.reason}</code>): {issue.count}</p>)}{!data.issues.length && <p>{r.none}</p>}</Panel><Panel title={t.redacted}><p>{data.redacted_field_count}</p><p>{t.qualityHelp}</p></Panel>
 <Panel title={r.problems}><div className="table-scroll"><table id="quality-problems"><tbody>{data.problems.map(item => <tr key={item.device_id + item.record_id}><td>{timestamp(item.received_at)}</td><td>{item.issues.map((issue, index) => <p key={index}>{issueLabel(String(issue.reason), language)} · <code>{String(issue.reason)}</code>{issue.code ? <> · <code>{String(issue.code)}</code></> : null}</p>)}</td><td><Link to={rawLink(params, item.device_id, item.record_id, data.context.session_id)}>{t.raw}</Link></td></tr>)}</tbody></table></div>{!data.problems.length && <p>{r.none}</p>}{paging('problem', data.problem_total, data.problem_offset, data.problem_limit)}</Panel>
 </>}</>
}
