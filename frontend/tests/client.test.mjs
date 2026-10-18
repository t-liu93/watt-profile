import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/lib/api.ts', import.meta.url), 'utf8')
const { APIClient, RequestOwner, APIError } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpile(source, { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext })).toString('base64')}`)
test('old request ownership aborts transport and rejects a late transport result', async () => {
  const owner = new RequestOwner()
  let finish, signal
  const pending = owner.run(s => { signal = s; return new Promise(resolve => { finish = resolve }) })
  owner.cancel()
  assert.equal(signal.aborted, true)
  finish('sensitive old value')
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(await owner.run(async () => 'new context'), 'new context')
})
test('same-origin client adds CSRF only to mutations and never retries', async t => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original })
  const calls = []
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); return Response.json({ data: { ok: true } }) }
  const client = new APIClient('synthetic-csrf', () => assert.fail('expired'))
  await client.request('/api/admin/v1/devices')
  await client.request('/api/admin/v1/devices', { method: 'POST', body: '{}' })
  assert.equal(calls[0].options.headers.has('X-CSRF-Token'), false)
  assert.equal(calls[1].options.headers.get('X-CSRF-Token'), 'synthetic-csrf')
  assert.equal(calls[1].options.credentials, 'same-origin')
  assert.equal(calls[1].options.cache, 'no-store')
  await assert.rejects(client.request('https://other.invalid/'), /Management path required/)
  globalThis.fetch = async () => { calls.push({}); return Response.json({ error: { code: 'csrf_rejected' } }, { status: 403 }) }
  await assert.rejects(client.request('/api/admin/v1/devices', { method: 'POST' }), error => error instanceof APIError && error.code === 'csrf_rejected')
  assert.equal(calls.length, 3)
})
test('auth expiry cancels every owner, clears client and calls login return once', async t => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original })
  let expired = 0, finish
  const client = new APIClient('synthetic', () => expired++)
  const owner = client.owner()
  const pending = owner.run(() => new Promise(resolve => { finish = resolve }))
  globalThis.fetch = async () => Response.json({ error: { code: 'authentication_required' } }, { status: 401 })
  await assert.rejects(client.request('/api/admin/v1/devices'), { name: 'AbortError' })
  finish('old business state')
  await assert.rejects(pending, { name: 'AbortError' })
  await assert.rejects(client.request('/api/admin/v1/devices'), { name: 'AbortError' })
  assert.equal(expired, 1)
})
test('old context failures cannot overwrite the current view error', async () => {
  const owner = new RequestOwner()
  let fail
  const pending = owner.run(() => new Promise((_, reject) => { fail = reject }))
  owner.cancel()
  fail(new APIError('temporarily_unavailable', 503))
  await assert.rejects(pending, { name: 'AbortError' })
})
test('clear aborts direct mutations and publishes a sensitive state reset', async t => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original })
  let signal, reset = 0
  const client = new APIClient('synthetic-csrf', () => {})
  const unsubscribe = client.subscribe(() => reset++)
  globalThis.fetch = async (_, options) => {
    signal = options.signal
    return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError'))))
  }
  const pending = client.request('/api/admin/v1/devices', { method: 'POST', body: '{}' })
  client.clear()
  assert.equal(signal.aborted, true)
  assert.equal(client.isClosed(), true)
  assert.equal(reset, 1)
  await assert.rejects(pending, { name: 'AbortError' })
  unsubscribe()
})
test('a timed-out mutation reports uncertainty without retrying', async t => {
  const originalFetch = globalThis.fetch, originalTimer = globalThis.setTimeout
  t.after(() => { globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimer })
  globalThis.setTimeout = callback => originalTimer(callback, 1)
  let calls = 0
  globalThis.fetch = async (_path, { signal }) => {
    calls++
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('Timeout', 'AbortError')), { once: true }))
  }
  const client = new APIClient('synthetic', () => assert.fail('not expired'))
  await assert.rejects(client.request('/api/admin/v1/sites', { method: 'POST', body: '{}' }), error => error instanceof APIError && error.code === 'timeout')
  assert.equal(calls, 1)
  assert.equal(client.isClosed(), false)
})
