import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, copyFile, readFile, writeFile, rm, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { ROOT, FORMAT, TRANSFORM, sha, fingerprint, sourceURL, parseResource, prepare, verify, update } from '../scripts/ui.mjs'

const commit = 'a'.repeat(40)
const configFiles = ['components.json', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts', 'scripts/ui.mjs']
async function fixture(t) {
  const root = await mkdtemp(resolve(tmpdir(), 'watt-ui-test-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  for (const path of configFiles) {
    await mkdir(dirname(resolve(root, path)), { recursive: true })
    await copyFile(resolve(ROOT, path), resolve(root, path))
  }
  const pkg = JSON.parse(await readFile(resolve(root, 'package.json')))
  const resources = {
    button: { name: 'button', type: 'registry:ui', registryDependencies: ['helper'], files: [{ path: 'ui/button.tsx', type: 'registry:ui', content: 'import { cn } from "@/lib/utils"\nexport const Button = cn("fixture")\n' }] },
    helper: { name: 'helper', type: 'registry:lib', registryDependencies: ['utils'], files: [{ path: 'lib/helper.ts', type: 'registry:lib', content: 'export const helper = true\n' }] },
    utils: { name: 'utils', type: 'registry:lib', dependencies: ['clsx'], files: [{ path: 'lib/utils.ts', type: 'registry:lib', content: 'export const cn = (s: string) => s\n' }] },
  }
  const payloads = Object.fromEntries(Object.entries(resources).map(([n, r]) => [n, Buffer.from(JSON.stringify(r))]))
  const lock = { formatVersion: FORMAT, transformVersion: TRANSFORM, base: 'radix', style: 'new-york', upstreamCommit: commit, selected: ['button'], configurationSHA256: await fingerprint(root), resources: {}, generated: {} }
  for (const [name, payload] of Object.entries(payloads)) {
    const parsed = parseResource(payload, name, pkg)
    lock.resources[name] = { url: sourceURL(commit, name), sha256: sha(payload), dependencies: parsed.edges, npmDependencies: parsed.npm }
    for (const f of parsed.files) lock.generated[f.path] = f.sha256
  }
  await writeFile(resolve(root, 'ui-components.lock.json'), JSON.stringify(lock))
  const fetcher = async url => {
    const name = url.split('/').at(-1).replace('.json', '')
    return new Response(payloads[name], { status: 200 })
  }
  return { root, lock, pkg, resources, payloads, fetcher }
}
test('recursive helper inputs restore identically twice without changing configuration or locks', async t => {
  const a = await fixture(t), b = await fixture(t)
  const snapshot = async root => Promise.all([...configFiles, 'ui-components.lock.json'].map(p => readFile(resolve(root, p))))
  const before = await snapshot(a.root)
  await prepare(a.root, { fetcher: a.fetcher })
  await prepare(b.root, { fetcher: b.fetcher })
  await verify(a.root)
  for (const p of Object.keys(a.lock.generated)) assert.deepEqual(await readFile(resolve(a.root, p)), await readFile(resolve(b.root, p)))
  assert.deepEqual(await snapshot(a.root), before)
  assert.equal(a.lock.resources.button.dependencies.includes('utils'), true)
})
test('damaged downloads fail before writing outputs; unavailable sources fail', async t => {
  const f = await fixture(t)
  await assert.rejects(prepare(f.root, { fetcher: async () => new Response('corrupt') }), /Input hash mismatch/)
  await assert.rejects(verify(f.root), /Generated file set/)
  await assert.rejects(prepare(f.root, { fetcher: async () => new Response('', { status: 503 }) }), /Unavailable source/)
  await assert.rejects(prepare(f.root, { fetcher: async () => { throw new Error('network unavailable') } }), /network unavailable/)
})
test('unlocked recursive dependencies and undeclared altered dependency graph fail', async t => {
  const f = await fixture(t)
  const missing = structuredClone(f.lock)
  delete missing.resources.utils
  await assert.rejects(prepare(f.root, { lock: missing, fetcher: f.fetcher }), /Unlocked dependency/)
  const altered = structuredClone(f.lock)
  altered.resources.button.dependencies = ['helper']
  await assert.rejects(prepare(f.root, { lock: altered, fetcher: f.fetcher }), /Dependency graph mismatch/)
})
test('generated hash, missing/extra files, and configuration mismatch fail verification', async t => {
  const f = await fixture(t)
  await prepare(f.root, { fetcher: f.fetcher })
  const target = resolve(f.root, 'src/generated/ui/button.tsx')
  await writeFile(target, 'corrupt')
  await assert.rejects(verify(f.root), /Generated file set/)
  await prepare(f.root, { fetcher: f.fetcher })
  await rm(target)
  await assert.rejects(verify(f.root), /Generated file set/)
  await prepare(f.root, { fetcher: f.fetcher })
  await writeFile(resolve(f.root, 'src/generated/extra.ts'), '')
  await assert.rejects(verify(f.root), /Generated file set/)
  await prepare(f.root, { fetcher: f.fetcher })
  await writeFile(resolve(f.root, 'components.json'), '{}')
  await assert.rejects(verify(f.root), /configuration fingerprint/)
  await assert.rejects(prepare(f.root, { fetcher: f.fetcher }), /configuration fingerprint/)
})
test('unsupported actions, aliases, paths, npm dependencies and versions are rejected', async t => {
  const f = await fixture(t)
  const parse = item => parseResource(JSON.stringify(item), item.name, f.pkg)
  for (const key of ['css', 'cssVars', 'envVars', 'scripts', 'registries']) assert.throws(() => parse({ ...f.resources.button, [key]: {} }), /Unsupported registry action/)
  assert.throws(() => parse({ ...f.resources.button, dependencies: ['unlocked-package'] }), /Unlocked npm/)
  for (const path of ['../escape.tsx', '/tmp/escape.tsx', 'ui/../../escape.tsx']) assert.throws(() => parse({ ...f.resources.button, files: [{ path, type: 'registry:ui', content: '' }] }), /escaping resource path/)
  assert.throws(() => parse({ ...f.resources.button, files: [{ path: 'ui/button.tsx', type: 'registry:ui', content: '', target: '/tmp/escape' }] }), /Unsupported file action/)
  assert.throws(() => parse({ ...f.resources.button, registryDependencies: ['https://floating.example/button.json'] }), /Unsupported registry dependency/)
  assert.throws(() => parse({ ...f.resources.button, files: [{ path: 'ui/button.tsx', type: 'registry:ui', content: 'import { x } from "@/unknown/helper"' }] }), /Unsupported (internal )?import/)
})
test('input/output hashes, immutable URLs, transform versions and symlink escape are rejected', async t => {
  const f = await fixture(t)
  for (const change of [l => { l.generated['src/generated/ui/button.tsx'] = 'b'.repeat(64) }, l => { l.resources.button.sha256 = 'b'.repeat(64) }]) {
    const lock = structuredClone(f.lock); change(lock)
    await assert.rejects(prepare(f.root, { lock, fetcher: f.fetcher }), /hash mismatch/)
  }
  for (const change of [l => { l.resources.button.url = 'https://example.test/button.json' }, l => { l.transformVersion = 'unknown' }, l => { l.generated['src/generated/../escape.tsx'] = 'a'.repeat(64) }]) {
    const lock = structuredClone(f.lock); change(lock)
    await assert.rejects(prepare(f.root, { lock, fetcher: f.fetcher }))
  }
  await mkdir(resolve(f.root, 'src'), { recursive: true })
  await symlink(tmpdir(), resolve(f.root, 'src/generated'))
  await assert.rejects(prepare(f.root, { fetcher: f.fetcher }), /Symlink path rejected/)
})
test('update requires explicit ref and component selection before resolving anything', async () => {
  await assert.rejects(update(ROOT, []), /Explicit/)
  await assert.rejects(update(ROOT, ['--ref=main', '--components=../escape']), /Invalid component selection/)
})
