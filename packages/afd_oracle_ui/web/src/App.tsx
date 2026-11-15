import { useState } from 'react'
import { Link, NavLink, Route, Routes } from 'react-router-dom'
import Home from './pages/Home'
import RepoPage from './pages/RepoPage'
import Activity from './pages/Activity'
import Benchmark from './pages/Benchmark'

type Theme = 'system' | 'light' | 'dark'
const NEXT: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' }

function readTheme(): Theme {
  try {
    const t = localStorage.getItem('oracle-theme')
    return t === 'light' || t === 'dark' ? t : 'system'
  } catch {
    return 'system'
  }
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  function cycle() {
    const t = NEXT[theme]
    setTheme(t)
    if (t === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = t
    try {
      if (t === 'system') localStorage.removeItem('oracle-theme')
      else localStorage.setItem('oracle-theme', t)
    } catch { /* storage unavailable: the choice lasts for this page only */ }
  }
  const label = `Theme: ${theme}. Switch to ${NEXT[theme]}.`
  return (
    <button className="icon-btn" onClick={cycle} title={label} aria-label={label}>
      {theme === 'light' ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
      ) : theme === 'dark' ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="8.5" /><path d="M12 3.5v17a8.5 8.5 0 0 0 0-17z" fill="currentColor" /></svg>
      )}
    </button>
  )
}

/** The Oracle eye: an almond outline with a glowing pupil. */
export function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <linearGradient id="oracle-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" style={{ stopColor: 'var(--accent)' }} />
          <stop offset="1" style={{ stopColor: 'var(--gold)' }} />
        </linearGradient>
      </defs>
      <path d="M3 16c3.4-6.2 7.7-9 13-9s9.6 2.8 13 9c-3.4 6.2-7.7 9-13 9s-9.6-2.8-13-9z" fill="none"
        stroke="url(#oracle-g)" strokeWidth="2.2" strokeLinejoin="round" />
      <circle cx="16" cy="16" r="4.2" fill="url(#oracle-g)" />
      <circle cx="17.4" cy="14.6" r="1.2" style={{ fill: 'var(--surface)' }} />
    </svg>
  )
}

export default function App() {
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand" aria-label="Oracle home">
            <BrandMark />
            <span className="brand-word">Oracle</span>
          </Link>
          <nav aria-label="Main">
            <NavLink to="/" end>Repositories</NavLink>
            <NavLink to="/activity">Activity</NavLink>
            <NavLink to="/benchmark">Benchmark</NavLink>
          </nav>
          <span className="spacer" />
          <a className="icon-btn" href="/api/docs" target="_blank" rel="noreferrer" title="API reference" aria-label="API reference">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M8 7l-5 5 5 5M16 7l5 5-5 5M13.5 4l-3 16" /></svg>
          </a>
          <ThemeToggle />
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/benchmark" element={<Benchmark />} />
          <Route path="/repos/:id/*" element={<RepoPage />} />
          <Route path="*" element={<p className="empty">Page not found. <Link to="/">Back to repositories</Link></p>} />
        </Routes>
      </main>
    </>
  )
}
