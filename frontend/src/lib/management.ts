import { useCallback, useEffect, useRef, useState } from 'react';
import { APIClient, APIError, RequestOwner } from './api';
export interface Site {
    site_id: string;
    display_name: string;
    address: string;
    description: string;
}
export interface Device {
    device_id: string;
    display_name: string;
    created_at: string;
    last_authenticated_contact_at: string | null;
}
export interface Page<T> {
    items: T[];
    total: number;
}
export interface SiteSummary {
    site: Site;
    reading_count: number;
    latest_reading_received_at: string | null;
}
export interface SiteStatus extends SiteSummary {
    authorized_device_ids: string[];
}
export interface DeviceSummary {
    device: Device;
    latest_reading_received_at: string | null;
}
export interface DeviceDetail {
    device: Device;
    status: DeviceSummary;
    credentials: {
        credential_id: string;
        created_at: string;
        disabled_at: string | null;
    }[];
    sites: Site[];
}
export interface RegistrationResult {
    site: Site;
    device_id: string;
    credential_id: string | null;
    actions: Record<'site' | 'device' | 'credential' | 'grant', string>;
    metadata_differs: boolean;
    credential_mask: string;
}
export const prefix = '/api/admin/v1';
export const siteLabel = (site: Site) => site.display_name || site.address || site.site_id.slice(0, 8);
export const deviceLabel = (device: Device) => device.display_name || device.device_id.slice(0, 8);
export const formValues = (form: HTMLFormElement) => Object.fromEntries(new FormData(form)) as Record<string, string>;
export const errorCode = (error: unknown) => error instanceof APIError ? error.code : 'unknown';
export const aborted = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';
/** Request lifetime and a synchronous mutation lock are shared by administration forms. */
export function useManagement(client: APIClient) {
    const [owner] = useState(() => client.owner());
    const [readOwner] = useState(() => client.owner());
    const lock = useRef(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [message, setMessage] = useState('');
    useEffect(() => () => { client.release(owner); client.release(readOwner); }, [client, owner, readOwner]);
    const request = <T,>(path: string, method = 'GET', body?: unknown, target: RequestOwner = method === 'GET' ? readOwner : owner) => target.run(signal => client.request<T>(prefix + path, { signal, method, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: typeof body === 'string' ? body : JSON.stringify(body) }) }));
    async function run(action: () => Promise<void>, mutation = false) {
        if (mutation && lock.current)
            return;
        if (mutation) {
            readOwner.cancel();
            lock.current = true;
            setBusy(true);
        }
        setError('');
        setMessage('');
        try {
            await action();
        }
        catch (error) {
            if (!aborted(error))
                setError(errorCode(error));
        }
        finally {
            if (mutation) {
                lock.current = false;
                setBusy(false);
            }
        }
    }
    return { owner, readOwner, request, run, busy, isBusy: () => lock.current, error, setError, message, setMessage };
}

/** Only lightweight management status reads participate; mutation ownership stays separate. */
export function useStatusRefresh<T>(client: APIClient, management: ReturnType<typeof useManagement>, path: string) {
    const [result, setResult] = useState<T | null>(null);
    const [updatedAt, setUpdatedAt] = useState('');
    const [error, setError] = useState('');
    const [paused, setPaused] = useState(false);
    const state = useRef({ management, paused });
    state.current = { management, paused };
    const pending = useRef<Promise<boolean> | null>(null);
    const generation = useRef(0);
    const load = useCallback((afterMutation = false): Promise<boolean> => {
        if (document.hidden || client.isClosed() || (!afterMutation && state.current.management.isBusy())) return Promise.resolve(false);
        if (pending.current && !afterMutation) return pending.current;
        if (afterMutation) state.current.management.readOwner.cancel();
        const current = ++generation.current;
        const task = (async () => {
            try {
                const value = await state.current.management.request<T>(path);
                if (generation.current !== current) return false;
                setResult(value);
                setUpdatedAt(new Date().toISOString());
                setError('');
                return true;
            } catch (error) {
                if (generation.current === current && !aborted(error)) setError(errorCode(error));
                return false;
            } finally {
                if (generation.current === current) pending.current = null;
            }
        })();
        pending.current = task;
        return task;
    }, [client, path]);
    useEffect(() => {
        setResult(null); setUpdatedAt(''); setError('');
        void load();
        const poll = () => { if (!state.current.paused) void load(); };
        const visibility = () => {
            if (document.hidden) {
                generation.current++; pending.current = null;
                state.current.management.readOwner.cancel();
            } else poll();
        };
        const timer = window.setInterval(poll, 10000);
        document.addEventListener('visibilitychange', visibility);
        return () => {
            window.clearInterval(timer);
            document.removeEventListener('visibilitychange', visibility);
            generation.current++; pending.current = null;
            state.current.management.readOwner.cancel();
        };
    }, [load]);
    return { result, updatedAt, error, paused, setPaused, load };
}
