import { timestamp } from './lib/presentation'
import { useEffect, useRef, useState } from 'react';
import type { ComponentProps, FormEvent, ReactNode } from 'react';
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { Button, ConfirmationDialog } from './components/ui';
import { resources } from './i18n';
import { SiteRaw } from './Raw';
import { SiteAnalysis } from './Analysis';
import type { Language } from './i18n';
import { managementResources } from './management-i18n';
import { APIClient, RequestOwner } from './lib/api';
import { aborted, deviceLabel, errorCode, formValues, siteLabel, useManagement, useStatusRefresh } from './lib/management';
import type { DeviceDetail, DeviceSummary, Page, RegistrationResult, Site, SiteStatus, SiteSummary } from './lib/management';
type Props = {
    client: APIClient;
    language: Language;
};
type M = ReturnType<typeof useManagement>;
function AdminForm({ m, onSubmit, ...props }: ComponentProps<'form'> & {
    m: M;
}) {
    return <form {...props} noValidate onSubmit={event => {
            if (m.isBusy()) { event.preventDefault(); return; }
            const missing = Array.from(event.currentTarget.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('[required]')).find(input => !input.value);
            if (missing) {
                event.preventDefault();
                m.setError('invalid_input');
                missing.focus();
                return;
            }
            onSubmit?.(event);
        }}/>;
}
function Panel({ title, children }: {
    title: string;
    children: ReactNode;
}) {
    return <section className="neon-panel">
    <h3>{title}</h3>{children}</section>;
}
function Feedback({ m, language }: {
    m: M;
    language: Language;
}) {
    const errors = resources[language].errors;
    return <>
    <p id="error" role="alert" hidden={!m.error}>{errors[m.error as keyof typeof errors] ?? errors.unknown}{m.error && <> {managementResources[language].uncertain}</>}</p>
    <p id="message" role="status" hidden={!m.message}>{m.message}</p>
    </>;
}
function StatusRefresh({ status, language }: {
    status: { paused: boolean; setPaused: (value: boolean) => void; updatedAt: string; error: string };
    language: Language;
}) {
    const t = managementResources[language], errors = resources[language].errors;
    return <div className="action-row">
        <Button aria-pressed={status.paused} onClick={() => status.setPaused(!status.paused)}>{status.paused ? t.resumeRefresh : t.pauseRefresh}</Button>
        {status.updatedAt && <p id="status-updated" role="status">{t.updatedAt} <time dateTime={status.updatedAt}>{timestamp(status.updatedAt)}</time></p>}
        {status.error && <p id="status-error" role="alert">{t.statusRefreshFailed} {errors[status.error as keyof typeof errors] ?? errors.unknown}</p>}
    </div>;
}
function Field({ label, name, value = '', multiline = false, required = false, maxLength }: {
    label: string;
    name: string;
    value?: string;
    multiline?: boolean;
    required?: boolean;
    maxLength?: number;
}) {
    return <label>{label}{multiline ? <textarea aria-label={label} name={name} defaultValue={value} required={required}/> : <input aria-label={label} name={name} defaultValue={value} required={required} maxLength={maxLength}/>}</label>;
}
function Pagination({ total, offset, limit, onChange, language }: {
    total: number;
    offset: number;
    limit: number;
    onChange: (offset: number) => void;
    language: Language;
}) {
    const t = managementResources[language];
    return <div className="action-row">
    <span>{t.total}: {total} · {t.offset}: {offset}</span>
    <Button disabled={!offset} onClick={() => onChange(Math.max(0, offset - limit))}>{t.previous}</Button>
    <Button disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>{t.next}</Button>
    </div>;
}
function Details({ entries }: {
    entries: [
        string,
        ReactNode
    ][];
}) {
    return <dl>{entries.map(([label, value]) => <div key={label}>
        <dt>{label}</dt>
        <dd>{value}</dd>
        </div>)}</dl>;
}
export function Sites({ client, language }: Props) {
    const t = managementResources[language], m = useManagement(client);
    const [params, setParams] = useSearchParams();
    const offset = Math.max(0, Number(params.get('offset')) || 0), limit = 20;
    const [created, setCreated] = useState<Site | null>(null);
    const query = params.toString();
    const searchParams = new URLSearchParams(query);
    searchParams.set('offset', String(offset)); searchParams.set('limit', String(limit));
    const status = useStatusRefresh<Page<SiteSummary>>(client, m, '/workspace/sites?' + searchParams);
    const { result, load } = status;
    function search(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        const values = formValues(event.currentTarget);
        setParams(values.value ? { [values.mode]: values.value } : {});
    }
    return <>
    <h2>{resources[language].sites}</h2>
    <Feedback m={m} language={language}/>
    <StatusRefresh status={status} language={language}/>
    <Panel title={resources[language].sites}>
    <AdminForm m={m} className="inline-form" onSubmit={search}>
    <label>{t.searchMode}<select key={query} aria-label={t.searchMode} name="mode" defaultValue={params.has('site_id') ? 'site_id' : 'q'}>
    <option value="q">{t.text}</option>
    <option value="site_id">{t.exact}</option>
    </select>
    </label>
    <Field key={query} label={t.search} name="value" value={params.get('q') ?? params.get('site_id') ?? ''}/>
    <Button type="submit" disabled={m.busy}>{t.apply}</Button>
    <Button id="reload" onClick={() => { if (!m.isBusy()) void load(); }}>{t.reload}</Button>
    </AdminForm>{!result && !m.error && !status.error && <p role="status">{t.loading}</p>}<div className="table-scroll">
    <table>
    <thead>
    <tr>{[t.name, t.address, t.count, t.latest, t.siteId].map(label => <th key={label}>{label}</th>)}</tr>
    </thead>
    <tbody id="sites">{result?.items.map(item => <tr key={item.site.site_id}>
        <td>
        <Link to={'/sites/' + item.site.site_id}>{siteLabel(item.site)}</Link>
        </td>
        <td>{item.site.address}</td>
        <td>{item.reading_count}</td>
        <td>{timestamp(item.latest_reading_received_at, t.unavailable)}</td>
        <td className="identifier">
        <Link to={'/sites/' + item.site.site_id}>{item.site.site_id}</Link>
        </td>
        </tr>)}{result?.items.length === 0 && <tr>
        <td colSpan={5}>{t.emptySites}</td>
        </tr>}</tbody>
    </table>
    </div>{result && <Pagination language={language} total={result.total} offset={offset} limit={limit} onChange={value => { const next = new URLSearchParams(params); next.set('offset', String(value)); setParams(next); }}/>}</Panel>
    <Panel title={t.separateSite}>
    <AdminForm m={m} id="create-site" onSubmit={event => { event.preventDefault(); const data = formValues(event.currentTarget); void m.run(async () => { const site = await m.request<Site>('/sites', 'POST', data); setCreated(site); m.setMessage(t.created); await load(true); }, true); }}>
    <Field label={t.siteId} name="site_id" required/>
    <Field label={t.name} name="display_name" maxLength={100}/>
    <Field label={t.address} name="address" multiline/>
    <Field label={t.description} name="description" multiline/>
    <Button type="submit" disabled={m.busy}>{t.createSite}</Button>
    </AdminForm>{created && <Link to={'/sites/' + created.site_id}>{t.openSite}</Link>}</Panel>
    </>;
}
export function Devices({ client, language }: Props) {
    const t = managementResources[language], m = useManagement(client);
    const [params, setParams] = useSearchParams(), offset = Math.max(0, Number(params.get('offset')) || 0), limit = 20;
    const [created, setCreated] = useState('');
    const status = useStatusRefresh<Page<DeviceSummary>>(client, m, `/devices?offset=${offset}&limit=${limit}`);
    const { result, load } = status;
    return <>
    <h2>{resources[language].devices}</h2>
    <Feedback m={m} language={language}/>
    <StatusRefresh status={status} language={language}/>
    <Panel title={resources[language].devices}>
    <Button id="reload" onClick={() => { if (!m.isBusy()) void load(); }}>{t.reload}</Button>{!result && !m.error && !status.error && <p role="status">{t.loading}</p>}<div id="devices">{result?.items.map(({ device, latest_reading_received_at }) => <article key={device.device_id}>
        <Link to={'/devices/' + device.device_id}>{deviceLabel(device)}</Link>
        <Details entries={[[t.deviceId, device.device_id], [t.contact, timestamp(device.last_authenticated_contact_at, t.unavailable)], [t.latest, timestamp(latest_reading_received_at, t.unavailable)]]}/>
        </article>)}{result?.items.length === 0 && <p>{t.emptyDevices}</p>}</div>{result && <Pagination language={language} total={result.total} offset={offset} limit={limit} onChange={value => setParams({ offset: String(value) })}/>}</Panel>
    <Panel title={t.separateDevice}>
    <AdminForm m={m} id="create-device" onSubmit={event => { event.preventDefault(); const data = formValues(event.currentTarget); void m.run(async () => { const device = await m.request<{
        device_id: string;
    }>('/devices', 'POST', data); setCreated(device.device_id); m.setMessage(t.created); await load(true); }, true); }}>
    <Field name="device_id" label={t.deviceId} required/>
    <Button type="submit" disabled={m.busy}>{t.createDevice}</Button>
    </AdminForm>{created && <Link to={'/devices/' + created}>{t.openDevice}</Link>}</Panel>
    </>;
}
export function SiteView(props: Props) { const { id } = useParams(); return <SiteSettings key={id} {...props} id={id!}/>; }
function SiteSettings({ client, language, id }: Props & {
    id: string;
}) {
    const t = managementResources[language], m = useManagement(client);
    const [revision, setRevision] = useState(0);
    const status = useStatusRefresh<SiteStatus>(client, m, '/sites/' + id);
    const { result, load } = status;
    const [confirmation, setConfirmation] = useState<Record<string, string> | null>(null);
    const [params] = useSearchParams(), tab = params.get('tab') ?? 'overview';
    return <>
    <h2>{result ? siteLabel(result.site) : resources[language].site}</h2>
    <p className="identifier">{id}</p>
    <nav className="site-tabs" aria-label={resources[language].site}>{(['overview', 'usage', 'phases', 'quality', 'raw', 'settings'] as const).map(key => <Link key={key} className={tab === key ? 'active' : ''} to={(() => { const next = new URLSearchParams(params); next.set('tab', key); return '?' + next; })()} onClick={() => { setConfirmation(null); }}>{t[key]}</Link>)}</nav>
    <Feedback m={m} language={language}/>
    <div hidden={tab !== 'settings'}><StatusRefresh status={status} language={language}/>
    <Button id="reload" onClick={() => { if (!m.isBusy()) void load(); }}>{t.reload}</Button></div>{!result && !m.error && !status.error && <p role="status">{t.loading}</p>}{tab === 'overview' || tab === 'usage' || tab === 'phases' || tab === 'quality' ? <SiteAnalysis client={client} language={language} id={id} tab={tab}/> : tab === 'raw' ? <SiteRaw client={client} language={language} id={id}/> : tab !== 'settings' ? <Panel title={t[tab as 'overview'] ?? t.overview}>
        <p>{t.upcoming}</p>
        </Panel> : result && <>
        <Panel title={t.settings}>
        <div id="status">
        <Details entries={[[t.siteId, id], [t.name, result.site.display_name || t.none], [t.address, result.site.address], [t.description, result.site.description], [t.count, result.reading_count], [t.latest, timestamp(result.latest_reading_received_at, t.unavailable)], [t.authorized, result.authorized_device_ids.length ? result.authorized_device_ids.map(device => <Link key={device} to={'/devices/' + device}>{device} </Link>) : t.none]]}/>
        </div>
        <AdminForm m={m} key={revision} id="edit-site" onSubmit={event => { event.preventDefault(); setConfirmation(formValues(event.currentTarget)); }}>
        <Field name="display_name" label={t.name} maxLength={100} value={result.site.display_name}/>
        <Field name="address" label={t.address} multiline value={result.site.address}/>
        <Field name="description" label={t.description} multiline value={result.site.description}/>
        <Button type="submit" disabled={m.busy}>{t.save}</Button>
        </AdminForm>
        </Panel>
        <ConfirmationDialog open={!!confirmation} title={t.confirmTitle} description={t.metadataConfirm} confirmLabel={t.confirm} cancelLabel={t.cancel} busy={m.busy} onCancel={() => setConfirmation(null)} onConfirm={() => { if (!confirmation)
            return; const data = confirmation; void m.run(async () => { setConfirmation(null); await m.request('/sites/' + id, 'PATCH', data); if (await load(true)) setRevision(value => value + 1); m.setMessage(t.saved); }, true); }}/>
        </>}</>;
}
export function DeviceView(props: Props) { const { id } = useParams(); return <DeviceSettings key={id} {...props} id={id!}/>; }
function DeviceSettings({ client, language, id }: Props & {
    id: string;
}) {
    const t = managementResources[language], m = useManagement(client);
    const status = useStatusRefresh<DeviceDetail>(client, m, '/devices/' + id);
    const { result, load } = status;
    const [confirmation, setConfirmation] = useState<{
        kind: 'disable' | 'revoke';
        id: string;
    } | null>(null);
    return <>
    <h2>{result ? deviceLabel(result.device) : resources[language].device}</h2>
    <p className="identifier">{id}</p>
    <Feedback m={m} language={language}/>
    <StatusRefresh status={status} language={language}/>
    <Button id="reload" onClick={() => { if (!m.isBusy()) void load(); }}>{t.reload}</Button>{!result && !m.error && !status.error && <p role="status">{t.loading}</p>}{result && <>
        <Panel title={resources[language].device}>
        <div id="status">
        <Details entries={[[t.deviceId, id], [t.contact, timestamp(result.device.last_authenticated_contact_at, t.unavailable)], [t.latest, timestamp(result.status.latest_reading_received_at, t.unavailable)]]}/>
        </div>
        <AdminForm m={m} onSubmit={event => { event.preventDefault(); const data = formValues(event.currentTarget); void m.run(async () => { await m.request('/devices/' + id, 'PATCH', data); await load(true); m.setMessage(t.saved); }, true); }}>
        <Field name="display_name" label={t.name} value={result.device.display_name} maxLength={100}/>
        <Button type="submit" disabled={m.busy}>{t.saveName}</Button>
        </AdminForm>
        </Panel>
        <Panel title={t.credentials}>
        <div id="credentials">{result.credentials.map(credential => <article key={credential.credential_id}>
            <p className="identifier">{credential.credential_id}</p>
            <Details entries={[[t.createdAt, timestamp(credential.created_at)], [t.disabledAt, timestamp(credential.disabled_at, t.none)]]}/>{!credential.disabled_at && <Button disabled={m.busy} onClick={() => setConfirmation({ kind: 'disable', id: credential.credential_id })}>{t.disable}</Button>}</article>)}{!result.credentials.length && <p>{t.noCredentials}</p>}</div>{!result.credentials.length && <AdminForm m={m} id="first-credential" onSubmit={event => { event.preventDefault(); const input = event.currentTarget.elements.namedItem('token') as HTMLInputElement; const token = input.value; input.value = ''; if (!token)
                return; void m.run(async () => { await m.request('/devices/' + id + '/credentials', 'POST', { token }); await load(true); }, true); }}>
            <label>{t.first}<input name="token" type="password" autoComplete="off" required/>
            </label>
            <Button type="submit" disabled={m.busy}>{t.registerFirst}</Button>
            </AdminForm>}</Panel>
        <Panel title={t.grants}>
        <div id="grants">{result.sites.map(site => <article key={site.site_id}>
            <Link to={'/sites/' + site.site_id}>{siteLabel(site)} · {site.site_id}</Link>
            <Button disabled={m.busy} onClick={() => setConfirmation({ kind: 'revoke', id: site.site_id })}>{t.revoke}</Button>
            </article>)}{!result.sites.length && <p>{t.noGrants}</p>}</div>
        <AdminForm m={m} id="add-grant" onSubmit={event => { event.preventDefault(); const site = formValues(event.currentTarget).site_id; void m.run(async () => { await m.request('/devices/' + id + '/sites/' + encodeURIComponent(site), 'PUT', {}); await load(true); }, true); }}>
        <Field name="site_id" label={t.siteId} required/>
        <Button type="submit" disabled={m.busy}>{t.grant}</Button>
        </AdminForm>
        </Panel>
        <ConfirmationDialog open={!!confirmation} title={t.confirmTitle} description={confirmation?.kind === 'disable' ? t.disableConfirm : t.revokeConfirm} confirmLabel={t.confirm} cancelLabel={t.cancel} busy={m.busy} onCancel={() => setConfirmation(null)} onConfirm={() => { if (!confirmation)
            return; const action = confirmation; void m.run(async () => { setConfirmation(null); await m.request('/devices/' + id + (action.kind === 'disable' ? '/credentials/' + action.id + '/disable' : '/sites/' + action.id), action.kind === 'disable' ? 'POST' : 'DELETE', action.kind === 'disable' ? {} : undefined); setConfirmation(null); await load(true); }, true); }}/>
        </>}</>;
}
function RegistrationDetails({ result, language }: {
    result: RegistrationResult;
    language: Language;
}) {
    const t = managementResources[language];
    const action = (value: string) => value === 'create' ? t.create : value === 'reuse' ? t.reuse : value;
    return <Details entries={[[t.siteId, result.site.site_id], [t.deviceId, result.device_id], [t.address, result.site.address], [t.description, result.site.description], [t.name, result.site.display_name || t.none], [t.siteAction, action(result.actions.site)], [t.deviceAction, action(result.actions.device)], [t.credentialAction, action(result.actions.credential)], [t.grantAction, action(result.actions.grant)], [t.credentialMask, result.credential_mask]]}/>;
}
export function Registration({ client, language }: Props) {
    const t = managementResources[language], m = useManagement(client);
    const [preview, setPreview] = useState<RegistrationResult | null>(null), [result, setResult] = useState<RegistrationResult | null>(null);
    const [refreshFailed, setRefreshFailed] = useState(false);
    const [submittedMetadata, setSubmittedMetadata] = useState<{ address: string; description: string } | null>(null);
    const raw = useRef(''), input = useRef<HTMLTextAreaElement>(null), version = useRef(0);
    const location = useLocation();
    const [refreshOwner] = useState(() => new RequestOwner());
    function clear() { version.current++; raw.current = ''; if (input.current)
        input.current.value = ''; m.owner.cancel(); refreshOwner.cancel(); setPreview(null); setSubmittedMetadata(null); setResult(null); setRefreshFailed(false); m.setError(''); }
    useEffect(() => { const hide = () => clear(); window.addEventListener('pagehide', hide); return () => { raw.current = ''; if (input.current)
        input.current.value = ''; refreshOwner.cancel(); window.removeEventListener('pagehide', hide); }; }, []);
    // Even a same-view navigation invalidates the registration lifetime.
    useEffect(() => { clear(); }, [location.key]);
    return <>
    <h2>{t.registration}</h2>
    <Feedback m={m} language={language}/>{refreshFailed && <p id="refresh-error" role="alert">{t.refreshFailed}</p>}<Panel title={t.registration}>
    <p>{t.registrationHelp}</p>
    <AdminForm m={m} id="registration-form" onSubmit={event => { event.preventDefault(); const submitted = input.current?.value ?? ''; if (!submitted)
        return; raw.current = ''; setPreview(null); setSubmittedMetadata(null); setResult(null); setRefreshFailed(false); refreshOwner.cancel(); m.owner.cancel(); const current = ++version.current; void m.run(async () => { const response = await m.request<RegistrationResult>('/registrations/preview', 'POST', submitted); if (current !== version.current)
        return; raw.current = submitted; if (input.current)
        input.current.value = '';
        // Select only nonsecret metadata from a summary already validated by the Server.
        const metadata = JSON.parse(submitted) as { address: string; description: string };
        setSubmittedMetadata({ address: metadata.address, description: metadata.description });
        setPreview(response); }, true); }}>
    <label>{t.json}<textarea id="summary" name="summary" required ref={input} autoComplete="off" onChange={() => { version.current++; raw.current = ''; m.owner.cancel(); refreshOwner.cancel(); setPreview(null); setSubmittedMetadata(null); setResult(null); setRefreshFailed(false); m.setError(''); }}/>
    </label>
    <div className="action-row"><Button type="submit" disabled={m.busy}>{t.preview}</Button>
    <Button id="cancel-registration" onClick={() => { clear(); m.setMessage(t.cancelled); }}>{t.cancelRegistration}</Button></div>
    </AdminForm>
    <section id="registration-preview" hidden={!preview}>{preview && <>
        <h3>{t.previewTitle}</h3>
        <div id="preview">
        <h4>{t.serverPreview}</h4>
        <RegistrationDetails result={preview} language={language}/>
        {submittedMetadata && <><h4>{t.submittedMetadata}</h4><Details entries={[[t.address, submittedMetadata.address], [t.description, submittedMetadata.description]]}/></>}
        <p>{preview.metadata_differs ? t.differs : t.preserved}</p>
        </div>
        </>}<Button id="register" disabled={m.busy || !preview} onClick={() => {
            if (!raw.current || !preview)
                return;
            const submitted = raw.current, current = version.current;
            void m.run(async () => {
                const response = await m.request<RegistrationResult>('/registrations', 'POST', submitted);
                if (current !== version.current)
                    return;
                raw.current = '';
                if (input.current)
                    input.current.value = '';
                setPreview(null);
                setSubmittedMetadata(null);
                setResult(response);
                const completed = ++version.current; // Commit success clears secrets before the independent refresh starts.
                void m.request('/sites?limit=20', 'GET', undefined, refreshOwner).catch(error => { if (completed === version.current && !aborted(error)) {
                    setRefreshFailed(true);
                    m.setError(errorCode(error));
                } });
            }, true);
        }}>{t.commit}</Button>
    </section>
    <div id="registration-result">{result && <>
        <p>{t.applied}</p>
        <RegistrationDetails result={result} language={language}/>
        <div className="action-row">
        <Link to={'/sites/' + result.site.site_id}>{t.registeredSite}</Link>
        <Link to={'/devices/' + result.device_id}>{t.registeredDevice}</Link>
        </div>
        </>}</div>
    </Panel>
    </>;
}
