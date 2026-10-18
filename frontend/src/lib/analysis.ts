import { useCallback, useEffect, useRef, useState } from 'react'
import { APIClient } from './api'
import { aborted, errorCode, prefix } from './management'
/** Independent latest, context, overview, and zoom lifetimes. Errors retain only same-context success. */
export function useAnalysisRead<T>(client: APIClient, path: string | null, poll = false) {
  const [owner] = useState(() => client.owner())
  const [state, setState] = useState<{ path: string | null; result: T | null; error: string; updatedAt: string; loading: boolean }>({ path, result: null, error: '', updatedAt: '', loading: false })
  const [paused, setPaused] = useState(false)
  const generation = useRef(0), pending = useRef<Promise<T | null> | null>(null), pause = useRef(paused)
  pause.current = paused
  const load = useCallback((): Promise<T | null> => {
    if (!path || client.isClosed() || (poll && document.hidden)) return Promise.resolve(null)
    if (pending.current) return pending.current
    const version = ++generation.current
    setState(old => ({ ...(old.path === path ? old : { result: null, error: '', updatedAt: '' }), path, loading: true }))
    const task = owner.run(signal => client.request<T>(prefix + path, { signal })).then(result => {
      if (version === generation.current) setState({ path, result, error: '', updatedAt: new Date().toISOString(), loading: false })
      return result
    }).catch(error => {
      if (version === generation.current && !aborted(error)) setState(old => ({ ...old, error: errorCode(error), loading: false }))
      return null
    }).finally(() => { if (version === generation.current) pending.current = null })
    pending.current = task
    return task
  }, [client, owner, path, poll])
  useEffect(() => {
    void load()
    const cancel = () => { generation.current++; pending.current = null; owner.cancel() }
    const tick = () => { if (!pause.current && !document.hidden) void load() }
    const visibility = () => { if (document.hidden) { cancel(); setState(old => ({ ...old, loading: false })) } else tick() }
    const timer = poll ? window.setInterval(tick, 10000) : undefined
    if (poll) document.addEventListener('visibilitychange', visibility)
    return () => { cancel(); if (timer) window.clearInterval(timer); document.removeEventListener('visibilitychange', visibility) }
  }, [load, owner, poll])
  useEffect(() => () => client.release(owner), [client, owner])
  const current = state.path === path ? state : { result: null, error: '', updatedAt: '', loading: !!path }
  return { ...current, paused, setPaused, load }
}
