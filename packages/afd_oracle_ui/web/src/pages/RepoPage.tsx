import { useState } from 'react'
import { Link, NavLink, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import { ago, api, fmt, isActive, tokens, type Job, type RepoDetail } from '../api'
import JobProgress from '../components/JobProgress'
import InsightsTab from '../components/InsightsTab'
import RiskTab from '../components/RiskTab'
import ExploreTab from '../components/explore/ExploreTab'
import { JobTable } from './Activity'
import { shortOrigin } from './Home'

export default function RepoPage() {
  const { id = '' } = useParams()
  const { data: repo, error } = useQuery({
    queryKey: ['repo', id],
    queryFn: () => api.repo(id),
    refetchInterval: (q) => (isActive(q.state.data?.active_job) ? 3000 : 30000),
    retry: (n, e) => n < 20 && /unknown repository/.test((e as Error).message), // first ingest registers shortly
  })
  if (error && !repo) {
    return <PendingRepo id={id} />
  }
  if (!repo) return <p className="muted">Loading…</p>
  const base = `/repos/${encodeURIComponent(id)}`
  return (
    <>
      <header className="stack" style={{ gap: 8 }}>
        <nav className="crumbs" aria-label="Breadcrumb"><Link to="/">Repositories</Link><span>/</span><span>{repo.id}</span></nav>
        <div className="row" style={{ gap: 14 }}>
          <span className="monogram" aria-hidden="true" style={{ width: 44, height: 44, fontSize: '1.35rem' }}>{repo.id.charAt(0).toUpperCase()}</span>
          <div className="stack" style={{ gap: 4 }}>
            <div className="row">
              <h1 className="mono">{repo.id}</h1>
              {isActive(repo.active_job) && <span className="pill info">{repo.active_job!.kind === 'mine' ? 'Mining' : 'Ingesting'}</span>}
            </div>
            <div className="meta">
              <span title={repo.origin || undefined}>{shortOrigin(repo.origin)}</span>
              <span><b>{fmt(repo.commits)}</b> commits</span>
              {repo.ingested_at && <span>Updated {ago(repo.ingested_at)}</span>}
              {repo.head && <span>Head <span className="mono">{repo.head.slice(0, 10)}</span></span>}
            </div>
          </div>
        </div>
      </header>
      <nav className="tabs" aria-label="Repository sections">
        <NavLink to={base} end>Overview</NavLink>
        <NavLink to={`${base}/risk`}>Risk</NavLink>
        <NavLink to={`${base}/mine`}>Mine{repo.packets?.open ? <span className="badge">{fmt(repo.packets.open)}</span> : null}</NavLink>
        <NavLink to={`${base}/insights`}>Insights{repo.insights?.verified ? <span className="badge">{repo.insights.verified}</span> : null}</NavLink>
        <NavLink to={`${base}/explore`}>Explore</NavLink>
        <NavLink to={`${base}/reports`}>Reports</NavLink>
        <NavLink to={`${base}/activity`}>Activity</NavLink>
      </nav>
      <Routes>
        <Route index element={<Overview repo={repo} />} />
        <Route path="risk" element={<RiskTab repo={repo} />} />
        <Route path="mine" element={<MineTab repo={repo} />} />
        <Route path="insights" element={<InsightsTab repo={repo} />} />
        <Route path="explore" element={<ExploreTab repo={repo} />} />
        <Route path="reports" element={<ReportsTab id={repo.id} />} />
        <Route path="activity" element={<JobTable repo={repo.id} />} />
      </Routes>
    </>
  )
}

function PendingRepo({ id }: { id: string }) {
  const { data: jobs } = useQuery({ queryKey: ['jobs', id], queryFn: () => api.jobs(id), refetchInterval: 3000 })
  const job = jobs?.[0]
  return (
    <div className="panel">
      <h1 className="mono">{id}</h1>
      {job ? <JobProgress job={job} /> : <p className="muted">Waiting for the ingest to start…</p>}
    </div>
  )
}

const LANG_COLORS = ['var(--accent)', '#8b6fd6', '#d08a3c', '#3f9d8f', '#c45b7a', 'var(--faint)']

/** What the repository is for, from its own manifest and README at HEAD. */
function AboutPanel({ about }: { about: NonNullable<RepoDetail['about']> }) {
  const desc = about.description?.trim()
  const intro = (about.readme?.intro || []).filter(
    (p) => !desc || p.replace(/\W/g, '').toLowerCase() !== desc.replace(/\W/g, '').toLowerCase())
  const href = about.homepage && /^https?:\/\//.test(about.homepage) ? about.homepage : null
  if (!desc && !intro.length && !about.languages.length) return null
  return (
    <div className="panel about">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: 8 }}>
        <h2 style={{ margin: 0 }}>About {about.readme?.title || about.name || 'this repository'}</h2>
        <span className="small muted">
          {about.license && <>Licence {about.license} · </>}
          {fmt(about.files)} files
          {href && <> · <a href={href} target="_blank" rel="noreferrer">{href.replace(/^https?:\/\//, '').replace(/\/$/, '')}</a></>}
        </span>
      </div>
      {desc && <p className="about-lede">{desc}</p>}
      {intro.map((p, i) => <p key={i} className="muted" style={{ margin: '6px 0' }}>{p}</p>)}
      {about.languages.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="langbar" role="img" aria-label={about.languages.map((l) => `${l.name} ${Math.round(l.share * 100)}%`).join(', ')}>
            {about.languages.map((l, i) => (
              <span key={l.name} style={{ flexGrow: l.share, background: LANG_COLORS[i % LANG_COLORS.length] }} title={`${l.name}: ${l.files} files`} />
            ))}
          </div>
          <div className="row small" style={{ gap: 14, flexWrap: 'wrap', marginTop: 6 }}>
            {about.languages.map((l, i) => (
              <span key={l.name}><i className="dot" style={{ background: LANG_COLORS[i % LANG_COLORS.length] }} />{l.name} {Math.round(l.share * 100)}%</span>
            ))}
          </div>
        </div>
      )}
      {about.folders.length > 0 && (
        <div className="row small" style={{ gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
          <span className="muted">Top-level:</span>
          {about.folders.map((f) => <span key={f.name} className="pill mono" title={`${f.files} files`}>{f.name}/</span>)}
        </div>
      )}
      <p className="small muted" style={{ marginTop: 10, marginBottom: 0 }}>
        From {[about.source, about.readme?.path].filter(Boolean).join(' and ') || 'the repository files'} at the ingested commit.
      </p>
    </div>
  )
}

function Overview({ repo }: { repo: RepoDetail }) {
  const qc = useQueryClient()
  const nav = useNavigate()
  const [err, setErr] = useState<string | null>(null)
  const [confirm, setConfirm] = useState('')
  const [showDelete, setShowDelete] = useState(false)
  const active = repo.active_job
  const pool = Object.entries(repo.packets_by_kind || {}).flatMap(([kind, subs]) =>
    Object.entries(subs).map(([sub, st]) => ({ kind, sub, open: st.open || 0, done: (st.done || 0) + (st.claimed || 0) })))
  const noise = Object.entries(repo.noise_filtered || {}).sort((a, b) => b[1] - a[1])

  async function update(full = false) {
    setErr(null)
    try {
      await api.update(repo.id, full)
      qc.invalidateQueries()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  return (
    <div className="stack">
      {active && <div className="panel"><JobProgress job={active} /></div>}
      {repo.about && <AboutPanel about={repo.about} />}
      <div className="stats">
        <div className="stat"><b>{fmt(repo.commits)}</b><span>commits</span></div>
        <div className="stat"><b>{fmt(repo.alive_symbols)}</b><span>live symbols</span></div>
        <div className="stat"><b>{fmt(repo.stats?.reverts)}</b><span>reverts</span></div>
        <div className="stat"><b>{fmt(repo.stats?.branches)}</b><span>branches</span></div>
        <div className="stat"><b>{fmt(repo.stats?.cochange_pairs)}</b><span>co-change pairs</span></div>
        <div className="stat"><b>{fmt(repo.insights?.verified)}</b><span>verified insights</span></div>
      </div>
      <div className="panel">
        <div className="row">
          <h2>Keep it current</h2>
          <span className="spacer" />
          <button className="btn" disabled={!!active} onClick={() => update(false)}>Update</button>
          <button className="btn" disabled={!!active} onClick={() => update(true)}>Rebuild from scratch</button>
        </div>
        <p className="muted small">
          Update reads only commits added since the last ingest (fast). Rebuild re-reads the whole history.
          Neither uses AI tokens.
        </p>
        {err && <div className="error">{err}</div>}
      </div>
      <div className="panel">
        <div className="panel-head">
        <h2>Mining candidates</h2>
        <p className="muted small">
          Built automatically from the history. Candidates a program can recognise as noise were dropped before any AI
          sees them.
        </p>
        </div>
        <div className="tablewrap">
          <table>
            <thead><tr><th>Kind</th><th>Type</th><th className="num">To mine</th><th className="num">Mined</th><th>Progress</th></tr></thead>
            <tbody>
              {pool.map((p) => {
                const pct = p.open + p.done ? Math.round((100 * p.done) / (p.open + p.done)) : 0
                return (
                  <tr key={p.kind + p.sub}><td className="kind">{p.kind}</td><td className="muted">{p.sub.replace(/_/g, ' ')}</td>
                    <td className="num">{fmt(p.open)}</td><td className="num">{fmt(p.done)}</td>
                    <td><div className="meter"><div className="bar"><i style={{ width: `${pct}%` }} /></div><span className="small faint mono">{pct}%</span></div></td></tr>
                )
              })}
            </tbody>
          </table>
        </div>
        {noise.length > 0 && (
          <details>
            <summary className="small muted">Dropped as noise: {fmt(noise.reduce((a, [, n]) => a + n, 0))}</summary>
            <ul className="small">{noise.map(([k, n]) => <li key={k}><code>{k}</code> {fmt(n)}</li>)}</ul>
          </details>
        )}
        <div><button className="btn primary" onClick={() => nav(`/repos/${encodeURIComponent(repo.id)}/mine`)}>Go to mining</button></div>
      </div>
      <div className="panel" style={{ borderColor: 'color-mix(in srgb, var(--bad) 22%, var(--line))' }}>
        <div className="panel-head">
          <h2>Danger zone</h2>
          <p className="muted small">Removing a repository deletes everything Oracle has learned about it.</p>
        </div>
        {!showDelete ? (
          <div><button className="btn danger" onClick={() => setShowDelete(true)}>Remove from the knowledge base…</button></div>
        ) : (
          <div className="stack">
            <p className="small">This deletes the graph, vectors, history store and insights for <code>{repo.id}</code>. Type its name to confirm.</p>
            <div className="row">
              <input id="confirm-delete" className="input mono" style={{ maxWidth: 260 }} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              <button className="btn danger" disabled={confirm !== repo.id || !!active}
                onClick={async () => {
                  try { await api.remove(repo.id, true); qc.invalidateQueries(); nav('/') } catch (e) { setErr((e as Error).message) }
                }}>Remove permanently</button>
              <button className="btn" onClick={() => setShowDelete(false)}>Keep it</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function MineTab({ repo }: { repo: RepoDetail }) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'all' | 'cap'>('cap')
  const [cap, setCap] = useState(30)
  const [err, setErr] = useState<string | null>(null)
  const [started, setStarted] = useState<Job | null>(null)
  const max = mode === 'all' ? null : Math.max(1, cap)
  const { data: est } = useQuery({ queryKey: ['estimate', repo.id, max], queryFn: () => api.estimate(repo.id, max) })
  const { data: jobs } = useQuery({ queryKey: ['jobs', repo.id], queryFn: () => api.jobs(repo.id) })
  const lastMine = started || jobs?.find((j) => j.kind === 'mine') || null
  const busy = isActive(repo.active_job)

  async function start() {
    setErr(null)
    try {
      const j = await api.mine(repo.id, max)
      setStarted(j)
      qc.invalidateQueries()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  const openTotal = est ? Object.values(est.open).reduce((a, b) => a + b, 0) : 0
  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-head">
        <h2>Mine for insights</h2>
        <p className="muted small">
          Claude reviews each candidate: an analyst records findings with exact citations, and an independent reviewer
          tries to refute each one. Only survivors count as verified. This runs Claude Code in the background on your
          subscription.
        </p>
        </div>
        <div className="row">
          {Object.entries(est?.open || {}).map(([k, n]) => <span key={k} className="tag">{k}: {fmt(n)} open</span>)}
        </div>
        <div className="row">
          <label className="check"><input type="radio" name="mode" checked={mode === 'cap'} onChange={() => setMode('cap')} />
            Mine the top
            <input id="mine-cap" className="input" type="number" min={1} max={openTotal || 1} style={{ width: 90 }}
              value={cap} onChange={(e) => { setCap(Number(e.target.value)); setMode('cap') }} />
            candidates</label>
          <label className="check"><input type="radio" name="mode" checked={mode === 'all'} onChange={() => setMode('all')} />
            Mine everything ({fmt(openTotal)})</label>
        </div>
        {est && (
          <div className="stats">
            <div className="stat"><b>{fmt(est.packets)}</b><span>candidates selected</span></div>
            <div className="stat"><b>~{tokens(est.estimated_tokens)}</b><span>estimated tokens</span></div>
            <div className="stat"><b>~{fmt(est.agents_estimate)}</b><span>Claude agents</span></div>
          </div>
        )}
        {est && est.estimated_tokens > 2_000_000 && (
          <div className="notice">That is a large run. It counts against your Claude plan's usage limits; consider a smaller cap first.</div>
        )}
        {err && <div className="error">{err}</div>}
        <div className="row">
          <button className="btn primary" disabled={busy || !est?.packets} onClick={start}>
            {busy ? 'A job is running…' : `Start mining ${est ? fmt(est.packets) : ''} candidates`}
          </button>
        </div>
      </div>
      {lastMine && (
        <div className="panel">
          <JobProgress job={lastMine} onDone={() => qc.invalidateQueries()} />
          {!isActive(lastMine) && lastMine.status !== 'succeeded' && (
            <div className="row">
              <button className="btn sm" onClick={() => api.release(lastMine.id).then(() => qc.invalidateQueries())}>
                Release unfinished candidates
              </button>
              <span className="small muted">Puts this run's candidates back in the queue.</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ReportsTab({ id }: { id: string }) {
  const { data: list } = useQuery({ queryKey: ['reports', id], queryFn: () => api.reports(id) })
  const [name, setName] = useState<string | null>(null)
  const current = name || list?.[0]?.name || null
  const { data: report } = useQuery({ queryKey: ['report', current], queryFn: () => api.report(current!), enabled: !!current })
  if (!list?.length) return <p className="empty">No reports yet. A full mining run writes one.</p>
  return (
    <div className="stack">
      <div className="row">
        <select id="report-pick" className="select" value={current || ''} onChange={(e) => setName(e.target.value)}>
          {list.map((r) => <option key={r.name} value={r.name}>{r.name} ({ago(r.modified)})</option>)}
        </select>
      </div>
      <article className="panel markdown"><ReactMarkdown>{report?.markdown || ''}</ReactMarkdown></article>
    </div>
  )
}
