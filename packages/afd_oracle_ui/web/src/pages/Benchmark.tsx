import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fmt } from '../api'

type Bench = {
  exists: boolean
  progress: string[]
  running: { repo: string; phase: string; since: string } | null
  live: string[]
  memory: string[]
  results: Record<string, any>
  scans: Record<string, any>
}

const dur = (s?: number) => (s == null ? '' : s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`)

/** Live view of the multi-repository benchmark (data/bench): what's running, memory headroom, per-repo results. */
export default function Benchmark() {
  const { data } = useQuery({
    queryKey: ['bench'],
    queryFn: () => fetch('/api/bench').then((r) => r.json() as Promise<Bench>),
    refetchInterval: 5000,
  })
  if (!data) return <p className="muted">Loading…</p>
  if (!data.exists) return <p className="empty">No benchmark has been run.</p>
  const repos = Array.from(new Set([...Object.keys(data.results || {}), ...Object.keys(data.scans || {}),
    ...(data.running ? [data.running.repo] : [])]))
  const mem = (data.memory || []).map((l) => {
    const m = l.match(/^(\S+) free RAM ([\d.]+) GB, free commit ([\d.]+) GB/) || l.match(/^(\S+) free ([\d.]+) GB/)
    return m ? { t: m[1], ram: Number(m[2]), commit: m[3] ? Number(m[3]) : null } : null
  }).filter(Boolean) as { t: string; ram: number; commit: number | null }[]
  const last = mem[mem.length - 1]
  const maxV = Math.max(10, ...mem.map((m) => Math.max(m.ram, m.commit || 0)))
  const done = data.progress.some((l) => l.includes('SCANS ALL DONE'))
  const guard = data.progress.filter((l) => l.includes('MEMGUARD:')).slice(-1)[0]

  return (
    <>
      <header className="stack" style={{ gap: 6 }}>
        <span className="eyebrow">Benchmark</span>
        <h1>Ten-repository run</h1>
        <p className="muted">Ingest (no embeddings), candidate catalogue, graph projection and vulnerability scans across ten public repositories. Refreshes every 5 seconds.</p>
      </header>

      <div className="stats">
        <div className="stat"><b>{Object.values(data.results || {}).filter((r: any) => r.ok).length} / 10</b><span>ingested</span></div>
        <div className="stat"><b>{Object.values(data.scans || {}).filter((r: any) => r.ok).length} / 10</b><span>scanned</span></div>
        <div className="stat"><b>{data.running ? data.running.repo : done ? 'finished' : 'idle'}</b>
          <span>{data.running ? `${data.running.phase} since ${data.running.since}` : 'current step'}</span></div>
        <div className="stat"><b>{last ? `${last.commit ?? '–'} GB` : '–'}</b><span>free commit (RAM {last ? `${last.ram} GB` : '–'}); guard stops below 2 GB</span></div>
      </div>
      {guard && !data.running && <div className="notice">{guard}</div>}

      {mem.length > 1 && (
        <div className="panel">
          <h2>Memory headroom</h2>
          <svg viewBox={`0 0 ${mem.length * 6} 100`} preserveAspectRatio="none" style={{ width: '100%', height: 110 }} role="img"
            aria-label="Free RAM and free commit over time">
            <line x1="0" x2={mem.length * 6} y1={100 - (2 / maxV) * 100} y2={100 - (2 / maxV) * 100} stroke="var(--bad)" strokeDasharray="4 3" strokeWidth="0.6" />
            <polyline fill="none" stroke="var(--accent)" strokeWidth="1.5" vectorEffect="non-scaling-stroke"
              points={mem.map((m, i) => `${i * 6},${100 - ((m.commit ?? m.ram) / maxV) * 100}`).join(' ')} />
            <polyline fill="none" stroke="var(--faint)" strokeWidth="1" vectorEffect="non-scaling-stroke"
              points={mem.map((m, i) => `${i * 6},${100 - (m.ram / maxV) * 100}`).join(' ')} />
          </svg>
          <p className="small muted">Accent: free commit. Grey: free RAM. Dashed: the 2 GB guard line. Last {mem.length} samples, 30 s apart.</p>
        </div>
      )}

      <div className="panel tablewrap">
        <table>
          <thead><tr><th>Repository</th><th>Ingest</th><th className="num">Time</th><th className="num">Candidates</th>
            <th className="num">Kinds hit</th><th>Builder errors</th><th>Scan</th><th className="num">Findings</th></tr></thead>
          <tbody>
            {repos.map((rid) => {
              const r = data.results?.[rid] || {}
              const c = r.catalog || {}
              const sc = data.scans?.[rid]
              const isRunning = data.running?.repo === rid
              return (
                <tr key={rid}>
                  <td className="mono">{r.ok ? <Link to={`/repos/${encodeURIComponent(rid)}`}>{rid}</Link> : rid}</td>
                  <td>{isRunning && data.running?.phase === 'ingest' ? <span className="pill info">running</span>
                    : r.ok ? <span className="pill ok">ok</span> : r.rc != null ? <span className="pill bad">failed</span> : <span className="pill">queued</span>}</td>
                  <td className="num">{isRunning ? "" : dur(r.seconds)}</td>
                  <td className="num">{c.candidates != null ? fmt(c.candidates) : ''}</td>
                  <td className="num">{c.with_hits ?? ''}</td>
                  <td className="small">{Object.keys(c.errors || {}).join(', ') || (r.ok ? 'none' : '')}</td>
                  <td>{isRunning && data.running?.phase === 'scan' ? <span className="pill info">running</span>
                    : sc?.ok ? <span className="pill ok">ok</span> : sc ? <span className="pill bad">failed</span> : <span className="pill">waiting</span>}</td>
                  <td className="num">{sc?.total != null ? fmt(sc.total) : ''}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {data.running && (
        <div className="panel">
          <h2>Live: {data.running.repo}</h2>
          <pre className="log" style={{ maxHeight: 260 }}>{data.live.join('\n') || 'Waiting for output…'}</pre>
        </div>
      )}

      <div className="panel">
        <h2>Progress log</h2>
        <pre className="log" style={{ maxHeight: 300 }}>{data.progress.slice().reverse().join('\n')}</pre>
      </div>
    </>
  )
}
