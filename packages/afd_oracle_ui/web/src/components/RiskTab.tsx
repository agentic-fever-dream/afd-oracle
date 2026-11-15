import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, fmt, isActive, type Job, type RepoDetail } from '../api'
import JobProgress from './JobProgress'
import VulnPanel from './VulnPanel'

const CATEGORIES: [string, string][] = [
  ['security', 'Security'], ['race', 'Races & shared state'], ['logic_bomb', 'Suspicious code'],
  ['contract', 'String contracts'], ['error_handling', 'Error handling'], ['performance', 'Performance'],
]

/** Change-safety overview: baseline hazards, riskiest files, test coverage of the code, and a pre-change check. */
export default function RiskTab({ repo }: { repo: RepoDetail }) {
  const qc = useQueryClient()
  const { data: s } = useQuery({ queryKey: ['safety', repo.id], queryFn: () => api.safety(repo.id) })
  const [category, setCategory] = useState('')
  const [severity, setSeverity] = useState('warn')
  const { data: findings } = useQuery({
    queryKey: ['findings', repo.id, category, severity],
    queryFn: () => api.findings(repo.id, { category, severity }),
  })
  const [target, setTarget] = useState('')
  const [assess, setAssess] = useState<any | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)

  if (!s) return <p className="muted">Loading…</p>
  const count = (cat: string, sev: string) => s.findings[cat]?.[sev] ?? 0
  const testPct = s.tests.functions ? Math.round((100 * s.tests.functions_with_tests) / s.tests.functions) : 0

  async function runAssess(e: React.FormEvent) {
    e.preventDefault()
    setErr(null)
    const items = target.split(/[\s,]+/).filter(Boolean)
    const paths = items.filter((x) => !x.includes('::') && /\.\w+$/.test(x))
    const symbols = items.filter((x) => !paths.includes(x))
    try {
      setAssess(await api.assess(repo.id, paths, symbols))
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-head">
        <h2>Change safety</h2>
        <p className="muted small">
          Deterministic checks rebuilt on every update. In Claude Code, Oracle runs <code>assess_change</code> before an
          edit and <code>check_diff</code> after it, so new hazards, breaking changes and missed partners are caught as
          they are written.
        </p>
        </div>
        <div className="stats">
          {CATEGORIES.map(([k, label]) => (
            <div key={k} className="stat">
              <b>{fmt(count(k, 'block') + count(k, 'warn'))}</b>
              <span>{label}{count(k, 'block') ? ` · ${count(k, 'block')} blocking` : ''}</span>
            </div>
          ))}
        </div>
        <div className="stats">
          <div className="stat"><b>{testPct}%</b><span>functions reached by a known test</span></div>
          <div className="stat"><b>{fmt(s.api.public_symbols)}</b><span>public API symbols ({s.api.deprecated} deprecated)</span></div>
          <div className="stat"><b>{fmt(s.contracts.keys)}</b><span>string contracts ({s.contracts.orphans} orphaned)</span></div>
          <div className="stat"><b>{fmt(s.fixes.commits)}</b><span>fix commits traced ({Object.entries(s.fixes.zones).filter(([k]) => k !== 'bug').map(([k, n]) => `${n} ${k}`).join(', ') || 'no zones'})</span></div>
        </div>
      </div>

      <form className="panel" onSubmit={runAssess}>
        <h2>Check before you change</h2>
        <p className="muted small">File paths or symbols (e.g. <code>src/app.py</code> or <code>src/app.py::Flask.run</code>), separated by spaces.</p>
        <div className="row">
          <input id="assess-target" className="input" style={{ flex: '1 1 320px', width: 'auto' }} value={target}
            onChange={(e) => setTarget(e.target.value)} placeholder="src/flask/app.py" />
          <button className="btn primary" disabled={!target.trim()}>Assess</button>
        </div>
        {err && <div className="error">{err}</div>}
        {assess && <AssessView a={assess} />}
      </form>

      <div className="panel">
        <h2>Riskiest files</h2>
        <div className="tablewrap"><table>
          <thead><tr><th>File</th><th className="num">Risk</th><th className="num">Churn</th><th className="num">Fixes</th><th className="num">Hazards</th><th>Tests</th></tr></thead>
          <tbody>
            {s.risky_files.map((f) => (
              <tr key={f.path}>
                <td className="mono small">{f.path}</td>
                <td className="num"><span className={`pill ${f.score >= 70 ? 'bad' : f.score >= 40 ? 'warn' : 'ok'}`}>{f.score}</span></td>
                <td className="num">{fmt(f.factors.churn)}</td>
                <td className="num">{fmt(f.factors.fragility)}</td>
                <td className="num">{fmt(f.factors.hazards)}</td>
                <td>{f.factors.test_gap ? <span className="pill warn">none known</span> : <span className="pill ok">yes</span>}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </div>

      <div className="panel">
        <div className="row">
          <h2>Findings at HEAD</h2>
          <span className="spacer" />
          <select id="f-cat" className="select" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
            <option value="">All categories</option>
            {CATEGORIES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
          <select id="f-sev" className="select" value={severity} onChange={(e) => setSeverity(e.target.value)} aria-label="Severity">
            <option value="block">Blocking</option><option value="warn">Warnings</option><option value="info">Info</option><option value="">All</option>
          </select>
        </div>
        {!findings?.length ? <p className="empty">Nothing at this level.</p> : (
          <div className="tablewrap"><table>
            <thead><tr><th>Severity</th><th>Where</th><th>What</th></tr></thead>
            <tbody>
              {findings.slice(0, 200).map((f, i) => (
                <tr key={i}>
                  <td><span className={`pill ${f.severity === 'block' ? 'bad' : f.severity === 'warn' ? 'warn' : 'info'}`}>{f.severity}</span></td>
                  <td className="mono small">{f.path}{f.line ? `:${f.line}` : ''}</td>
                  <td><div>{f.message}</div>{f.detail?.text && <code className="small">{f.detail.text}</code>}<div className="small muted">{f.rule}</div></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>

      <VulnPanel repo={repo} />

      <div className="panel">
        <h2>Pull requests &amp; issues</h2>
        <p className="muted small">
          {s.discussions.github
            ? <>Linked from <code>{s.discussions.github}</code>: {fmt(s.discussions.count)} synced{s.discussions.synced_at ? `, last ${s.discussions.synced_at.slice(0, 16).replace('T', ' ')} UTC` : ''}. Gives Claude the reasoning behind changes.</>
            : 'This repository does not come from GitHub, so there is nothing to sync.'}
        </p>
        {s.discussions.github && (
          <div><button className="btn" disabled={isActive(repo.active_job)}
            onClick={async () => { try { setJob(await api.syncDiscussions(repo.id)) } catch (e) { setErr((e as Error).message) } }}>
            {s.discussions.count ? 'Sync new activity' : 'Sync from GitHub'}</button></div>
        )}
        {job && <JobProgress job={job} onDone={() => qc.invalidateQueries({ queryKey: ['safety', repo.id] })} />}
      </div>
    </div>
  )
}

function AssessView({ a }: { a: any }) {
  if (a.error) return <div className="error">{a.error}</div>
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row">
        <span className={`pill ${a.risk_level === 'high' ? 'bad' : a.risk_level === 'medium' ? 'warn' : 'ok'}`}>{a.risk_level} risk</span>
        <span className="small muted">{a.blast_radius.callers} callers · {a.tests.length} tests · {a.hidden_coupling.filter((p: any) => p.hidden).length} hidden partners · {a.contracts.length} contracts</span>
      </div>
      {a.must.length > 0 && <div><strong className="small">Must</strong><ul className="small">{a.must.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>}
      {a.should.length > 0 && <div><strong className="small">Should</strong><ul className="small">{a.should.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>}
      {a.blast_radius.top.length > 0 && (
        <details><summary className="small">Callers ({a.blast_radius.callers})</summary>
          <ul className="small mono">{a.blast_radius.top.map((c: any) => <li key={c.uid}>{c.uid.split(':').slice(2, -1).join(':')} <span className="muted">({c.via}{c.confidence ? `, ${c.confidence}` : ''})</span></li>)}</ul>
        </details>
      )}
      {a.tests.length > 0 && (
        <details><summary className="small">Tests to run ({a.tests.length})</summary>
          <ul className="small mono">{a.tests.map((t: any) => <li key={t.test}>{t.test} <span className="muted">{t.how}</span></li>)}</ul>
        </details>
      )}
    </div>
  )
}
