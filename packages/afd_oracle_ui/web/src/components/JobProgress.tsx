import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, isActive, type Job } from '../api'

const INGEST_STEPS: [string, string][] = [
  ['clone', 'Clone'], ['history', 'Git history'], ['snapshot', 'Code structure'], ['moves', 'Moves'],
  ['reverts', 'Reverts'], ['branches', 'Branches'], ['cochange', 'Co-change'], ['metrics', 'Metrics'],
  ['safety', 'Change safety'], ['catalog_index', 'Line index'], ['catalog', 'Candidate catalogue'],
  ['neo4j', 'Graph (Neo4j)'], ['embed_symbols', 'Embed symbols'], ['embed_commits', 'Embed commits'],
  ['packets', 'Mining packets'],
]

/** Live view of a job: step checklist (ingest), counters (mine), and the streamed log. */
export default function JobProgress({ job: initial, onDone }: { job: Job; onDone?: (j: Job) => void }) {
  const [job, setJob] = useState<Job>(initial)
  const [log, setLog] = useState<string[]>([])
  const qc = useQueryClient()
  const logRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    setJob(initial)
    setLog([])
    const es = new EventSource(`/api/jobs/${initial.id}/events`)
    es.addEventListener('log', (e) => setLog((l) => [...l.slice(-400), JSON.parse((e as MessageEvent).data)]))
    es.addEventListener('job', (e) => setJob((j) => ({ ...j, ...JSON.parse((e as MessageEvent).data) })))
    es.addEventListener('end', () => {
      es.close()
      api.job(initial.id).then((j) => {
        setJob(j)
        onDone?.(j)
        qc.invalidateQueries()
      })
    })
    return () => es.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial.id])

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [log])

  const statusPill =
    job.status === 'succeeded' ? 'ok' : job.status === 'failed' ? 'bad' : job.status === 'cancelled' ? 'warn' : 'info'

  return (
    <div className="stack">
      <div className="row">
        <span className={`pill ${statusPill}`}>{job.status}</span>
        <strong>{job.title}</strong>
        <span className="spacer" />
        {isActive(job) && (
          <button className="btn sm danger" onClick={() => api.cancel(job.id).then(setJob)}>Cancel</button>
        )}
      </div>
      {job.error && job.status !== 'succeeded' && <div className="error">{job.error}</div>}
      {job.kind === 'ingest' && <Steps job={job} />}
      {job.kind === 'mine' && <MineCounters job={job} />}
      <details open={isActive(job)}>
        <summary className="small muted">Log ({log.length} lines)</summary>
        <pre className="log" ref={logRef}>{log.join('\n') || 'Waiting for output…'}</pre>
      </details>
    </div>
  )
}

function Steps({ job }: { job: Job }) {
  const p = job.progress || {}
  const steps = INGEST_STEPS.filter(([k]) => k !== 'clone' || p.clone)
  const firstPending = steps.findIndex(([k]) => !(p[k] && p[k].done >= p[k].total && p[k].total > 0))
  return (
    <div className="steps">
      {steps.map(([key, label], i) => {
        const s = p[key]
        const done = !!s && s.total > 0 && s.done >= s.total
        const active = !done && isActive(job) && (s ? true : i === firstPending)
        const pct = s && s.total > 1 ? Math.round((100 * s.done) / s.total) : null
        return (
          <div key={key} className={`step ${done ? 'done' : ''} ${active ? 'active' : ''}`}>
            <span className="icon" aria-hidden="true" />
            <span>{label}</span>
            <div className="bar" aria-hidden={pct === null}>{pct !== null && <i style={{ width: `${pct}%` }} />}</div>
            <span className="small muted mono">
              {pct !== null ? `${s!.done.toLocaleString()}/${s!.total.toLocaleString()}` : done ? 'done' : ''}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function MineCounters({ job }: { job: Job }) {
  const [counts, setCounts] = useState<{ insights: Record<string, number>; packets: Record<string, number> } | null>(null)
  useEffect(() => {
    let stop = false
    const tick = () =>
      job.repo &&
      api.run(job.repo, job.id).then((c) => {
        if (!stop) setCounts(c)
      })
    tick()
    const t = isActive(job) ? setInterval(tick, 4000) : undefined
    return () => {
      stop = true
      if (t) clearInterval(t)
    }
  }, [job.id, job.status, job.repo])
  const i = counts?.insights || {}
  const total = Object.values(job.args?.packets || {}).reduce((a: number, b: any) => a + Number(b), 0)
  return (
    <div className="stats">
      <div className="stat"><b>{total}</b><span>candidates in this run</span></div>
      <div className="stat"><b>{i.proposed ?? 0}</b><span>proposed, awaiting review</span></div>
      <div className="stat"><b>{i.verified ?? 0}</b><span>verified</span></div>
      <div className="stat"><b>{i.rejected ?? 0}</b><span>rejected by reviewer</span></div>
      {job.summary?.cost_usd != null && (
        <div className="stat"><b>${Number(job.summary.cost_usd).toFixed(2)}</b><span>orchestrator cost (list price)</span></div>
      )}
    </div>
  )
}
