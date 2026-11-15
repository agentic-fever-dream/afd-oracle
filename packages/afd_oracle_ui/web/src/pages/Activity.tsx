import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ago, api, isActive, type Job } from '../api'
import JobProgress from '../components/JobProgress'

export function JobTable({ repo }: { repo?: string }) {
  const [open, setOpen] = useState<Job | null>(null)
  const { data } = useQuery({
    queryKey: ['jobs', repo ?? ''],
    queryFn: () => api.jobs(repo),
    refetchInterval: (q) => (q.state.data?.some(isActive) ? 3000 : 15000),
  })
  if (!data?.length) return <p className="empty">No activity yet.</p>
  return (
    <>
      <div className="panel tablewrap">
        <table>
          <thead>
            <tr><th>What</th>{!repo && <th>Repository</th>}<th>Status</th><th>Started</th><th>Duration</th><th /></tr>
          </thead>
          <tbody>
            {data.map((j) => (
              <tr key={j.id}>
                <td style={{ fontWeight: 500 }}>{j.title}</td>
                {!repo && <td>{j.repo ? <Link to={`/repos/${encodeURIComponent(j.repo)}`}>{j.repo}</Link> : '—'}</td>}
                <td><span className={`pill ${j.status === 'succeeded' ? 'ok' : j.status === 'failed' ? 'bad' : j.status === 'cancelled' ? 'warn' : 'info'}`}>{j.status}</span></td>
                <td className="small">{ago(j.started ?? j.created)}</td>
                <td className="small mono">{j.started ? dur((j.ended ?? Date.now() / 1000) - j.started) : ''}</td>
                <td><button className="btn sm" onClick={() => setOpen(j)}>Details</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {open && (
        <>
          <div className="drawer-backdrop" onClick={() => setOpen(null)} />
          <aside className="drawer" aria-label="Job details">
            <div className="row"><span className="spacer" /><button className="btn sm" onClick={() => setOpen(null)}>Close</button></div>
            <JobProgress job={open} />
          </aside>
        </>
      )}
    </>
  )
}

const dur = (s: number) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`)

export default function Activity() {
  return (
    <>
      <header className="stack" style={{ gap: 6 }}>
        <span className="eyebrow">History</span>
        <h1>Activity</h1>
        <p className="muted">Ingests, mining runs and setup tasks, newest first.</p>
      </header>
      <JobTable />
    </>
  )
}
