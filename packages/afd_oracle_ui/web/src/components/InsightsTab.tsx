import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, githubCommitUrl, type Insight, type RepoDetail } from '../api'

const SEV = { high: 0, medium: 1, low: 2, info: 3 } as const

export default function InsightsTab({ repo }: { repo: RepoDetail }) {
  const { data, isLoading } = useQuery({ queryKey: ['insights', repo.id], queryFn: () => api.insights(repo.id) })
  const [status, setStatus] = useState('verified')
  const [label, setLabel] = useState('')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<Insight | null>(null)

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (data || [])
      .filter((i) => (!status || i.status === status) && (!label || i.label === label))
      .filter((i) => !needle || JSON.stringify(i).toLowerCase().includes(needle))
      .sort((a, b) => SEV[a.severity] - SEV[b.severity] || a.title.localeCompare(b.title))
  }, [data, status, label, q])

  if (isLoading) return <p className="muted">Loading…</p>
  return (
    <div className="stack">
      <div className="filters">
        <select id="f-status" className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="verified">Verified</option><option value="proposed">Awaiting review</option>
          <option value="rejected">Rejected by reviewer</option><option value="">All</option>
        </select>
        <select id="f-label" className="select" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Type">
          <option value="">All types</option><option value="RejectedAttempt">Rejected attempts</option>
          <option value="AntiPattern">Anti-patterns</option><option value="Pattern">Patterns</option>
        </select>
        <input id="f-q" className="input" style={{ flex: '1 1 220px', width: 'auto' }} placeholder="Search titles, files, commits…"
          value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
        <span className="small muted" style={{ padding: '0 6px' }}>{list.length} shown</span>
      </div>
      {!list.length ? (
        <p className="empty">No insights match. {data?.length ? '' : 'Mine this repository to produce some.'}</p>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {list.map((i) => (
            <button key={i.uid} className={`insight sev-${i.severity}`} onClick={() => setOpen(i)}>
              <div className="row" style={{ gap: 6 }}>
                <span className="tag sev">{i.severity}</span><span className="tag">{i.label}</span><span className="tag">{i.kind.replace(/_/g, ' ')}</span>
                <span className="spacer" /><span className="small faint mono">{i.uid.slice(8, 16)}</span>
              </div>
              <strong>{i.title}</strong>
              <span className="small muted">{i.summary.slice(0, 220)}{i.summary.length > 220 ? '…' : ''}</span>
            </button>
          ))}
        </div>
      )}
      {open && <InsightDrawer insight={open} repo={repo} onClose={() => setOpen(null)} />}
    </div>
  )
}

function InsightDrawer({ insight: i, repo, onClose }: { insight: Insight; repo: RepoDetail; onClose: () => void }) {
  const [sha, setSha] = useState<string | null>(null)
  const files = Array.from(new Set((i.citations.files || []).map((f) => f.path)))
  const syms = (i.citations.symbols || []).map((s) => (typeof s === 'string' ? s : s.uid))
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" aria-label="Insight details">
        <div className="row"><span className="eyebrow">Insight</span><span className="spacer" /><button className="btn sm" onClick={onClose}>Close</button></div>
        <div className={`row sev-${i.severity}`} style={{ gap: 6 }}>
          <span className="tag sev">{i.severity}</span><span className="tag">{i.label}</span><span className="tag">{i.kind.replace(/_/g, ' ')}</span>
          <span className={`pill ${i.status === 'verified' ? 'ok' : i.status === 'rejected' ? 'bad' : 'warn'}`}>{i.status}</span>
        </div>
        <h2 style={{ fontSize: '1.35rem' }}>{i.title}</h2>
        <p style={{ lineHeight: 1.65 }}>{i.summary}</p>
        <section className="stack" style={{ gap: 6 }}>
          <h3>Commits</h3>
          <div className="row">
            {(i.citations.commits || []).map((c) => {
              const gh = githubCommitUrl(repo.origin, c.hash)
              return (
                <span key={c.hash} className="row" style={{ gap: 4 }}>
                  <button className="btn sm mono" onClick={() => setSha(c.hash)}>{c.hash.slice(0, 8)}</button>
                  <span className="small muted">{c.role}</span>
                  {gh && <a className="small" href={gh} target="_blank" rel="noreferrer">GitHub</a>}
                </span>
              )
            })}
          </div>
        </section>
        <section className="stack" style={{ gap: 6 }}>
          <h3>Files</h3>
          <div className="row">{files.map((f) => <code key={f}>{f}</code>)}</div>
          {syms.length > 0 && (<><h3>Symbols</h3><div className="row">{syms.map((s) => <code key={s} title={s}>{s.split(':')[3] || s}</code>)}</div></>)}
        </section>
        {i.verifier_notes && (
          <section className="stack" style={{ gap: 6 }}>
            <h3>Reviewer's notes</h3>
            <p className="small muted">{i.verifier_notes}</p>
          </section>
        )}
        <p className="small muted mono">{i.uid}{i.workflow_run ? ` · run ${i.workflow_run}` : ''}</p>
        {sha && <CommitView repo={repo.id} sha={sha} />}
      </aside>
    </>
  )
}

export function CommitView({ repo, sha }: { repo: string; sha: string }) {
  const { data, error, isLoading } = useQuery({ queryKey: ['commit', repo, sha], queryFn: () => api.commit(repo, sha) })
  if (isLoading) return <p className="muted">Loading commit…</p>
  if (error || !data) return <div className="error">{(error as Error)?.message || 'Commit not found'}</div>
  return (
    <section className="stack" style={{ gap: 8 }}>
      <h3 className="mono">{data.hash.slice(0, 12)}</h3>
      <div className="small muted">
        {data.author} · authored {data.authored?.slice(0, 10) || data.ts.slice(0, 10)}
        {data.landed_on_main ? ` · landed on main ${data.landed_on_main.slice(0, 10)}` : ' · never reached main'}
      </div>
      <pre className="log" style={{ maxHeight: 160 }}>{data.message}</pre>
      {data.files.map((f) => (
        <div key={f.path} className="stack" style={{ gap: 4 }}>
          <div className="small"><code>{f.status}</code> {f.path} <span className="muted">+{f.added} −{f.deleted}</span></div>
          <pre className="diff">
            {f.patch.split('\n').map((l, n) => (
              <span key={n} className={l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : l.startsWith('@@') ? 'hunk' : ''}>{l || ' '}</span>
            ))}
          </pre>
        </div>
      ))}
    </section>
  )
}
