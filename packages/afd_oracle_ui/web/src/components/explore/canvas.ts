/** Shared canvas helpers: colours per node group, pan/zoom transform, DPR-aware sizing. */

export const GROUP_COLORS: Record<string, string> = {
  Repository: '#8f8cff', Directory: '#94a3b8', File: '#4fa3e0', Symbol: '#4cc38a', Commit: '#e6a23c',
  Author: '#e879a6', Branch: '#a78bfa', Insight: '#f2705e', Vulnerability: '#ef4444', Node: '#94a3b8',
}
export const GROUP_LABELS: Record<string, string> = {
  Symbol: 'Code symbol', File: 'File', Directory: 'Folder', Commit: 'Commit', Author: 'Author', Branch: 'Branch',
  Insight: 'Insight', Repository: 'Repository', Vulnerability: 'Vulnerability',
}
export const colorOf = (group: string) => GROUP_COLORS[group] || GROUP_COLORS.Node

export type View = { tx: number; ty: number; k: number }

export function themeColors() {
  const cs = getComputedStyle(document.documentElement)
  const v = (n: string) => cs.getPropertyValue(n).trim()
  return { fg: v('--fg'), muted: v('--muted'), faint: v('--faint'), line: v('--line-strong'), surface: v('--surface'),
    accent: v('--accent'), bg: v('--sunken') }
}

/** Keep a canvas's backing store in sync with its CSS size and the device pixel ratio. */
export function fitCanvas(c: HTMLCanvasElement): { w: number; h: number; dpr: number } {
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = c.clientWidth, h = c.clientHeight
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr)
    c.height = Math.round(h * dpr)
  }
  return { w, h, dpr }
}

/** Zoom around the cursor. */
export function zoomAt(view: View, sx: number, sy: number, factor: number): View {
  const k = Math.min(6, Math.max(0.15, view.k * factor))
  const f = k / view.k
  return { k, tx: sx - (sx - view.tx) * f, ty: sy - (sy - view.ty) * f }
}

export function truncate(s: string, n: number) {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

/** React wheel listeners are passive, so zooming would also scroll the page; block that natively. */
export function blockWheelScroll(el: HTMLElement | null): () => void {
  if (!el) return () => {}
  const stop = (e: WheelEvent) => e.preventDefault()
  el.addEventListener('wheel', stop, { passive: false })
  return () => el.removeEventListener('wheel', stop)
}

/** Plain-language name for "the nodes connected to X by this edge". ``dir`` is relative to X. */
const REL: Record<string, [string, string]> = {
  TOUCHED: ['Changed', 'Commits that changed it'],
  DEFINES: ['Defines', 'Defined in'],
  CALLS: ['Calls', 'Called by'],
  IMPORTS: ['Imports', 'Imported by'],
  INHERITS: ['Inherits from', 'Subclasses'],
  CO_COMMITTED: ['Changes together with', 'Changes together with'],
  CONCERNS: ['About', 'Insights about it'],
  AFFECTS: ['Affects', 'Vulnerabilities in it'],
  IN_SYMBOL: ['In function', 'Vulnerabilities in it'],
  EVIDENCED_BY: ['Evidence', 'Cited as evidence by'],
  CONTAINS: ['Contains', 'In folder'],
  AUTHORED: ['Authored', 'Author'],
  PARENT: ['Parent commit', 'Child commits'],
  REVERTS: ['Reverts', 'Reverted by'],
  UNIQUE_TO: ['Only on branch', 'Branch-only commits'],
  POINTS_TO: ['Branch tip', 'Branch tips here'],
}
export function relLabel(type: string, dir: string): string {
  const r = REL[type]
  if (r) return dir === 'out' ? r[0] : r[1]
  const t = type.toLowerCase().replace(/_/g, ' ')
  return dir === 'out' ? t : `${t} (incoming)`
}

/** Greedy label placement: draws each label only if its box doesn't overlap one already drawn. */
export class LabelPlacer {
  private boxes: [number, number, number, number][] = []
  constructor(private ctx: CanvasRenderingContext2D) {}
  fits(x: number, y: number, w: number, h: number) {
    return !this.boxes.some(([a, b, c, d]) => x < c && x + w > a && y < d && y + h > b)
  }
  reserve(x: number, y: number, w: number, h: number) { this.boxes.push([x, y, x + w, y + h]) }
  /** Text with a halo; returns false when skipped for overlap. */
  text(s: string, x: number, y: number, o: { font: string; color: string; halo: string; force?: boolean }) {
    const ctx = this.ctx
    ctx.font = o.font
    const w = ctx.measureText(s).width, h = 14
    if (!o.force && !this.fits(x - 2, y - 11, w + 4, h)) return false
    this.reserve(x - 2, y - 11, w + 4, h)
    ctx.lineWidth = 3.5; ctx.strokeStyle = o.halo; ctx.lineJoin = 'round'
    ctx.strokeText(s, x, y)
    ctx.fillStyle = o.color
    ctx.fillText(s, x, y)
    return true
  }
  /** A rounded pill with text, centred on x. */
  pill(s: string, x: number, y: number, o: { font: string; color: string; bg: string; border: string; force?: boolean }) {
    const ctx = this.ctx
    ctx.font = o.font
    const w = ctx.measureText(s).width + 16, h = 20
    const left = x - w / 2, top = y - h / 2
    if (!o.force && !this.fits(left, top, w, h)) return false
    this.reserve(left, top, w, h)
    ctx.beginPath()
    ctx.roundRect(left, top, w, h, 10)
    ctx.fillStyle = o.bg; ctx.fill()
    ctx.lineWidth = 1; ctx.strokeStyle = o.border; ctx.stroke()
    ctx.fillStyle = o.color
    ctx.textAlign = 'center'
    ctx.fillText(s, x, y + 4)
    ctx.textAlign = 'left'
    return true
  }
}
