// Thin client for the dashboard API. Mutations carry X-AFD-Oracle-UI, which the server requires (CSRF guard).

export type Check = { ok: boolean; detail: string; fix?: string | null; warnings?: string[] }
export type Health = { ok: boolean; root: string; checks: Record<string, Check> }

export type Job = {
  id: string
  kind: 'ingest' | 'mine' | 'setup' | 'discussions'
  repo: string | null
  status: 'queued' | 'running' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled'
  title: string
  args: Record<string, any> | null
  progress: Record<string, { done: number; total: number; at?: number }>
  summary: Record<string, any> | null
  error: string | null
  created: number
  started: number | null
  ended: number | null
  log?: string[]
}

export type RepoSummary = {
  id: string
  origin?: string | null
  head?: string | null
  ingested_at?: number | null
  commits: number
  alive_symbols?: number
  commits_behind?: number | null
  insights: Record<string, number>
  packets: Record<string, number>
  active_job?: Job | null
  last_job?: Job | null
  pending?: boolean
  error?: string
}

export type RepoDetail = RepoSummary & {
  packets_by_kind: Record<string, Record<string, Record<string, number>>>
  insights_by_label: Record<string, number>
  stats: Record<string, number>
  last_ingest: Job | null
  noise_filtered: Record<string, number> | null
  about?: About | null
}

export type About = {
  name?: string
  description?: string
  homepage?: string
  license?: string
  source?: string
  readme?: { path: string; title?: string | null; intro: string[] }
  languages: { name: string; files: number; share: number }[]
  folders: { name: string; files: number }[]
  files: number
}

export type Estimate = {
  open: Record<string, number>
  selected: Record<string, number>
  packets: number
  estimated_tokens: number
  tokens_per_packet: number
  agents_estimate: number
}

export type Insight = {
  uid: string
  label: 'Pattern' | 'AntiPattern' | 'RejectedAttempt'
  kind: string
  title: string
  summary: string
  severity: 'info' | 'low' | 'medium' | 'high'
  confidence: number
  status: 'proposed' | 'verified' | 'rejected'
  verifier_notes: string | null
  workflow_run: string | null
  created_by_model: string | null
  created_at: number
  updated_at: number
  citations: {
    commits?: { hash: string; role?: string }[]
    files?: { path: string; at_commit?: string }[]
    symbols?: (string | { uid: string })[]
  }
}

export type CommitDiff = {
  hash: string
  author: string
  ts: string
  authored?: string
  landed_on_main?: string | null
  landed_via?: string | null
  message: string
  files: { path: string; old_path?: string | null; status: string; added: number; deleted: number; patch: string; truncated: boolean }[]
}

export type SafetyFinding = {
  category: string; rule: string; severity: 'block' | 'warn' | 'info'; path: string; line: number
  symbol: string | null; message: string; detail: Record<string, any>
}

