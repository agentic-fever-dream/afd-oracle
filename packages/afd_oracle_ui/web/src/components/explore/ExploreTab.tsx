import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type ExploreEdge, type ExploreHit, type ExploreNode, type RepoDetail } from '../../api'
import { CommitView } from '../InsightsTab'
import GraphCanvas, { type Membership } from './GraphCanvas'
import VectorMap from './VectorMap'
import { colorOf, GROUP_LABELS, relLabel } from './canvas'

const SCOPES = [
  { key: 'code', label: 'Code', hint: 'Functions and classes, by meaning and keywords' },
  { key: 'commits', label: 'Commits', hint: 'Commit messages and diffs' },
  { key: 'insights', label: 'Insights', hint: 'Verified findings from mining' },
  { key: 'vulns', label: 'Vulns', hint: 'Scanner findings: vulnerable dependencies, code issues, misconfigurations, secrets' },
  { key: 'graph', label: 'Names', hint: 'Exact names: symbols, files, branches, authors, commit hashes' },
] as const
type Scope = (typeof SCOPES)[number]['key']
const MAP_SCOPE: Record<string, string> = { code: 'code', commits: 'commits', insights: 'insights', vulns: 'vulns' }
const MAX_NODES = 600

type Graph = { nodes: Map<string, ExploreNode>; edges: Map<string, ExploreEdge>; groups: Record<string, Membership> }
const emptyGraph = (): Graph => ({ nodes: new Map(), edges: new Map(), groups: {} })
const edgeKey = (e: ExploreEdge) => `${e.source}|${e.type}|${e.target}`

const hitToNode = (h: ExploreHit): ExploreNode => ({
  uid: h.uid, group: h.group, kind: h.kind || h.payload?.kind || h.group, labels: h.labels || [], title: h.title,
  subtitle: h.subtitle, alive: h.payload?.alive ?? h.props?.alive, props: h.props || h.payload || {},
})

