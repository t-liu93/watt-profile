import { createHash } from 'node:crypto'
import { readFile, writeFile, mkdir, readdir, rm, lstat } from 'node:fs/promises'
import { dirname, resolve, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export const FORMAT = 1
export const TRANSFORM = 'radix-new-york-path-alias-v1'
const CONFIG_FILES = ['components.json', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts', 'scripts/ui.mjs']
const allowedItem = new Set(['$schema', 'name', 'type', 'author', 'title', 'description', 'dependencies', 'devDependencies', 'registryDependencies', 'files'])
export const sha = value => createHash('sha256').update(value).digest('hex')
const fail = message => { throw new Error(message) }
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)
const namePattern = /^[a-z][a-z0-9-]*$/
export const sourceURL = (commit, name) => `https://raw.githubusercontent.com/shadcn-ui/ui/${commit}/apps/v4/public/r/styles/new-york/${name}.json`
export async function fingerprint(root) {
  const inputs = []
  for (const name of CONFIG_FILES) inputs.push([name, sha(await readFile(resolve(root, name)))])
  return sha(JSON.stringify(inputs))
}
function safePath(path) {
  if (!/^(ui\/[a-z][a-z0-9-]*\.tsx|lib\/[a-z][a-z0-9-]*\.ts)$/.test(path)) fail(`Unsupported or escaping resource path: ${path}`)
  return `src/generated/${path}`
}
function dependencies(item) {
  const edges = new Set(item.registryDependencies ?? [])
  for (const file of item.files) for (const match of file.content.matchAll(/(?:from\s*|import\s*)["'](@\/[^"']+)["']/g)) {
    const ref = match[1]
    if (ref === '@/lib/utils') edges.add('utils')
    else if (/^@\/components\/ui\/[a-z][a-z0-9-]*$/.test(ref)) edges.add(ref.split('/').at(-1))
    else fail(`Unsupported internal import: ${ref}`)
  }
  for (const name of edges) if (!namePattern.test(name)) fail(`Unsupported registry dependency: ${name}`)
  return [...edges].sort()
}
export function parseResource(bytes, name, packageJSON) {
  const item = JSON.parse(bytes)
  for (const key of Object.keys(item)) if (!allowedItem.has(key)) fail(`Unsupported registry action/content: ${key}`)
  if (item.name !== name || !['registry:ui', 'registry:lib'].includes(item.type) || !Array.isArray(item.files) || !item.files.length) fail(`Unsupported registry item: ${name}`)
  for (const key of ['dependencies', 'devDependencies', 'registryDependencies']) if (item[key] !== undefined && (!Array.isArray(item[key]) || item[key].some(x => typeof x !== 'string'))) fail(`Invalid ${key}`)
  const installed = { ...packageJSON.dependencies, ...packageJSON.devDependencies }
  const npm = new Set([...(item.dependencies ?? []), ...(item.devDependencies ?? [])])
  const files = []
  for (const file of item.files) {
    for (const key of Object.keys(file)) if (!['path', 'content', 'type', 'target'].includes(key)) fail(`Unsupported file property: ${key}`)
    if (file.target || typeof file.content !== 'string' || file.type !== item.type) fail(`Unsupported file action: ${name}`)
    const path = safePath(file.path)
    if ((item.type === 'registry:ui') !== file.path.startsWith('ui/')) fail('Registry path/type mismatch')
    // The versioned transformation changes paths/import aliases only, never component bodies.
    const content = file.content.replaceAll('@/lib/utils', '@/generated/lib/utils').replaceAll('@/components/ui/', '@/generated/ui/')
    for (const match of content.matchAll(/(?:from\s*|import\s*)["']([^"']+)["']/g)) {
      const specifier = match[1]
      if (specifier.startsWith('@/generated/')) continue
      if (specifier.startsWith('.') || specifier.startsWith('@/')) fail(`Unsupported import: ${specifier}`)
      npm.add(specifier.startsWith('@') ? specifier.split('/').slice(0, 2).join('/') : specifier.split('/')[0])
    }
    files.push({ path, content, sha256: sha(content) })
  }
  for (const dep of npm) if (!installed[dep] || !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(installed[dep])) fail(`Unlocked npm dependency: ${dep}`)
  return { edges: dependencies(item), npm: [...npm].sort(), files }
}
async function json(root, name) { return JSON.parse(await readFile(resolve(root, name), 'utf8')) }
async function download(url, fetcher) {
  const response = await fetcher(url, { signal: AbortSignal.timeout(30000), redirect: 'error' })
  if (!response.ok) fail(`Unavailable source ${url}: ${response.status}`)
  return Buffer.from(await response.arrayBuffer())
}
export async function validateLock(root, lock) {
  if (lock.formatVersion !== FORMAT || lock.transformVersion !== TRANSFORM || lock.base !== 'radix' || lock.style !== 'new-york' || !/^[a-f0-9]{40}$/.test(lock.upstreamCommit)) fail('Unsupported component lock format/base/style/commit')
  if (lock.configurationSHA256 !== await fingerprint(root)) fail('Generation configuration fingerprint mismatch')
  if (!Array.isArray(lock.selected) || !lock.selected.length || !lock.resources || !lock.generated) fail('Invalid component lock')
  for (const name of [...lock.selected, ...Object.keys(lock.resources)]) if (!namePattern.test(name)) fail('Invalid resource name')
  const reachable = new Set()
  const visit = name => {
    if (reachable.has(name)) return
    const resource = lock.resources[name]
    if (!resource) fail(`Unlocked dependency: ${name}`)
    if (resource.url !== sourceURL(lock.upstreamCommit, name) || !/^[a-f0-9]{64}$/.test(resource.sha256) || !Array.isArray(resource.dependencies) || !Array.isArray(resource.npmDependencies)) fail(`Invalid resource: ${name}`)
    reachable.add(name)
    for (const edge of resource.dependencies) visit(edge)
  }
  for (const name of lock.selected) visit(name)
  if (reachable.size !== Object.keys(lock.resources).length) fail('Unreachable locked resources')
  for (const [path, hash] of Object.entries(lock.generated)) {
    safePath(path.replace(/^src\/generated\//, ''))
    if (!path.startsWith('src/generated/') || !/^[a-f0-9]{64}$/.test(hash)) fail('Invalid generated path/hash')
  }
}
async function assertNoSymlink(root, path) {
  const absolute = resolve(root, path)
  if (!absolute.startsWith(resolve(root) + '/')) fail('Path escape')
  let current = root
  for (const segment of relative(root, absolute).split('/')) {
    current = resolve(current, segment)
    try { if ((await lstat(current)).isSymbolicLink()) fail(`Symlink path rejected: ${current}`) } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
}
async function outputs(root) {
  const base = resolve(root, 'src/generated')
  await assertNoSymlink(root, 'src/generated')
  const result = {}
  async function walk(path) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const full = resolve(path, entry.name)
      if (entry.isSymbolicLink()) fail('Generated symlink rejected')
      if (entry.isDirectory()) await walk(full)
      else if (entry.isFile()) result[relative(root, full)] = sha(await readFile(full))
      else fail('Unsupported generated file')
    }
  }
  try { await walk(base) } catch (error) { if (error.code !== 'ENOENT') throw error }
  return result
}
export async function verify(root = ROOT, lock) {
  lock ??= await json(root, 'ui-components.lock.json')
  await validateLock(root, lock)
  const actual = await outputs(root)
  if (!eq(Object.entries(actual).sort(), Object.entries(lock.generated).sort())) fail('Generated file set/hash mismatch; run ui:prepare')
}
export async function prepare(root = ROOT, { fetcher = fetch, lock } = {}) {
  lock ??= await json(root, 'ui-components.lock.json')
  await validateLock(root, lock)
  const pkg = await json(root, 'package.json')
  const generated = {}, payloads = {}
  // Validate every download and the complete graph before writing generated outputs.
  for (const [name, resource] of Object.entries(lock.resources)) {
    const bytes = await download(resource.url, fetcher)
    if (sha(bytes) !== resource.sha256) fail(`Input hash mismatch: ${name}`)
    const parsed = parseResource(bytes, name, pkg)
    if (!eq(parsed.edges, resource.dependencies) || !eq(parsed.npm, resource.npmDependencies)) fail(`Dependency graph mismatch: ${name}`)
    for (const edge of parsed.edges) if (!lock.resources[edge]) fail(`Unlocked dependency: ${edge}`)
    for (const file of parsed.files) {
      if (generated[file.path]) fail(`Duplicate generated target: ${file.path}`)
      generated[file.path] = file
    }
    payloads[name] = bytes
  }
  if (!eq(Object.entries(Object.fromEntries(Object.values(generated).map(f => [f.path, f.sha256]))).sort(), Object.entries(lock.generated).sort())) fail('Output hash mismatch')
  await assertNoSymlink(root, 'src/generated')
  await assertNoSymlink(root, '.ui-cache')
  for (const path of Object.keys(generated)) await assertNoSymlink(root, path)
  await rm(resolve(root, 'src/generated'), { recursive: true, force: true })
  for (const file of Object.values(generated)) {
    await mkdir(dirname(resolve(root, file.path)), { recursive: true })
    await writeFile(resolve(root, file.path), file.content)
  }
  await mkdir(resolve(root, '.ui-cache'), { recursive: true })
  for (const [name, bytes] of Object.entries(payloads)) {
    await assertNoSymlink(root, `.ui-cache/${name}.json`)
    await writeFile(resolve(root, `.ui-cache/${name}.json`), bytes)
  }
  await verify(root, lock)
}
export async function update(root = ROOT, args = process.argv.slice(3)) {
  const options = Object.fromEntries(args.map(arg => { const i = arg.indexOf('='); if (i < 3) fail('Use --ref=<ref> --components=button,dialog'); return [arg.slice(2, i), arg.slice(i + 1)] }))
  if (!options.ref || !options.components || Object.keys(options).some(k => !['ref', 'components'].includes(k))) fail('Explicit --ref and --components selection required')
  const selected = [...new Set(options.components.split(','))].sort()
  if (!selected.length || selected.some(n => !namePattern.test(n))) fail('Invalid component selection')
  const response = await download(`https://api.github.com/repos/shadcn-ui/ui/commits/${encodeURIComponent(options.ref)}`, fetch)
  const commit = JSON.parse(response).sha
  if (!/^[a-f0-9]{40}$/.test(commit)) fail('Invalid resolved upstream commit')
  const pkg = await json(root, 'package.json')
  const cliPackage = await json(root, 'node_modules/shadcn/package.json')
  if (cliPackage.version !== pkg.devDependencies.shadcn) fail('Pinned shadcn CLI installation mismatch; run npm ci')
  const cli = resolve(root, 'node_modules/shadcn/dist/index.js')
  const resources = {}, generated = {}
  const visit = async name => {
    if (resources[name]) return
    const url = sourceURL(commit, name)
    // Inspection is an explicit development operation; prepare/verify never invoke CLI.
    const inspected = spawnSync(process.execPath, [cli, 'view', url], { cwd: root, encoding: 'utf8', timeout: 60000 })
    if (inspected.status !== 0) fail(`Pinned CLI inspection failed for ${name}: ${inspected.stderr}`)
    const bytes = await download(url, fetch)
    const parsed = parseResource(bytes, name, pkg)
    resources[name] = { url, sha256: sha(bytes), dependencies: parsed.edges, npmDependencies: parsed.npm }
    for (const file of parsed.files) {
      if (generated[file.path]) fail(`Duplicate generated target: ${file.path}`)
      generated[file.path] = file.sha256
    }
    for (const dependency of parsed.edges) await visit(dependency)
  }
  for (const name of selected) await visit(name)
  const lock = { formatVersion: FORMAT, transformVersion: TRANSFORM, base: 'radix', style: 'new-york', upstreamCommit: commit, cliVersion: cliPackage.version, selected, configurationSHA256: await fingerprint(root), resources, generated }
  await validateLock(root, lock)
  await writeFile(resolve(root, 'ui-components.candidate.lock.json'), JSON.stringify(lock, null, 2) + '\n')
  console.log('Candidate created: ui-components.candidate.lock.json. Recreate and verify this exact candidate before accepting it as ui-components.lock.json.')
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const command = process.argv[2]
    if (command === 'prepare') await prepare()
    else if (command === 'verify') await verify()
    else if (command === 'update') await update()
    else fail('Expected update, prepare, or verify')
  } catch (error) { console.error(error.message); process.exitCode = 1 }
}
