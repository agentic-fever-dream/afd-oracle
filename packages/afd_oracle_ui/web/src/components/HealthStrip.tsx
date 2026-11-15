import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Job } from '../api'
import JobProgress from './JobProgress'

const LABELS: Record<string, string> = {
  docker: 'Docker', neo4j: 'Graph (Neo4j)', qdrant: 'Vectors (Qdrant)', claude: 'Claude Code',
  mcp: 'MCP server', skill: 'Claude skill', commands: '/afd oracle', service: 'Runs at login',
}
const FIX_LABEL: Record<string, string> = {
  infra: 'Start services', mcp: 'Register for Claude', skill: 'Install skill + commands', service: 'Run at login',
}

/** Readiness at a glance: one quiet line when all is well, full cards with one-click fixes when something isn't. */
export default function HealthStrip() {
  const qc = useQueryClient()
  const { data, isLoading, error } = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30000 })
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [setupJob, setSetupJob] = useState<Job | null>(null)
  const [expanded, setExpanded] = useState(false)

  async function fix(what: 'infra' | 'mcp' | 'skill' | 'service') {
    setBusy(what)
    setMsg(null)
    try {
      const out = await api.setup(what)
      if (what === 'infra') setSetupJob(out as Job)
      else setMsg(what === 'mcp' ? 'Registered. Restart open Claude Code sessions to pick it up.'
        : what === 'service' ? 'Installed. The dashboard now starts when you log in and restarts itself if it stops.'
        : 'Skill and /afd oracle commands installed. New Claude Code sessions will use them.')
      qc.invalidateQueries({ queryKey: ['health'] })
    } catch (e) {
      setMsg((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (isLoading) return <div className="health-summary muted">Checking services…</div>
  if (error || !data) return <div className="error">Could not reach the dashboard API.</div>
  const entries = Object.entries(data.checks)
  const failing = entries.filter(([, c]) => !c.ok)
  const warnings = entries.flatMap(([, c]) => c.warnings || [])
  const showCards = expanded || failing.length > 0
  return (
    <section className="stack" aria-label="System health" style={{ gap: 10 }}>
      <div className="health-summary">
        <span className={`dot ${failing.length ? '' : 'ok'}`} />
        <strong>{failing.length ? `${failing.length} of ${entries.length} checks need attention` : 'All systems ready'}</strong>
        <div className="chips">
          {entries.map(([key, c]) => (
            <span key={key} className="chip" title={c.detail}>
              <span className={`dot ${c.ok ? 'ok' : ''}`} style={{ width: 6, height: 6, boxShadow: 'none' }} />
              {LABELS[key] || key}
            </span>
          ))}
        </div>
        <span className="spacer" />
        {!failing.length && (
          <button className="btn sm" onClick={() => setExpanded((x) => !x)} aria-expanded={expanded}>
            {expanded ? 'Hide details' : 'Details'}
          </button>
        )}
      </div>
      {showCards && (
        <div className="health">
          {(expanded ? entries : failing).map(([key, c]) => (
            <div key={key} className={`health-item ${c.ok ? '' : 'bad'}`}>
              <div className="name"><span className={`dot ${c.ok ? 'ok' : ''}`} />{LABELS[key] || key}</div>
              <div className="detail" title={c.detail}>{c.detail}</div>
              {!c.ok && c.fix && (
                <div>
                  <button className="btn sm primary" disabled={!!busy} onClick={() => fix(c.fix as any)}>
                    {busy === c.fix ? 'Working…' : FIX_LABEL[c.fix] || 'Fix'}
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {warnings.map((w) => <div key={w} className="notice">{w}</div>)}
      {msg && <div className="notice">{msg}</div>}
      {setupJob && (
        <div className="panel">
          <JobProgress job={setupJob} onDone={() => qc.invalidateQueries({ queryKey: ['health'] })} />
        </div>
      )}
    </section>
  )
}
