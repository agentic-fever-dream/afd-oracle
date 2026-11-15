import { useEffect, useRef, useState } from 'react'
import type { ExploreEdge, ExploreNode } from '../../api'
import { blockWheelScroll, colorOf, fitCanvas, GROUP_LABELS, LabelPlacer, relLabel, themeColors, truncate, zoomAt, type View } from './canvas'

type Sim = { x: number; y: number; vx: number; vy: number; pinned?: boolean }
/** Which expansion a node came from: its parent and the relationship (edge type + direction from the parent). */
export type Membership = { parent: string; type: string; dir: string }

type Props = {
  nodes: ExploreNode[]
  edges: ExploreEdge[]
  selected: string | null
  groups: Record<string, Membership>
  fitSignal: number
  onSelect: (uid: string | null) => void
  onExpand: (uid: string) => void
}

const isDead = (n: ExploreNode) => n.alive === false || n.alive === 'False'
const gkey = (m: Membership) => `${m.parent}|${m.type}|${m.dir}`

type Group = { key: string; parent: string; type: string; dir: string; members: string[]; angle: number; other: string }

/**
 * Force-directed graph where each expansion forms labelled groups: the neighbours a node was expanded into are
 * gathered by relationship ("Commits that changed it", "Called by", …) around their parent, drawn as a bubble with
 * a pill label, and joined to the parent by one bundled line instead of dozens of edges.
 */
