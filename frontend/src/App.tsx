import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { FormEvent } from 'react'
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { Button, NavigationDialog } from './components/ui'
import { Devices, DeviceView, Registration, Sites, SiteView } from './Management'
import { APIClient } from './lib/api'
import { initialLanguage, initialTheme, resources, savePreference } from './i18n'
import type { Language } from './i18n'

const csrf = () => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? ''
export function App() {
  const [language, setLanguage] = useState<Language>(initialLanguage)
  const [theme, setTheme] = useState(initialTheme)
  const [expired, setExpired] = useState(false)
  const [client] = useState(() => new APIClient(csrf(), () => setExpired(true)))
  const closed = useSyncExternalStore(client.subscribe, client.isClosed)
  const location = useLocation()
  const t = resources[language]
  const login = location.pathname === '/login'
  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en'
    document.documentElement.dataset.theme = theme
    document.title = login ? t.login : t.app
  }, [language, theme, login, t])
  useEffect(() => {
    const update = () => { document.documentElement.dataset.hidden = String(document.hidden) }
    update(); document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  useEffect(() => {
    if (!expired) return
    // Unmount the protected tree before returning to a fresh pre-login form.
    document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.remove()
    window.location.replace('/login')
  }, [expired])
  useEffect(() => {
    const clear = () => client.clear()
    const restore = (event: PageTransitionEvent) => { if (event.persisted) window.location.reload() }
    window.addEventListener('pagehide', clear)
    window.addEventListener('pageshow', restore)
    return () => { window.removeEventListener('pagehide', clear); window.removeEventListener('pageshow', restore) }
  }, [client])
  const preferences = <div className="preferences"><label><span className="sr-only">{t.language}</span><select aria-label={t.language} value={language} onChange={event => { const value = event.target.value as Language; setLanguage(value); savePreference('language', value) }}><option value="en">English</option><option value="zh">中文</option></select></label><Button id="theme-toggle" onClick={() => { const value = theme === 'dark' ? 'light' : 'dark'; setTheme(value); savePreference('theme', value) }}>{theme === 'dark' ? t.light : t.dark}</Button></div>
  const nav = <nav aria-label={t.menu}>{[['/', t.sites], ['/devices', t.devices], ['/registration', t.registration]].map(([path, label]) => <NavLink key={path} to={path} end={path === '/'} className={({ isActive }) => isActive || (path === '/' && location.pathname.startsWith('/sites')) ? 'active' : ''}>{label}</NavLink>)}</nav>
  return <><a className="skip-link" href="#content">{t.skip}</a><header className="app-header"><div className="brand">W<span aria-hidden="true"> / </span>PROFILE</div><h1>{login ? t.login : t.app}</h1>{preferences}{!login && !expired && <form method="post" action="/logout" onSubmit={() => client.clear()}><input type="hidden" name="csrf" value={csrf()} /><Button type="submit">{t.logout}</Button></form>}</header>{login ? <main id="content" className="login-main"><Login language={language} /></main> : !expired && !closed && <div className="workspace"><aside className="sidebar">{nav}<p>{t.workbench}</p></aside><div className="mobile-navigation"><NavigationDialog title={t.menu} closeLabel={t.close}>{nav}</NavigationDialog></div><main id="content" className="content" tabIndex={-1}><Routes><Route path="/" element={<Sites language={language} client={client} />} /><Route path="/sites" element={<Sites language={language} client={client} />} /><Route path="/devices" element={<Devices language={language} client={client} />} /><Route path="/registration" element={<Registration language={language} client={client} />} /><Route path="/sites/:id" element={<SiteView language={language} client={client} />} /><Route path="/devices/:id" element={<DeviceView language={language} client={client} />} /><Route path="*" element={<><h2>{t.notFound}</h2><Link to="/">{t.home}</Link></>} /></Routes></main></div>}</>
}
function Login({ language }: { language: Language }) {
  const t = resources[language]
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const formRef = useRef<HTMLFormElement>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => {
    const clear = () => { controller.current?.abort(); formRef.current?.reset() }
    window.addEventListener('pagehide', clear)
    return () => { clear(); window.removeEventListener('pagehide', clear) }
  }, [])
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const form = event.currentTarget
    const missing = Array.from(form.querySelectorAll<HTMLInputElement>('input[required]')).find(input => !input.value)
    if (missing) { setError('required'); missing.focus(); return }
    const data = new URLSearchParams(new FormData(form) as unknown as Record<string, string>)
    setBusy(true); setError('')
    controller.current = new AbortController()
    const timer = window.setTimeout(() => controller.current?.abort(), 22000)
    try {
      const response = await fetch('/login', { method: 'POST', body: data, credentials: 'same-origin', cache: 'no-store', signal: controller.current.signal })
      if (response.ok && response.redirected) { window.location.assign('/'); return }
      const result = await response.json()
      setError(result.error?.code ?? 'unknown')
    } catch { setError('connection') }
    finally { window.clearTimeout(timer); (form.elements.namedItem('password') as HTMLInputElement).value = ''; setBusy(false) }
  }
  return <section className="neon-panel login-panel"><span className="eyebrow">{t.accessEyebrow}</span><p>{t.loginHelp}</p>{error && <p id="error" role="alert">{t.errors[error as keyof typeof t.errors] ?? t.errors.unknown}</p>}<form ref={formRef} method="post" action="/login" noValidate aria-describedby={error ? 'error' : undefined} onSubmit={submit}><input type="hidden" name="csrf" value={csrf()} /><label>{t.username}<input name="username" autoComplete="username" required /></label><label>{t.password}<input name="password" type="password" autoComplete="current-password" required /></label><Button type="submit" disabled={busy}>{t.submit}</Button></form><a href="/login">{t.reloadLogin}</a></section>
}
