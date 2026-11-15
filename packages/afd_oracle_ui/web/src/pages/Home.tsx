import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ago, api, fmt, isActive, type RepoSummary } from '../api'
import HealthStrip from '../components/HealthStrip'
import AddRepoDialog from '../components/AddRepoDialog'

/** "https://github.com/pallets/flask.git" -> "github.com/pallets/flask"; local paths keep their last two parts. */
export function shortOrigin(origin?: string | null) {
  if (!origin) return 'Local repository'
  const url = origin.replace(/^[a-z+]+:\/\//, '').replace(/^git@([^:]+):/, '$1/').replace(/\.git$/, '')
  return url.includes('/') && !/^[A-Za-z]:[\\/]/.test(origin) ? url : origin.split(/[\\/]/).slice(-2).join('/')
}

export default function Home() {
  const [adding, setAdding] = useState(false)
  const { data: repos, isLoading } = useQuery({
    queryKey: ['repos'],
    queryFn: api.repos,
    refetchInterval: (q) => (q.state.data?.some((r) => isActive(r.active_job)) ? 3000 : 20000),
  })

  return (
    <>
      <header className="page-head">
        <div className="stack" style={{ gap: 6 }}>
          <span className="eyebrow">Knowledge base</span>
          <h1>Repositories</h1>
          <p className="lede">
            Everything Claude knows about your codebases' history. Repositories added here are available to Claude
            Code in every session through the Oracle MCP server.
          </p>
        </div>
        <span className="spacer" />
        <button className="btn primary" onClick={() => setAdding(true)}>
          <span aria-hidden="true">+</span> Add repository
        </button>
      </header>
      <HealthStrip />
      {isLoading ? (
        <p className="muted">Loading…</p>
      ) : (
        <div className="grid-cards">
          {repos?.map((r) => <RepoCard key={r.id} r={r} />)}
          <button className="add-card" onClick={() => setAdding(true)}>
            <span className="plus" aria-hidden="true">+</span>
            <strong>{repos?.length ? 'Add another repository' : 'Add your first repository'}</strong>
            <span className="small">A git URL or a folder on this computer</span>
          </button>
        </div>
      )}
      {adding && <AddRepoDialog onClose={() => setAdding(false)} />}
    </>
  )
}

function RepoCard({ r }: { r: RepoSummary }) {
  const ingesting = isActive(r.active_job) && r.active_job!.kind === 'ingest'
  return (
    <Link to={`/repos/${encodeURIComponent(r.id)}`} className="panel repo-card">
      <div className="row" style={{ flexWrap: 'nowrap' }}>
        <div className="repo-title">
          <span className="monogram" aria-hidden="true">{r.id.charAt(0).toUpperCase()}</span>
          <div style={{ minWidth: 0 }}>
            <h2>{r.id}</h2>
            <div className="origin" title={r.origin || undefined}>{shortOrigin(r.origin)}</div>
          </div>
        </div>
        <span className="spacer" />
        {isActive(r.active_job) ? (
          <span className="pill info">{r.active_job!.kind === 'mine' ? 'Mining' : 'Ingesting'}</span>
        ) : r.last_job?.status === 'failed' ? (
          <span className="pill bad">Last job failed</span>
        ) : (
          <span className="pill ok">Ready</span>
        )}
      </div>
      <div className="nums">
        <div className="num" title="Commits read into the knowledge base">
          <b>{fmt(r.commits)}</b><span>ingested</span></div>
        <div className="num" title="Commits on the local default branch not read by the last ingest">
          <b>{ingesting ? '…' : r.commits_behind == null ? '–' : fmt(r.commits_behind)}</b>
          <span>{ingesting ? 'ingesting' : 'to ingest'}</span></div>
        <div className="num" title="Mining candidates Claude has reviewed">
          <b>{fmt((r.packets?.done || 0) + (r.packets?.claimed || 0))}</b><span>mined</span></div>
        <div className="num" title="Mining candidates not yet reviewed by Claude">
          <b>{fmt(r.packets?.open || 0)}</b><span>to mine</span></div>
        <div className="num" title={`${fmt(r.insights?.proposed || 0)} awaiting verification, ${fmt(r.insights?.rejected || 0)} rejected`}>
          <b>{fmt(r.insights?.verified || 0)}</b><span>insights</span></div>
      </div>
      <div className="foot">
        <span>{r.ingested_at ? `Updated ${ago(r.ingested_at)}` : r.head ? <>Head <span className="mono">{r.head.slice(0, 8)}</span></> : ''}</span>
        <span className="go">Open →</span>
      </div>
    </Link>
  )
}
