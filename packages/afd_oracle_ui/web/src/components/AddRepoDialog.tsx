import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'

const guessId = (src: string) =>
  src.trim().replace(/[\\/]+$/, '').split(/[\\/:]/).pop()?.replace(/\.git$/, '').replace(/[^A-Za-z0-9._-]/g, '-') || ''

export default function AddRepoDialog({ onClose }: { onClose: () => void }) {
  const nav = useNavigate()
  const [source, setSource] = useState('')
  const [id, setId] = useState('')
  const [idTouched, setIdTouched] = useState(false)
  const [prs, setPrs] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const isUrl = /^(https?:\/\/|ssh:\/\/|git@|git:\/\/)/.test(source.trim())

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const job = await api.addRepo(source.trim(), (idTouched ? id : guessId(source)) || null, prs)
      onClose()
      nav(`/repos/${encodeURIComponent(job.repo!)}`)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-title" onSubmit={submit}>
        <h2 id="add-title">Add a repository</h2>
        <p className="muted small">
          Paste a git URL to clone it, or the path of a folder on this computer. Ingestion is automatic and uses no
          AI tokens. Mining for insights is a separate, optional step.
        </p>
        <label className="field">
          Git URL or local folder
          <input id="add-source" className="input" autoFocus required value={source}
            placeholder="https://github.com/org/repo.git  or  C:\code\my-repo"
            onChange={(e) => setSource(e.target.value)} />
        </label>
        <label className="field">
          Name in the knowledge base
          <input id="add-id" className="input mono" value={idTouched ? id : guessId(source)}
            onChange={(e) => { setId(e.target.value); setIdTouched(true) }} pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" />
        </label>
        {isUrl && (
          <label className="check">
            <input id="add-prs" type="checkbox" checked={prs} onChange={(e) => setPrs(e.target.checked)} />
            Include unmerged pull requests (finds abandoned attempts; slower first clone)
          </label>
        )}
        {err && <div className="error">{err}</div>}
        <div className="row">
          <span className="spacer" />
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={busy || !source.trim()}>
            {busy ? 'Starting…' : 'Add and ingest'}
          </button>
        </div>
      </form>
    </>
  )
}