export default function GraphCanvas({ nodes, edges, selected, groups, fitSignal, onSelect, onExpand }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const sim = useRef(new Map<string, Sim>())
  const view = useRef<View>({ tx: 0, ty: 0, k: 1 })
  const alpha = useRef(1)
  const pendingFit = useRef(true)
  const dirty = useRef(true)
  const hover = useRef<string | null>(null)
  const drag = useRef<{ uid: string | null; sx: number; sy: number; moved: boolean; vx0: number; vy0: number } | null>(null)
  const data = useRef({ nodes, edges, selected, groups, glist: [] as Group[] })
  const [tip, setTip] = useState<{ x: number; y: number; n: ExploreNode; g?: string } | null>(null)

  // Groups: members per (parent, relationship), with a fixed angular slot around the parent.
  const byNode = new Map(nodes.map((n) => [n.uid, n]))
  const gmap = new Map<string, Group>()
  for (const n of nodes) {
    const m = groups[n.uid]
    if (!m || !byNode.has(m.parent)) continue
    const k = gkey(m)
    if (!gmap.has(k)) gmap.set(k, { key: k, parent: m.parent, type: m.type, dir: m.dir, members: [], angle: 0, other: n.group })
    gmap.get(k)!.members.push(n.uid)
  }
  const glist = Array.from(gmap.values())
  const perParent = new Map<string, Group[]>()
  glist.forEach((g) => perParent.set(g.parent, [...(perParent.get(g.parent) || []), g]))
  for (const [parent, gs] of perParent) {
    gs.sort((a, b) => b.members.length - a.members.length)
    const up = groups[parent] // grow away from the parent's own parent
    const pp = up ? sim.current.get(up.parent) : undefined, me = sim.current.get(parent)
    const base = pp && me ? Math.atan2(me.y - pp.y, me.x - pp.x) : -Math.PI / 2
    const spread = pp ? Math.PI * 1.2 : Math.PI * 2
    gs.forEach((g, i) => {
      g.angle = pp ? base - spread / 2 + (spread * (i + 0.5)) / gs.length : base + (spread * i) / gs.length
    })
  }
  data.current = { nodes, edges, selected, groups, glist }

  useEffect(() => {
    const s = sim.current
    const live = new Set(nodes.map((n) => n.uid))
    for (const uid of Array.from(s.keys())) if (!live.has(uid)) s.delete(uid)
    nodes.forEach((n, i) => {
      if (s.has(n.uid)) return
      const m = groups[n.uid]
      const a = m ? s.get(m.parent) : undefined
      const g = m ? data.current.glist.find((x) => x.key === gkey(m)) : undefined
      const ang = g ? g.angle + (Math.random() - 0.5) * 0.6 : (i * 2.399963) % (2 * Math.PI)
      const r = a ? 120 + Math.random() * 40 : 60 + Math.sqrt(s.size) * 30
      s.set(n.uid, { x: (a?.x ?? 0) + Math.cos(ang) * r, y: (a?.y ?? 0) + Math.sin(ang) * r, vx: 0, vy: 0 })
    })
    alpha.current = Math.max(alpha.current, 0.9)
    pendingFit.current = true
    dirty.current = true
  }, [nodes, groups])

  useEffect(() => { dirty.current = true }, [selected, edges])
  useEffect(() => blockWheelScroll(canvas.current), [])

  /** The view that fits everything (with room for labels and group pills). ``ease`` < 1 moves part-way there. */
  function fitNow(ease = 1) {
    const c = canvas.current
    if (!c || !sim.current.size) return
    const pts = Array.from(sim.current.values())
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y)
    const [x0, x1, y0, y1] = [Math.min(...xs) - 40, Math.max(...xs) + 40, Math.min(...ys) - 50, Math.max(...ys) + 40]
    const k = Math.min(1.5, Math.max(0.15, Math.min((c.clientWidth - 220) / (x1 - x0), (c.clientHeight - 60) / (y1 - y0))))
    const target = { k, tx: c.clientWidth / 2 - ((x0 + x1) / 2) * k, ty: c.clientHeight / 2 - ((y0 + y1) / 2) * k }
    const v = view.current
    view.current = { k: v.k + (target.k - v.k) * ease, tx: v.tx + (target.tx - v.tx) * ease, ty: v.ty + (target.ty - v.ty) * ease }
    dirty.current = true
  }
  useEffect(() => { if (fitSignal) fitNow() }, [fitSignal])

  useEffect(() => {
    let raf = 0
    const c = canvas.current!
    view.current = { tx: c.clientWidth / 2, ty: c.clientHeight / 2, k: 1 }
    const ro = new ResizeObserver(() => { dirty.current = true })
    ro.observe(c)
    const loop = () => {
      if (alpha.current > 0.005) {
        // several ticks per frame while hot, so a fresh expansion settles in about a second
        const n = sim.current.size > 250 ? 1 : alpha.current > 0.3 ? 4 : 2
        for (let i = 0; i < n && alpha.current > 0.005; i++) step()
        dirty.current = true
      }
      // follow the layout while it settles, until the user takes over the camera
      if (pendingFit.current && !drag.current) { fitNow(alpha.current > 0.05 ? 0.12 : 1); if (alpha.current <= 0.05) pendingFit.current = false }
      if (dirty.current) { draw(); dirty.current = false }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => { cancelAnimationFrame(raf); ro.disconnect() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function step() {
    const s = sim.current
    const { edges, groups, glist } = data.current
    const pts = Array.from(s.entries())
    const a = alpha.current
    // repulsion (stronger between nodes of different groups so bubbles stay apart)
    for (let i = 0; i < pts.length; i++) {
      const [ui, p] = pts[i]
      const gi = groups[ui] ? gkey(groups[ui]) : ui
      for (let j = i + 1; j < pts.length; j++) {
        const [uj, q] = pts[j]
        let dx = p.x - q.x, dy = p.y - q.y
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.5 }
        if (d2 > 360000) continue
        const same = groups[uj] && gkey(groups[uj]) === gi
        const f = ((same ? 160 : 1300) * a) / d2
        p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f
      }
      p.vx -= p.x * 0.002 * a; p.vy -= p.y * 0.002 * a
    }
    // members gather at their group's slot around the parent
    for (const g of glist) {
      const par = s.get(g.parent)
      if (!par) continue
      const dist = 130 + 18 * Math.sqrt(g.members.length)
      const tx = par.x + Math.cos(g.angle) * dist, ty = par.y + Math.sin(g.angle) * dist
      for (const uid of g.members) {
        const p = s.get(uid)
        if (!p) continue
        p.vx += (tx - p.x) * 0.1 * a; p.vy += (ty - p.y) * 0.1 * a
      }
    }
    // springs only for edges not already explained by grouping
    for (const e of edges) {
      const ms = groups[e.target], mt = groups[e.source]
      if ((ms && ms.parent === e.source) || (mt && mt.parent === e.target)) continue
      const p = s.get(e.source), q = s.get(e.target)
      if (!p || !q) continue
      const dx = q.x - p.x, dy = q.y - p.y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      const f = ((d - 110) / d) * 0.02 * a
      p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f
    }
    for (const [, p] of pts) {
      if (p.pinned) { p.vx = p.vy = 0; continue }
      p.vx *= 0.55; p.vy *= 0.55
      p.x += Math.max(-40, Math.min(40, p.vx)); p.y += Math.max(-40, Math.min(40, p.vy))
    }
    alpha.current *= 0.982
  }

  function groupGeom(g: Group) {
    const s = sim.current
    const ps = g.members.map((u) => s.get(u)).filter(Boolean) as Sim[]
    const cx = ps.reduce((a, p) => a + p.x, 0) / ps.length, cy = ps.reduce((a, p) => a + p.y, 0) / ps.length
    const r = Math.max(...ps.map((p) => Math.hypot(p.x - cx, p.y - cy))) + 24
    return { cx, cy, r }
  }

  function draw() {
    const c = canvas.current
    if (!c) return
    const { w, h, dpr } = fitCanvas(c)
    const ctx = c.getContext('2d')!
    const t = themeColors()
    const { nodes, edges, selected, groups, glist } = data.current
    const v = view.current
    const s = sim.current
    const S = (x: number, y: number) => ({ x: x * v.k + v.tx, y: y * v.k + v.ty })
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)

    const focus = hover.current || selected
    const near = new Set<string>()
    if (focus) for (const e of edges) { if (e.source === focus) near.add(e.target); if (e.target === focus) near.add(e.source) }
    const geoms = new Map(glist.map((g) => [g.key, groupGeom(g)]))
    const parents = new Set(glist.map((g) => g.parent))

    // group bubbles + bundled link to parent
    for (const g of glist) {
      const gm = geoms.get(g.key)!, par = s.get(g.parent)
      if (!par) continue
      const col = colorOf(g.other)
      const ctr = S(gm.cx, gm.cy), pp = S(par.x, par.y)
      const active = focus === g.parent || g.members.includes(focus || '')
      ctx.globalAlpha = focus && !active ? 0.35 : 1
      ctx.strokeStyle = col + (active ? 'cc' : '66'); ctx.lineWidth = Math.min(6, 1.5 + Math.sqrt(g.members.length) * 0.8)
      const ang = Math.atan2(ctr.y - pp.y, ctr.x - pp.x)
      ctx.beginPath(); ctx.moveTo(pp.x, pp.y)
      ctx.lineTo(ctr.x - Math.cos(ang) * gm.r * v.k, ctr.y - Math.sin(ang) * gm.r * v.k); ctx.stroke()
      if (g.members.length > 1) {
        ctx.beginPath(); ctx.arc(ctr.x, ctr.y, gm.r * v.k, 0, Math.PI * 2)
        ctx.fillStyle = col + '12'; ctx.fill()
        ctx.lineWidth = 1; ctx.strokeStyle = col + (active ? '99' : '44'); ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([])
      }
    }
    ctx.globalAlpha = 1

    // other edges
    for (const e of edges) {
      const ms = groups[e.target], mt = groups[e.source]
      const bundled = (ms && ms.parent === e.source) || (mt && mt.parent === e.target)
      const on = focus && (e.source === focus || e.target === focus)
      const memberFocus = bundled && ((ms && ms.parent === e.source && e.target === focus) || (mt && mt.parent === e.target && e.source === focus))
      if (bundled && !memberFocus) continue
      const p = s.get(e.source), q = s.get(e.target)
      if (!p || !q) continue
      const a = S(p.x, p.y), b = S(q.x, q.y)
      ctx.strokeStyle = on ? t.accent : t.line
      ctx.globalAlpha = focus && !on ? 0.3 : on ? 0.9 : 0.6
      ctx.lineWidth = on ? 1.5 : 1
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
      const ang = Math.atan2(b.y - a.y, b.x - a.x)
      const ax = b.x - Math.cos(ang) * 10, ay = b.y - Math.sin(ang) * 10
      ctx.fillStyle = ctx.strokeStyle
      ctx.beginPath(); ctx.moveTo(ax, ay)
      ctx.lineTo(ax - Math.cos(ang - 0.4) * 6, ay - Math.sin(ang - 0.4) * 6)
      ctx.lineTo(ax - Math.cos(ang + 0.4) * 6, ay - Math.sin(ang + 0.4) * 6); ctx.fill()
    }
    ctx.globalAlpha = 1

    // nodes
    const deg = new Map<string, number>()
    edges.forEach((e) => { deg.set(e.source, (deg.get(e.source) || 0) + 1); deg.set(e.target, (deg.get(e.target) || 0) + 1) })
    const zs = Math.min(1.3, Math.max(0.75, Math.sqrt(v.k)))
    for (const n of nodes) {
      const p = s.get(n.uid)
      if (!p) continue
      const pt = S(p.x, p.y)
      const sel = n.uid === selected, hov = n.uid === hover.current
      const dim = focus && !sel && !hov && !near.has(n.uid) && n.uid !== focus
      const isParent = parents.has(n.uid)
      const r = (sel ? 9 : isParent ? 8.5 : n.group === 'Insight' ? 7.5 : 6) * zs
      ctx.globalAlpha = dim ? 0.3 : 1
      const col = colorOf(n.group)
      if (sel || isParent) {
        ctx.fillStyle = col; ctx.globalAlpha = sel ? 0.22 : 0.12
        ctx.beginPath(); ctx.arc(pt.x, pt.y, r + 8, 0, Math.PI * 2); ctx.fill()
        ctx.globalAlpha = dim ? 0.3 : 1
      }
      ctx.beginPath()
      if (n.kind === 'Class' || n.group === 'File') ctx.roundRect(pt.x - r, pt.y - r, r * 2, r * 2, 2)
      else if (n.group === 'Commit') { ctx.moveTo(pt.x, pt.y - r - 1); ctx.lineTo(pt.x + r + 1, pt.y); ctx.lineTo(pt.x, pt.y + r + 1); ctx.lineTo(pt.x - r - 1, pt.y); ctx.closePath() }
      else ctx.arc(pt.x, pt.y, r, 0, Math.PI * 2)
      if (isDead(n)) {
        ctx.fillStyle = t.surface; ctx.fill()
        ctx.setLineDash([2.5, 2]); ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.stroke(); ctx.setLineDash([])
      } else {
        ctx.fillStyle = col; ctx.fill()
        ctx.strokeStyle = t.surface; ctx.lineWidth = 1.5; ctx.stroke()
      }
    }
    ctx.globalAlpha = 1

    // labels: selected/hovered first, then group pills, then node names by importance, skipping overlaps
    const L = new LabelPlacer(ctx)
    for (const uid of [selected, hover.current]) {
      const n = uid ? nodes.find((x) => x.uid === uid) : undefined
      const p = uid ? s.get(uid) : undefined
      if (n && p) { const pt = S(p.x, p.y); L.text(truncate(n.title, 70), pt.x + 13, pt.y + 4, { font: '600 12.5px Geist, system-ui', color: t.fg, halo: t.surface, force: true }) }
    }
    for (const g of glist) {
      const gm = geoms.get(g.key)!
      const ctr = S(gm.cx, gm.cy)
      const thing = (GROUP_LABELS[g.other] || g.other).toLowerCase()
      const rel = relLabel(g.type, g.dir)
      const text = /commit|insight|author|folder|branch/i.test(rel) ? `${rel} · ${g.members.length}` : `${rel} · ${g.members.length} ${thing}${g.members.length > 1 ? 's' : ''}`
      L.pill(text, Math.min(w - 90, Math.max(90, ctr.x)), Math.min(h - 14, Math.max(14, ctr.y - gm.r * v.k - 4)), { font: '600 11.5px Geist, system-ui', color: t.fg, bg: t.surface, border: colorOf(g.other) + 'aa' })
    }
    const score = (n: ExploreNode) => (parents.has(n.uid) ? 1e5 : 0) + (near.has(n.uid) ? 5e4 : 0) + (deg.get(n.uid) || 0)
    for (const n of [...nodes].sort((a, b) => score(b) - score(a))) {
      if (n.uid === selected || n.uid === hover.current) continue
      const p = s.get(n.uid)
      if (!p) continue
      const pt = S(p.x, p.y)
      if (pt.x < -50 || pt.y < -20 || pt.x > w || pt.y > h + 20) continue
      const dim = focus && !near.has(n.uid) && n.uid !== focus
      const parent = parents.has(n.uid)
      L.text(truncate(n.title, parent ? 46 : 28), pt.x + 11, pt.y + 4, {
        font: `${parent ? 600 : 400} ${parent ? 12 : 11}px Geist, system-ui`, color: dim ? t.faint : parent ? t.fg : t.muted, halo: t.surface,
      })
    }
  }

  function toWorld(e: { clientX: number; clientY: number }) {
    const rect = canvas.current!.getBoundingClientRect()
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top
    const v = view.current
    return { sx, sy, x: (sx - v.tx) / v.k, y: (sy - v.ty) / v.k }
  }
  function pick(x: number, y: number): string | null {
    let best: string | null = null, bd = (14 / view.current.k) ** 2
    for (const [uid, p] of sim.current) {
      const d = (p.x - x) ** 2 + (p.y - y) ** 2
      if (d < bd) { bd = d; best = uid }
    }
    return best
  }

  return (
    <div className="canvas-wrap">
      <canvas
        ref={canvas}
        className="graph-canvas"
        onPointerDown={(e) => {
          const w = toWorld(e)
          drag.current = { uid: pick(w.x, w.y), sx: w.sx, sy: w.sy, moved: false, vx0: view.current.tx, vy0: view.current.ty }
          ;(e.target as HTMLCanvasElement).setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          const w = toWorld(e)
          const d = drag.current
          if (d) {
            if (Math.abs(w.sx - d.sx) + Math.abs(w.sy - d.sy) > 3) d.moved = true
            if (d.uid) {
              const p = sim.current.get(d.uid)
              if (p) { p.x = w.x; p.y = w.y; p.pinned = true; alpha.current = Math.max(alpha.current, 0.2) }
            } else { pendingFit.current = false; view.current = { ...view.current, tx: d.vx0 + (w.sx - d.sx), ty: d.vy0 + (w.sy - d.sy) } }
            dirty.current = true
            return
          }
          const uid = pick(w.x, w.y)
          if (uid !== hover.current) { hover.current = uid; dirty.current = true }
          const n = uid ? data.current.nodes.find((x) => x.uid === uid) : null
          const m = uid ? data.current.groups[uid] : undefined
          setTip(n ? { x: w.sx, y: w.sy, n, g: m ? relLabel(m.type, m.dir) : undefined } : null)
        }}
        onPointerUp={() => {
          const d = drag.current
          drag.current = null
          if (d && !d.moved) onSelect(d.uid)
        }}
        onPointerLeave={() => { hover.current = null; setTip(null); dirty.current = true }}
        onDoubleClick={(e) => {
          const w = toWorld(e)
          const uid = pick(w.x, w.y)
          if (uid) { const p = sim.current.get(uid); if (p) p.pinned = false; onExpand(uid) }
        }}
        onWheel={(e) => {
          const w = toWorld(e)
          pendingFit.current = false
          view.current = zoomAt(view.current, w.sx, w.sy, Math.exp(-e.deltaY * 0.0015))
          dirty.current = true
        }}
      />
      <div className="map-hint">Click to inspect · double-click to expand · drag to move · scroll to zoom</div>
      {tip && (
        <div className="canvas-tip" style={{ left: tip.x + 14, top: tip.y + 10 }}>
          <span className="dot" style={{ background: colorOf(tip.n.group), boxShadow: 'none' }} />
          <div>
            <strong>{truncate(tip.n.title, 80)}</strong>
            <div className="small muted">{tip.n.kind}{tip.n.subtitle ? ` · ${truncate(tip.n.subtitle, 60)}` : ''}</div>
            {tip.g && <div className="small faint">Group: {tip.g}</div>}
          </div>
        </div>
      )}
    </div>
  )
}
