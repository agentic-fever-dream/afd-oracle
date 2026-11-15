import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, fmt, isActive, type Job, type RepoDetail } from '../api'
import JobProgress from './JobProgress'

const SEV = ['critical', 'high', 'medium', 'low', 'info']
const SEV_PILL: Record<string, string> = { critical: 'bad', high: 'bad', medium: 'warn', low: 'info', info: '' }
const NETWORK: Record<string, string> = {
  none: 'runs offline', download: 'downloads its rules or database; sends nothing',
  sends: 'sends package names or code to an outside service',
}

/** Vulnerability scanners run in Docker over HEAD (afd-oracle scan): counts, last runs, and the findings list. */
export default function VulnPanel({ repo }: { repo: RepoDetail }) {
  const qc = useQueryClient()
  const [severity, setSeverity] = useState('')
  const [scanner, setScanner] = useState('')
  const [job, setJob] = useState<Job | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const { data } = useQuery({ queryKey: ['vulns', repo.id, severity, scanner], queryFn: () => api.vulns(repo.id, severity, scanner) })

  const totals: Record<string, number> = {}
  for (const c of data?.counts || []) totals[c.severity] = (totals[c.severity] || 0) + c.n
  const scanners = Array.from(new Set((data?.runs || []).map((r) => r.scanner)))

  async function run() {
    setErr(null)
    try { setJob(await api.scan(repo.id)) } catch (e) { setErr((e as Error).message) }
  }

  return (
    <div className="panel">
      <div className="row">
        <div className="panel-head">
          <h2>Vulnerability scans</h2>
          <p className="muted small">Semgrep, Bandit, Trivy, Gitleaks, Checkov and Hadolint run in Docker against a read-only copy of HEAD. None of them sends your code or package names anywhere. Findings are linked to files and functions in the graph and searchable in Explore.</p>
        </div>
        <span className="spacer" />
        <button className="btn primary" disabled={isActive(repo.active_job)} onClick={run}>
          {data?.runs?.length ? 'Scan again' : 'Run scanners'}
        </button>
      </div>
      {err && <div className="error">{err}</div>}
      {job && <JobProgress job={job} onDone={() => qc.invalidateQueries({ queryKey: ['vulns', repo.id] })} />}
      {!data?.runs?.length ? (
        <p className="empty">Not scanned yet. A scan takes a few minutes; images are pulled on first use.</p>
      ) : (
        <>
          <div className="stats">
            {SEV.map((s) => (
              <button key={s} className="stat" style={{ cursor: 'pointer', textAlign: 'left', outline: severity === s ? '2px solid var(--accent)' : undefined }}
                onClick={() => setSeverity(severity === s ? '' : s)}>
                <b>{fmt(totals[s] || 0)}</b><span>{s}</span>
              </button>
            ))}
          </div>
          <div className="tablewrap"><table>
            <thead><tr><th>Scanner</th><th>Last run</th><th className="num">Findings</th><th>Status</th><th>Network</th></tr></thead>
            <tbody>
              {data.runs.map((r) => (
                <tr key={r.scanner} style={{ cursor: 'pointer' }} onClick={() => setScanner(scanner === r.scanner ? '' : r.scanner)}>
                  <td className="mono small" style={{ fontWeight: scanner === r.scanner ? 600 : 400 }}>{r.scanner}</td>
                  <td className="small">{new Date(r.ran_at * 1000).toLocaleString()}</td>
                  <td className="num">{fmt(r.findings)}</td>
                  <td className="small">{r.status === 'ok' ? <span className="pill ok">ok</span> : <span className="muted">{r.status}</span>}</td>
                  <td className="small muted">{NETWORK[r.detail] || r.detail}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
          <div className="row">
            <strong className="small">{data.findings.length} findings{severity ? ` · ${severity}` : ''}{scanner ? ` · ${scanner}` : ''}</strong>
            {(severity || scanner) && <button className="btn sm" onClick={() => { setSeverity(''); setScanner('') }}>Clear filter</button>}
            <span className="spacer" />
            <span className="small faint">{scanners.length} scanners</span>
          </div>
          <div className="tablewrap"><table>
            <thead><tr><th>Severity</th><th>Where</th><th>What</th><th>Scanner</th></tr></thead>
            <tbody>
              {data.findings.slice(0, 300).map((f) => (
                <tr key={f.id}>
                  <td><span className={`pill ${SEV_PILL[f.severity] || ''}`}>{f.severity}</span></td>
                  <td className="mono small">{f.path || '—'}{f.line ? `:${f.line}` : ''}{f.package ? <div className="faint">{f.package} {f.version}{f.fixed ? ` → ${f.fixed}` : ''}</div> : null}</td>
                  <td><div>{f.title}</div><div className="small muted">{[f.cve, f.cwe].filter(Boolean).join(' · ')}{f.url ? <> · <a href={f.url} target="_blank" rel="noreferrer">details</a></> : null}</div></td>
                  <td className="mono small">{f.scanner}<div className="faint">{f.rule}</div></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </>
      )}
    </div>
  )
}