/** Explore the repository's graph and vector store: search, then grow a graph outward from what you find. */
export default function ExploreTab({ repo }: { repo: RepoDetail }) {
  const [scope, setScope] = useState<Scope>('code')
  const [mode, setMode] = useState('hybrid')
  const [liveOnly, setLiveOnly] = useState(true)
  const [q, setQ] = useState('')
  const [results, setResults] = useState<{ title: string; hits: ExploreHit[]; note?: string } | null>(null)
  const [searching, setSearching] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [graph, setGraph] = useState<Graph>(emptyGraph)
  const [selected, setSelected] = useState<string | null>(null)
  const [view, setView] = useState<'graph' | 'map'>('graph')
  const [mapQuery, setMapQuery] = useState<string | undefined>(undefined)
  const [fit, setFit] = useState(0)
  const [busy, setBusy] = useState<string | null>(null)

  const mapCollection = MAP_SCOPE[scope] || 'code'
  const { data: map, isFetching: mapLoading, error: mapErr } = useQuery({
    queryKey: ['vmap', repo.id, mapCollection, mapQuery],
    queryFn: () => api.explore.map(repo.id, mapCollection, mapQuery),
    enabled: view === 'map',
    staleTime: 5 * 60_000,
  })

  const merge = useCallback((nodes: ExploreNode[], edges: ExploreEdge[], anchor?: string) => {
    setGraph((g) => {
      const next: Graph = { nodes: new Map(g.nodes), edges: new Map(g.edges), groups: { ...g.groups } }
      for (const n of nodes) {
        if (next.nodes.size >= MAX_NODES && !next.nodes.has(n.uid)) break
        if (!next.nodes.has(n.uid) && anchor && !next.groups[n.uid]) {
          // group the new node under the node it was expanded from, by the relationship that connects them
          const e = edges.find((x) => (x.source === anchor && x.target === n.uid) || (x.target === anchor && x.source === n.uid))
          if (e) next.groups[n.uid] = { parent: anchor, type: e.type, dir: e.source === anchor ? 'out' : 'in' }
        }
        next.nodes.set(n.uid, { ...next.nodes.get(n.uid), ...n })
      }
      for (const e of edges) if (next.nodes.has(e.source) && next.nodes.has(e.target)) next.edges.set(edgeKey(e), e)
      return next
    })
  }, [])

  async function runSearch(e?: React.FormEvent) {
    e?.preventDefault()
    if (!q.trim()) return
    setSearching(true); setErr(null)
    try {
      const r = await api.explore.search(repo.id, q.trim(), scope, mode, scope === 'code' && liveOnly ? true : undefined)
      setResults({ title: `${r.hits.length} results`, hits: r.hits, note: r.note })
      if (view === 'map' && MAP_SCOPE[scope]) setMapQuery(q.trim())
    } catch (ex) {
      setErr((ex as Error).message)
    } finally {
      setSearching(false)
    }
  }

  async function addAndSelect(h: ExploreHit) {
    if (!h.uid) return
    merge([hitToNode(h)], [])
    setSelected(h.uid)
    setView('graph')
  }

  async function expand(uid: string, type?: string, dir?: string, limit?: number) {
    setBusy(uid + (type || '')); setErr(null)
    try {
      const r = await api.explore.neighbors(repo.id, uid, type, dir, limit)
      merge(r.nodes, r.edges, uid)
      // also connect new nodes to ones already on the canvas
    } catch (ex) {
      setErr((ex as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function findSimilar(uid: string, title: string) {
    setBusy('similar'); setErr(null)
    try {
      const r = await api.explore.similar(repo.id, uid)
      setResults({ title: `Similar to ${title}`, hits: r.hits })
    } catch (ex) {
      setErr((ex as Error).message)
    } finally {
      setBusy(null)
    }
  }

  function hide(uid: string) {
    setGraph((g) => {
      const groups = { ...g.groups }
      delete groups[uid]
      const next: Graph = { nodes: new Map(g.nodes), edges: new Map(), groups }
      next.nodes.delete(uid)
      for (const [k, e] of g.edges) if (e.source !== uid && e.target !== uid) next.edges.set(k, e)
      return next
    })
    setSelected(null)
  }

  // Deep links: ?q=…&scope=… runs a search, ?node=<uid> starts from a node and its neighbours, ?view=map.
  const [params] = useSearchParams()
  const booted = useRef(false)
  useEffect(() => {
    if (booted.current) return
    booted.current = true
    const sc = params.get('scope') as Scope | null
    if (sc && SCOPES.some((x) => x.key === sc)) setScope(sc)
    const qq = params.get('q')
    if (qq) {
      setQ(qq)
      api.explore.search(repo.id, qq, sc || 'code', 'hybrid', (sc || 'code') === 'code' ? true : undefined)
        .then((r) => setResults({ title: `${r.hits.length} results`, hits: r.hits, note: r.note }))
        .catch((ex) => setErr((ex as Error).message))
    }
    const uid = params.get('node')
    if (uid) {
      api.explore.node(repo.id, uid).then((n) => { merge([n], []); setSelected(uid); expand(uid) })
        .catch((ex) => setErr((ex as Error).message))
    }
    if (params.get('view') === 'map') { setView('map'); if (qq) setMapQuery(qq) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Esc clears the selection.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSelected(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const nodes = useMemo(() => Array.from(graph.nodes.values()), [graph.nodes])
  const edges = useMemo(() => Array.from(graph.edges.values()), [graph.edges])
  const groups = useMemo(() => Array.from(new Set(nodes.map((n) => n.group))), [nodes])
  const scopeInfo = SCOPES.find((s) => s.key === scope)!

  return (
    <div className="explore">
      {/* ------------------------------------------------------------ search */}
      <aside className="explore-pane explore-search">
        <form className="stack" style={{ gap: 10 }} onSubmit={runSearch}>
          <div className="segmented" role="tablist" aria-label="Search in">
            {SCOPES.map((s) => (
              <button key={s.key} type="button" role="tab" aria-selected={scope === s.key}
                className={scope === s.key ? 'on' : ''} onClick={() => setScope(s.key)}>{s.label}</button>
            ))}
          </div>
          <div className="search-box">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>
            <input id="explore-q" className="input" value={q} onChange={(e) => setQ(e.target.value)} autoFocus
              placeholder={scope === 'graph' ? 'e.g. Blueprint, app.py, a commit hash' : scope === 'commits' ? 'e.g. fix session cookie expiry' : 'e.g. how requests are dispatched'} />
          </div>
          <div className="row" style={{ gap: 8 }}>
            {scope !== 'graph' && (
              <select id="explore-mode" className="select" value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Match by">
                <option value="hybrid">Meaning + keywords</option><option value="dense">Meaning only</option><option value="sparse">Keywords only</option>
              </select>
            )}
            {scope === 'code' && <label className="check small"><input type="checkbox" checked={liveOnly} onChange={(e) => setLiveOnly(e.target.checked)} />Current code only</label>}
            <span className="spacer" />
            <button className="btn primary sm" disabled={!q.trim() || searching}>{searching ? 'Searching…' : 'Search'}</button>
          </div>
          <p className="small faint">{scopeInfo.hint}</p>
        </form>
        {err && <div className="error">{err}</div>}
        {results ? (
          <div className="stack" style={{ gap: 6, minHeight: 0 }}>
            <div className="row">
              <strong className="small">{results.title}</strong>
              <span className="spacer" />
              {results.hits.length > 0 && (
                <button className="btn sm" onClick={() => { merge(results.hits.filter((h) => h.uid).map(hitToNode), []); setView('graph'); setFit((f) => f + 1) }}>
                  Add all to graph
                </button>
              )}
            </div>
            {results.note && <p className="small muted">{results.note}</p>}
            <ul className="hits">
              {results.hits.map((h, i) => (
                <li key={h.uid + i}>
                  <button className={`hit ${graph.nodes.has(h.uid) ? 'in-graph' : ''} ${selected === h.uid ? 'on' : ''}`} onClick={() => addAndSelect(h)}>
                    <span className="dot" style={{ background: colorOf(h.group), boxShadow: 'none' }} />
                    <span className="hit-body">
                      <span className="hit-title">{h.title}</span>
                      <span className="hit-sub">{h.subtitle || GROUP_LABELS[h.group] || h.group}</span>
                    </span>
                    {h.score !== undefined && <span className="hit-score mono">{h.score.toFixed(2)}</span>}
                  </button>
                </li>
              ))}
              {!results.hits.length && <li className="small muted" style={{ padding: 8 }}>Nothing found. Try another scope or wording.</li>}
            </ul>
          </div>
        ) : (
          <div className="explore-help small muted">
            <p><strong>Search</strong> code, commits or insights by meaning, or exact names.</p>
            <p><strong>Click</strong> a result to put it on the canvas. <strong>Double-click</strong> a node to pull in its neighbours, or pick an edge type in the inspector.</p>
            <p><strong>Vector map</strong> shows the whole collection in 2D, so you can see where your query lands.</p>
          </div>
        )}
      </aside>

      {/* ------------------------------------------------------------ canvas */}
      <section className="explore-pane explore-canvas">
        <div className="canvas-bar">
          <div className="segmented sm">
            <button className={view === 'graph' ? 'on' : ''} onClick={() => setView('graph')}>Graph</button>
            <button className={view === 'map' ? 'on' : ''} onClick={() => { setView('map'); if (q.trim() && MAP_SCOPE[scope]) setMapQuery(q.trim()) }}>Vector map</button>
          </div>
          {view === 'graph' ? (
            <>
              <span className="small muted">{nodes.length} nodes · {edges.length} edges</span>
              <span className="spacer" />
              <button className="btn sm" disabled={!nodes.length} onClick={() => setFit((f) => f + 1)}>Fit</button>
              <button className="btn sm" disabled={!nodes.length} onClick={() => { setGraph(emptyGraph()); setSelected(null) }}>Clear</button>
            </>
          ) : (
            <>
              <span className="small muted">
                {mapLoading ? 'Projecting vectors…' : map ? `${map.total.toLocaleString()} ${mapCollection}${map.sampled ? ' (sample)' : ''}` : ''}
                {mapQuery ? ` · query “${mapQuery}”` : ''}
              </span>
              <span className="spacer" />
              {mapQuery && <button className="btn sm" onClick={() => setMapQuery(undefined)}>Clear query</button>}
            </>
          )}
        </div>
        {view === 'graph' ? (
          nodes.length ? (
            <GraphCanvas nodes={nodes} edges={edges} selected={selected} groups={graph.groups} fitSignal={fit}
              onSelect={setSelected} onExpand={(uid) => { setSelected(uid); expand(uid) }} />
          ) : (
            <div className="canvas-empty">
              <svg viewBox="0 0 120 80" aria-hidden="true"><g fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M20 40L60 18L100 40L60 62Z" opacity=".5" /><path d="M60 18V62M20 40H100" opacity=".3" /></g><g fill="currentColor"><circle cx="20" cy="40" r="5" /><circle cx="60" cy="18" r="6" /><circle cx="100" cy="40" r="5" /><circle cx="60" cy="62" r="5" /><circle cx="60" cy="40" r="8" opacity=".8" /></g></svg>
              <strong>Start from a search</strong>
              <span className="small">Results you click appear here, and grow outward as you expand them.</span>
            </div>
          )
        ) : mapErr ? (
          <div className="canvas-empty"><div className="error">{(mapErr as Error).message}</div></div>
        ) : map && map.points.length ? (
          <VectorMap map={map} selected={selected}
            onSelect={(p) => { merge([{ uid: p.uid, group: ({ sym: 'Symbol', commit: 'Commit', insight: 'Insight', vuln: 'Vulnerability' } as Record<string, string>)[p.uid.split(':')[0]] || 'Node', kind: p.kind, labels: [], title: p.title, subtitle: p.subtitle, alive: p.alive, props: {} }], []); setSelected(p.uid) }} />
        ) : (
          <div className="canvas-empty"><span className="small">{mapLoading ? 'Building the map — the first time takes a few seconds…' : 'Nothing to map yet.'}</span></div>
        )}
        {view === 'graph' && groups.length > 0 && (
          <div className="map-legend">
            {groups.map((g) => <span key={g}><i style={{ background: colorOf(g) }} />{GROUP_LABELS[g] || g}</span>)}
            <span><i style={{ background: 'transparent', border: '1.5px dashed var(--faint)' }} />deleted</span>
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------ inspector */}
      <aside className="explore-pane explore-inspector">
        {selected ? (
          <Inspector repo={repo} uid={selected} busy={busy} onExpand={expand} onSimilar={findSimilar} onHide={hide}
            onSelect={setSelected} inGraph={graph.nodes.has(selected)} edges={edges} nodes={graph.nodes} />
        ) : (
          <div className="explore-help small muted">
            <p><strong>Nothing selected.</strong></p>
            <p>Select a node to see its properties, how it connects, and the code or commit behind it.</p>
            <p className="faint">Shapes: ■ files and classes, ◆ commits, ● everything else. Dashed outline: no longer in the code.</p>
          </div>
        )}
      </aside>
    </div>
  )
}

const TS_KEYS = new Set(['ts', 'first_ts', 'last_ts', 'tip_ts', 'created_at', 'updated_at'])
const HIDDEN = new Set(['uid', 'repo', 'dedupe_key', 'body_hash', 'lineage'])

function fmtProp(k: string, v: unknown): string {
  if (TS_KEYS.has(k) && v !== null && v !== '' && !isNaN(Number(v))) return new Date(Number(v) * 1000).toISOString().slice(0, 16).replace('T', ' ')
  if (Array.isArray(v)) return v.join(', ')
  if (typeof v === 'object' && v) return JSON.stringify(v)
  return String(v)
}

function Inspector({ repo, uid, busy, onExpand, onSimilar, onHide, onSelect, inGraph, edges, nodes }: {
  repo: RepoDetail; uid: string; busy: string | null; inGraph: boolean
  edges: ExploreEdge[]; nodes: Map<string, ExploreNode>
  onExpand: (uid: string, type?: string, dir?: string, limit?: number) => void
  onSimilar: (uid: string, title: string) => void; onHide: (uid: string) => void; onSelect: (uid: string) => void
}) {
  const { data: n, isLoading, error } = useQuery({ queryKey: ['xnode', repo.id, uid], queryFn: () => api.explore.node(repo.id, uid) })
  const [showDiff, setShowDiff] = useState(false)
  useEffect(() => setShowDiff(false), [uid])
  if (isLoading) return <p className="small muted">Loading…</p>
  if (error || !n) return <div className="error">{(error as Error)?.message || 'Not found'}</div>
  const onCanvas = (type: string, dir: string) =>
    edges.filter((e) => e.type === type && (dir === 'out' ? e.source === uid : e.target === uid)).length
  const linked = edges.filter((e) => e.source === uid || e.target === uid)
  const props = Object.entries(n.props).filter(([k, v]) => !HIDDEN.has(k) && v !== null && v !== '' && k !== 'message' && k !== 'summary' && k !== 'docstring')
  const long = (n.props.message || n.props.summary || n.props.docstring) as string | undefined
  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="stack" style={{ gap: 6 }}>
        <div className="row" style={{ gap: 6 }}>
          <span className="dot" style={{ background: colorOf(n.group), boxShadow: 'none' }} />
          <span className="tag">{n.kind}</span>
          {(n.alive === false || n.alive === 'False') && <span className="tag">deleted</span>}
        </div>
        <h3 style={{ fontSize: '1.02rem', textTransform: 'none', letterSpacing: '-0.01em', color: 'var(--fg)', overflowWrap: 'anywhere' }}>{n.title}</h3>
        {n.subtitle && <div className="small muted mono" style={{ overflowWrap: 'anywhere' }}>{n.subtitle}</div>}
      </div>
      <div className="row" style={{ gap: 6 }}>
        <button className="btn sm primary" disabled={!!busy} onClick={() => onExpand(uid)}>{busy === uid ? 'Expanding…' : 'Expand all'}</button>
        {n.has_vector && <button className="btn sm" disabled={!!busy} onClick={() => onSimilar(uid, n.title)}>{busy === 'similar' ? 'Searching…' : 'Find similar'}</button>}
        {n.group === 'Commit' && <button className="btn sm" onClick={() => setShowDiff((x) => !x)}>{showDiff ? 'Hide diff' : 'Show diff'}</button>}
        {inGraph && <button className="btn sm" onClick={() => onHide(uid)}>Hide</button>}
      </div>
      {long && <p className="small" style={{ whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto' }}>{long}</p>}
      {showDiff && n.props.hash && <CommitView repo={repo.id} sha={String(n.props.hash)} />}

      <section className="stack" style={{ gap: 6 }}>
        <h3 className="insp-h">Connections</h3>
        {!n.degrees.length && <p className="small muted">No connections.</p>}
        <ul className="degrees">
          {n.degrees.map((d) => {
            const shown = onCanvas(d.type, d.dir)
            return (
              <li key={d.type + d.dir + d.other}>
                <button disabled={!!busy} title={`Add up to ${shown ? Math.min(d.count, shown + 25) : Math.min(d.count, 25)} to the graph`}
                  onClick={() => onExpand(uid, d.type, d.dir, Math.min(200, shown ? shown + 25 : 25))}>
                  <span className="deg-type" title={`${d.type} (${d.dir === 'out' ? 'outgoing' : 'incoming'})`}>{relLabel(d.type, d.dir)}</span>
                  <span className="deg-other"><span className="dot" style={{ background: colorOf(d.other), boxShadow: 'none', width: 6, height: 6 }} />{relLabel(d.type, d.dir).toLowerCase().includes((GROUP_LABELS[d.other] || d.other).toLowerCase().split(' ')[0]) ? '' : GROUP_LABELS[d.other] || d.other}</span>
                  <span className="deg-count mono">{shown ? `${shown}/` : ''}{d.count.toLocaleString()}</span>
                </button>
              </li>
            )
          })}
        </ul>
      </section>

      {linked.length > 0 && (
        <section className="stack" style={{ gap: 6 }}>
          <h3 className="insp-h">On the canvas</h3>
          <ul className="degrees">
            {linked.slice(0, 40).map((e) => {
              const other = nodes.get(e.source === uid ? e.target : e.source)
              if (!other) return null
              return (
                <li key={edgeKey(e)}>
                  <button onClick={() => onSelect(other.uid)}>
                    <span className="deg-type" title={e.type}>{relLabel(e.type, e.source === uid ? 'out' : 'in')}</span>
                    <span className="deg-other" style={{ minWidth: 0 }}><span className="dot" style={{ background: colorOf(other.group), boxShadow: 'none', width: 6, height: 6 }} /><span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{other.title}</span></span>
                  </button>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      <section className="stack" style={{ gap: 6 }}>
        <h3 className="insp-h">Properties</h3>
        <dl className="props">
          {props.map(([k, v]) => (<div key={k}><dt>{k.replace(/_/g, ' ')}</dt><dd className={typeof v === 'string' && v.length > 20 ? 'mono' : ''}>{fmtProp(k, v)}</dd></div>))}
        </dl>
        <p className="small faint mono" style={{ overflowWrap: 'anywhere' }}>{uid}</p>
      </section>
    </div>
  )
}