export type Safety = {
  findings: Record<string, Record<string, number>>
  risky_files: { path: string; score: number; factors: Record<string, number> }[]
  contracts: { keys: number; orphans: number }
  tests: { functions: number; functions_with_tests: number; links: number; coverage_links: number }
  api: { public_symbols: number; deprecated: number; tags: string[] }
  fixes: { commits: number; zones: Record<string, number> }
  discussions: { count: number; synced_at: string | null; github: string | null }
}

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', ...(method === 'GET' ? {} : { 'X-AFD-Oracle-UI': '1' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* not JSON */
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => call<Health>('GET', '/api/health'),
  setup: (what: 'infra' | 'mcp' | 'skill' | 'service') => call<any>('POST', `/api/setup/${what}`),
  repos: () => call<RepoSummary[]>('GET', '/api/repos'),
  addRepo: (source: string, repo_id: string | null, include_prs: boolean) =>
    call<Job>('POST', '/api/repos', { source, repo_id: repo_id || null, include_prs }),
  repo: (id: string) => call<RepoDetail>('GET', `/api/repos/${encodeURIComponent(id)}`),
  update: (id: string, full = false) => call<Job>('POST', `/api/repos/${encodeURIComponent(id)}/ingest`, { full }),
  remove: (id: string, removeMirror: boolean) =>
    call<any>('DELETE', `/api/repos/${encodeURIComponent(id)}?confirm=${encodeURIComponent(id)}&remove_mirror=${removeMirror}`),
  estimate: (id: string, max: number | null) =>
    call<Estimate>('GET', `/api/repos/${encodeURIComponent(id)}/estimate${max ? `?max=${max}` : ''}`),
  mine: (id: string, max: number | null) => call<Job>('POST', `/api/repos/${encodeURIComponent(id)}/mine`, { max }),
  run: (id: string, runId: string) => call<any>('GET', `/api/repos/${encodeURIComponent(id)}/runs/${runId}`),
  release: (jobId: string) => call<{ released: number }>('POST', `/api/jobs/${jobId}/release`),
  insights: (id: string) => call<Insight[]>('GET', `/api/repos/${encodeURIComponent(id)}/insights`),
  commit: (id: string, sha: string) => call<CommitDiff>('GET', `/api/repos/${encodeURIComponent(id)}/commits/${sha}`),
  reports: (id: string) => call<{ name: string; modified: number; size: number }[]>('GET', `/api/repos/${encodeURIComponent(id)}/reports`),
  report: (name: string) => call<{ name: string; markdown: string }>('GET', `/api/reports/${encodeURIComponent(name)}`),
  safety: (id: string) => call<Safety>('GET', `/api/repos/${encodeURIComponent(id)}/safety`),
  findings: (id: string, q: { category?: string; severity?: string; path?: string }) =>
    call<SafetyFinding[]>('GET', `/api/repos/${encodeURIComponent(id)}/findings?${new URLSearchParams(
      Object.entries(q).filter(([, v]) => v) as [string, string][])}`),
  assess: (id: string, paths: string[], symbols: string[]) =>
    call<any>('POST', `/api/repos/${encodeURIComponent(id)}/assess`, { paths, symbols }),
  syncDiscussions: (id: string) => call<Job>('POST', `/api/repos/${encodeURIComponent(id)}/discussions`),
  vulns: (id: string, severity?: string, scanner?: string) =>
    call<{ runs: ScanRun[]; counts: { scanner: string; severity: string; n: number }[]; findings: VulnFinding[] }>(
      'GET', `/api/repos/${encodeURIComponent(id)}/vulns?${new URLSearchParams(Object.entries({ severity, scanner }).filter(([, v]) => v) as [string, string][])}`),
  scan: (id: string, osv = false) => call<Job>('POST', `/api/repos/${encodeURIComponent(id)}/scan`, { osv }),
  jobs: (repo?: string) => call<Job[]>('GET', `/api/jobs${repo ? `?repo=${encodeURIComponent(repo)}` : ''}`),
  job: (id: string) => call<Job>('GET', `/api/jobs/${id}`),
  cancel: (id: string) => call<Job>('POST', `/api/jobs/${id}/cancel`),
  explore: {
    search: (id: string, q: string, scope: string, mode = 'hybrid', alive?: boolean) =>
      call<{ hits: ExploreHit[]; note?: string }>('GET', `${ex(id)}/search?${qs({ q, scope, mode, alive })}`),
    node: (id: string, uid: string) => call<ExploreNodeDetail>('GET', `${ex(id)}/node?${qs({ uid })}`),
    neighbors: (id: string, uid: string, type?: string, dir?: string, limit?: number) =>
      call<{ nodes: ExploreNode[]; edges: ExploreEdge[] }>('GET', `${ex(id)}/neighbors?${qs({ uid, type, dir, limit })}`),
    similar: (id: string, uid: string) => call<{ hits: ExploreHit[] }>('GET', `${ex(id)}/similar?${qs({ uid })}`),
    map: (id: string, collection: string, q?: string) => call<VectorMap>('GET', `${ex(id)}/map?${qs({ collection, q })}`),
  },
}

const ex = (id: string) => `/api/repos/${encodeURIComponent(id)}/explore`
const qs = (o: Record<string, unknown>) =>
  new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => [k, String(v)]))

export type ScanRun = { scanner: string; ran_at: number; commit_hash: string; status: string; findings: number; seconds: number; detail: string }
export type VulnFinding = {
  id: string; scanner: string; rule: string; severity: string; title: string; message: string; path: string; line: number | null
  package: string; version: string; fixed: string; cve: string; cwe: string; url: string; symbol: string | null
}
export type ExploreNode = {
  uid: string; group: string; kind: string; labels: string[]; title: string; subtitle: string
  alive?: unknown; props: Record<string, any>
}
export type ExploreNodeDetail = ExploreNode & {
  degrees: { type: string; dir: 'in' | 'out'; other: string; count: number }[]; has_vector: boolean
}
export type ExploreEdge = { source: string; target: string; type: string; props: Record<string, any> }
export type ExploreHit = {
  uid: string; group: string; title: string; subtitle: string; score?: number; snippet?: string
  payload?: Record<string, any>; kind?: string; labels?: string[]; props?: Record<string, any>
}
export type MapPoint = { uid: string; title: string; subtitle: string; kind: string; alive?: unknown; x: number; y: number; c: number }
export type Topic = { id: number; count: number; terms: string[]; folder: string; label: string; x: number; y: number; r: number }
export type VectorMap = {
  points: MapPoint[]; clusters: Topic[]; total: number; sampled: boolean; skipped?: number; note?: string
  collection: string; query?: { x: number; y: number; cluster: number }; hits?: string[]
}

export const isActive = (j?: Job | null) => !!j && ['queued', 'running', 'cancelling'].includes(j.status)

export function ago(ts?: number | null): string {
  if (!ts) return 'never'
  const s = Date.now() / 1000 - ts
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

export const fmt = (n?: number | null) => (n ?? 0).toLocaleString()
export const tokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`)
export const githubCommitUrl = (origin: string | null | undefined, sha: string) => {
  const m = origin?.match(/github\.com[/:]([^/]+\/[^/.]+?)(\.git)?\/?$/)
  return m ? `https://github.com/${m[1]}/commit/${sha}` : null
}
