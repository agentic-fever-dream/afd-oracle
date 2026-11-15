import { useEffect, useMemo, useRef, useState } from 'react'
import type { MapPoint, VectorMap as VMap } from '../../api'
import { blockWheelScroll, fitCanvas, LabelPlacer, themeColors, truncate, zoomAt, type View } from './canvas'

/** 24 distinguishable mid-tone hues that read on both light and dark backgrounds. */
const TOPIC = ['#4f9fe0', '#43b97f', '#e39b33', '#d96aa0', '#9a7cf0', '#ec6a58', '#2aa9a0', '#b9a03c', '#6c8cf5',
  '#8fbf3a', '#e0784a', '#b06ad9', '#3fb6d6', '#d4ae2c', '#e25b84', '#5bb06a', '#7b8fb3', '#c98a5a', '#4cc0b0',
  '#a3a3f2', '#d27fd0', '#76a93a', '#e09a9a', '#5a9fa8']

type Props = { map: VMap; selected: string | null; onSelect: (p: MapPoint) => void }

/**
 * Topic map. The collection is clustered by meaning on the server; each topic is an island labelled with its
 * distinctive terms. Zoom in and individual items get labels. Search hits glow and the query lands as a star.
 */
export default function VectorMap({ map, selected, onSelect }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const view = useRef<View>({ tx: 0, ty: 0, k: 1 })
  const drag = useRef<{ sx: number; sy: number; tx: number; ty: number; moved: boolean } | null>(null)
  const [hover, setHover] = useState<{ p: MapPoint; x: number; y: number } | null>(null)
  const [focusTopic, setFocusTopic] = useState<number | null>(null)
  const [, redraw] = useState(0)
  const hits = useMemo(() => new Set(map.hits || []), [map.hits])
  const clusters = map.clusters || []
  const colorIdx = useMemo(() => new Map(clusters.map((c, i) => [c.id, i])), [clusters])
  const colorOf = (c: number) => TOPIC[(colorIdx.get(c) ?? 0) % TOPIC.length]

  const scale = () => Math.min(canvas.current!.clientWidth - 250, canvas.current!.clientHeight) * 0.47
  const origin = () => ({ x: (canvas.current!.clientWidth - 250) / 2, y: canvas.current!.clientHeight / 2 })
  const toScreen = (p: { x: number; y: number }) => {
    const v = view.current, s = scale(), o = origin()
    return { x: v.tx + (o.x + p.x * s) * v.k, y: v.ty + (o.y - p.y * s) * v.k }
  }

  function draw() {
    const c = canvas.current
    if (!c || c.clientWidth < 10) return
    const { w, h, dpr } = fitCanvas(c)
    const ctx = c.getContext('2d')!
    const t = themeColors()
    const k = view.current.k
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)
    const s = scale() * k
    const anyHits = hits.size > 0
    const labels = new LabelPlacer(ctx)

    // islands
    for (const cl of clusters) {
      const ctr = toScreen(cl)
      const dim = focusTopic !== null && focusTopic !== cl.id
      ctx.globalAlpha = dim ? 0.25 : 1
      ctx.beginPath(); ctx.arc(ctr.x, ctr.y, cl.r * s, 0, Math.PI * 2)
      ctx.fillStyle = colorOf(cl.id) + '14'; ctx.fill()
      ctx.strokeStyle = colorOf(cl.id) + (focusTopic === cl.id ? 'cc' : '55'); ctx.lineWidth = focusTopic === cl.id ? 2 : 1; ctx.stroke()
    }
    // points
    for (const p of map.points) {
      if (hits.has(p.uid) || p.uid === selected) continue
      const sp = toScreen(p)
      if (sp.x < -5 || sp.y < -5 || sp.x > w + 5 || sp.y > h + 5) continue
      const dim = (focusTopic !== null && focusTopic !== p.c) || anyHits
      ctx.globalAlpha = dim ? 0.18 : 0.8
      ctx.fillStyle = colorOf(p.c)
      const r = Math.min(4, 1.8 + k * 0.5)
      ctx.beginPath(); ctx.arc(sp.x, sp.y, r, 0, Math.PI * 2); ctx.fill()
    }
    ctx.globalAlpha = 1

    // highlighted points: selected + search hits, always labelled
    const special = map.points.filter((p) => hits.has(p.uid) || p.uid === selected)
    for (const p of special) {
      const sp = toScreen(p)
      ctx.fillStyle = colorOf(p.c)
      ctx.beginPath(); ctx.arc(sp.x, sp.y, p.uid === selected ? 7 : 5.5, 0, Math.PI * 2); ctx.fill()
      ctx.strokeStyle = p.uid === selected ? t.fg : t.surface; ctx.lineWidth = 2; ctx.stroke()
    }
    // query star
    if (map.query) {
      const q = toScreen(map.query)
      ctx.fillStyle = t.accent; ctx.strokeStyle = t.surface; ctx.lineWidth = 2
      ctx.beginPath()
      for (let i = 0; i < 10; i++) {
        const r = i % 2 ? 5 : 12, a = (Math.PI / 5) * i - Math.PI / 2
        ctx.lineTo(q.x + Math.cos(a) * r, q.y + Math.sin(a) * r)
      }
      ctx.closePath(); ctx.fill(); ctx.stroke()
      labels.text('your search', q.x + 15, q.y - 6, { font: '600 12px Geist, system-ui', color: t.accent, halo: t.surface, force: true })
    }

    // topic labels (pills at the top of each island), biggest topics win collisions
    for (const cl of clusters) {
      if (focusTopic !== null && focusTopic !== cl.id) continue
      const ctr = toScreen(cl)
      const y = Math.max(14, ctr.y - cl.r * s - 2)
      labels.pill(`${truncate(cl.label, 34)}  ${cl.count}`, ctr.x, y, {
        font: '600 11.5px Geist, system-ui', color: t.fg, bg: t.surface, border: colorOf(cl.id) + 'aa',
      })
    }
    for (const p of special) {
      const sp = toScreen(p)
      labels.text(truncate(p.title, 40), sp.x + 9, sp.y + 4, { font: '600 11.5px Geist, system-ui', color: t.fg, halo: t.surface })
    }
    // item labels once zoomed in
    if (k >= 1.8) {
      let budget = 160
      for (const p of map.points) {
        if (budget <= 0) break
        if (focusTopic !== null && focusTopic !== p.c) continue
        const sp = toScreen(p)
        if (sp.x < 0 || sp.y < 0 || sp.x > w - 40 || sp.y > h) continue
        if (labels.text(truncate(p.title, 30), sp.x + 6, sp.y + 4, { font: '11px Geist, system-ui', color: t.muted, halo: t.surface })) budget--
      }
    }
  }

  useEffect(() => blockWheelScroll(canvas.current), [])
  useEffect(() => {
    draw()
    const ro = new ResizeObserver(() => draw())
    ro.observe(canvas.current!)
    return () => ro.disconnect()
  })

  function zoomToTopic(id: number) {
    const cl = clusters.find((c) => c.id === id)
    const c = canvas.current
    if (!cl || !c) return
    const k = Math.min(6, Math.max(1, 0.42 / cl.r))
    const s = scale(), o = origin()
    const cx = o.x + cl.x * s, cy = o.y - cl.y * s
    view.current = { k, tx: o.x - cx * k, ty: o.y - cy * k }
    setFocusTopic(id)
    redraw((n) => n + 1)
  }

  function nearest(sx: number, sy: number): MapPoint | null {
    let best: MapPoint | null = null, bd = 81
    for (const p of map.points) {
      if (focusTopic !== null && focusTopic !== p.c && !hits.has(p.uid)) continue
      const s = toScreen(p)
      const d = ((s.x - sx) ** 2 + (s.y - sy) ** 2) * (hits.has(p.uid) ? 0.4 : 1)
      if (d < bd) { bd = d; best = p }
    }
    return best
  }
  function topicAt(sx: number, sy: number): number | null {
    const s = scale() * view.current.k
    for (const cl of clusters) {
      const c = toScreen(cl)
      if ((c.x - sx) ** 2 + (c.y - sy) ** 2 < (cl.r * s) ** 2) return cl.id
    }
    return null
  }
  const local = (e: { clientX: number; clientY: number }) => {
    const r = canvas.current!.getBoundingClientRect()
    return { sx: e.clientX - r.left, sy: e.clientY - r.top }
  }

  return (
    <div className="canvas-wrap">
      <canvas
        ref={canvas}
        className="graph-canvas"
        onPointerDown={(e) => {
          const { sx, sy } = local(e)
          drag.current = { sx, sy, tx: view.current.tx, ty: view.current.ty, moved: false }
          ;(e.target as HTMLCanvasElement).setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          const { sx, sy } = local(e)
          const d = drag.current
          if (d) {
            if (Math.abs(sx - d.sx) + Math.abs(sy - d.sy) > 3) d.moved = true
            view.current = { ...view.current, tx: d.tx + sx - d.sx, ty: d.ty + sy - d.sy }
            draw()
            return
          }
          const p = nearest(sx, sy)
          setHover(p ? { p, x: sx, y: sy } : null)
        }}
        onPointerUp={(e) => {
          const d = drag.current
          drag.current = null
          if (!d || d.moved) return
          const { sx, sy } = local(e)
          const p = nearest(sx, sy)
          if (p) onSelect(p)
          else {
            const tpc = topicAt(sx, sy)
            if (tpc !== null && tpc !== focusTopic) zoomToTopic(tpc)
          }
        }}
        onDoubleClick={() => { view.current = { tx: 0, ty: 0, k: 1 }; setFocusTopic(null); redraw((n) => n + 1) }}
        onPointerLeave={() => setHover(null)}
        onWheel={(e) => {
          const { sx, sy } = local(e)
          view.current = zoomAt(view.current, sx, sy, Math.exp(-e.deltaY * 0.0015))
          draw()
        }}
      />
      <div className="map-hint">Click a topic to zoom in · double-click to reset · scroll to zoom</div>
      <div className="topic-legend">
        <div className="row" style={{ padding: '2px 6px' }}>
          <h3 className="insp-h" style={{ padding: 0 }}>Topics</h3>
          <span className="spacer" />
          {focusTopic !== null && (
            <button style={{ width: 'auto', display: 'inline', padding: '0 6px', fontSize: '0.72rem', color: 'var(--accent)' }}
              onClick={() => { view.current = { tx: 0, ty: 0, k: 1 }; setFocusTopic(null) }}>Show all</button>
          )}
        </div>
        {clusters.map((c) => (
          <button key={c.id} className={focusTopic === c.id ? 'on' : ''} onClick={() => zoomToTopic(c.id)}
            title={`${c.count} items${c.folder ? ` · mostly in ${c.folder}` : ''}`}>
            <i style={{ background: colorOf(c.id) }} />
            <span style={{ minWidth: 0 }}><b>{c.label}</b>{c.folder && <small>{c.folder}/</small>}</span>
            <span className="n">{c.count}</span>
          </button>
        ))}
      </div>
      {hover && (
        <div className="canvas-tip" style={{ left: hover.x + 14, top: hover.y + 10 }}>
          <span className="dot" style={{ background: colorOf(hover.p.c ?? 0), boxShadow: 'none' }} />
          <div>
            <strong>{truncate(hover.p.title, 80)}</strong>
            <div className="small muted">{[hover.p.kind, truncate(hover.p.subtitle || '', 60)].filter(Boolean).join(' · ')}</div>
            <div className="small faint">Topic: {clusters.find((c) => c.id === hover.p.c)?.label}</div>
          </div>
        </div>
      )}
    </div>
  )
}
