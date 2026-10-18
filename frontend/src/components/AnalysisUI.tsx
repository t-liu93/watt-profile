import type { ReactNode } from 'react'
import { analysisResources } from '../analysis-i18n'
import { resources } from '../i18n'
import type { Language } from '../i18n'
import { plotNumber } from '../lib/analysis-time'
export function Panel({ title, children }: { title: string; children: ReactNode }) { return <section className="neon-panel analysis-panel"><h3>{title}</h3>{children}</section> }
export function ReadStatus({ read, language }: { read: { loading: boolean; error: string; updatedAt: string }; language: Language }) {
 const t = analysisResources[language], errors = resources[language].errors
 return <>{read.loading && <p role="status">{t.loading}</p>}{read.updatedAt && <p>{t.updated} <time dateTime={read.updatedAt}>{read.updatedAt}</time></p>}{read.error && <p role="alert">{t.failure} {errors[read.error as keyof typeof errors] ?? errors.unknown}</p>}</>
}
export function rawLink(params: URLSearchParams, device: string | null, record: string | null, session: string) {
 const next = new URLSearchParams(params)
 next.set('tab', 'raw'); next.set('raw_session_id', session); next.delete('raw_offset')
 for (const key of ['received_from', 'received_before']) { const bound = params.get(key); if (bound) next.set('raw_' + key, bound); else next.delete('raw_' + key) }
 if (device) next.set('raw_device_id', device); else next.delete('raw_device_id')
 if (record) next.set('raw_record_id', record); else next.delete('raw_record_id')
 return '?' + next
}
export function Unplottable({ values, language }: { values: (string | null)[]; language: Language }) { return values.some(value => value !== null && plotNumber(value) === null) ? <p role="note">{analysisResources[language].unplottable}</p> : null }
