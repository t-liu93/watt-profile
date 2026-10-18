export class APIError extends Error {
  constructor(public code: string, public status = 0) { super(code) }
}
/** One owner per view; replacing its context cancels and invalidates every old result. */
export class RequestOwner {
  private version = 0
  private controllers = new Set<AbortController>()
  cancel() { this.version++; for (const controller of this.controllers) controller.abort(); this.controllers.clear() }
  async run<T>(request: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const version = this.version
    const controller = new AbortController()
    this.controllers.add(controller)
    try {
      const value = await request(controller.signal)
      if (controller.signal.aborted || this.version !== version) throw new DOMException('Obsolete response', 'AbortError')
      return value
    } catch (error) {
      if (controller.signal.aborted || this.version !== version) throw new DOMException('Obsolete response', 'AbortError')
      throw error
    } finally { this.controllers.delete(controller) }
  }
}
export class APIClient {
  private owners = new Set<RequestOwner>()
  private expired = false
  private listeners = new Set<() => void>()
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  isClosed = () => this.expired
  private pending = new Set<AbortController>()
  constructor(private csrf: string, private onExpired: () => void) {}
  owner() { const owner = new RequestOwner(); this.owners.add(owner); return owner }
  release(owner: RequestOwner) { owner.cancel(); this.owners.delete(owner) }
  clear() {
    if (this.expired) return
    this.expired = true
    this.csrf = ''
    for (const controller of this.pending) controller.abort()
    this.pending.clear()
    for (const owner of this.owners) owner.cancel()
    this.owners.clear()
    for (const listener of this.listeners) listener()
  }
  async request<T>(path: string, options: RequestInit = {}): Promise<T> {
    if (this.expired) throw new DOMException('Session ended', 'AbortError')
    const url = new URL(path, 'http://management.invalid')
    if (!path.startsWith('/api/admin/v1/') || path.includes('\\') || !url.pathname.startsWith('/api/admin/v1/')) throw new Error('Management path required')
    const headers = new Headers(options.headers)
    if (!['GET', 'HEAD'].includes((options.method ?? 'GET').toUpperCase())) headers.set('X-CSRF-Token', this.csrf)
    const controller = new AbortController()
    this.pending.add(controller)
    let timedOut = false
    const timer = globalThis.setTimeout(() => { timedOut = true; controller.abort() }, 22000)
    const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal
    try {
      let response: Response
      try {
        response = await fetch(path, { ...options, signal, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error
        throw new APIError('connection')
      }
      if (this.expired || signal.aborted) throw new DOMException('Session ended', 'AbortError')
      if (response.status === 401) {
        this.clear()
        this.onExpired()
        throw new DOMException('Session ended', 'AbortError')
      }
      if (response.status === 204) return undefined as T
      const result = await response.json()
      if (this.expired || signal.aborted) throw new DOMException('Session ended', 'AbortError')
      if (!response.ok) throw new APIError(result.error?.code ?? 'unknown', response.status)
      return result.data as T
    } catch (error) {
      if (timedOut && !this.expired && !options.signal?.aborted) throw new APIError('timeout')
      if (this.expired || signal.aborted) throw new DOMException('Session ended', 'AbortError')
      throw error
    } finally { globalThis.clearTimeout(timer); this.pending.delete(controller) }
  }
}
