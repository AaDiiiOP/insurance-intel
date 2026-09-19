import { useCallback, useEffect, useState } from 'react'

import { getHealth, reindex } from '../api'

const LABELS = {
  api: 'Backend API',
  postgres: 'Postgres',
  chromadb: 'ChromaDB',
  ollama: 'Ollama (local model)',
}

const NOTES = {
  chromadb: 'Not used until Phase 2 — running now so it is ready.',
  ollama: 'Runs on your host machine, not in Docker.',
}

export default function Health() {
  const [health, setHealth] = useState(null)
  const [error, setError] = useState(null)
  const [checking, setChecking] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)
  const [rebuilt, setRebuilt] = useState(null)

  const check = useCallback(async () => {
    setChecking(true)
    setError(null)
    try {
      setHealth(await getHealth())
    } catch (err) {
      setError(err.message)
      setHealth(null)
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    check()
  }, [check])

  async function rebuild() {
    setRebuilding(true)
    setError(null)
    setRebuilt(null)
    try {
      const result = await reindex()
      setRebuilt(result)
      // The chunk count is part of the health detail, so refresh it rather
      // than leaving a stale "nothing indexed" beside a successful rebuild.
      await check()
    } catch (e) {
      setError(e.message)
    } finally {
      setRebuilding(false)
    }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Settings</p>
          <h1>
            Services<span className="stop">.</span>
          </h1>
          <p className="subtitle">
            Every dependency, probed for real rather than assumed. If a briefing
            fails, this page usually says why.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      {health && (
        <div
          className={`banner ${health.status === 'ok' ? 'good' : 'bad'}`}
        >
          {health.status === 'ok'
            ? 'Everything is running.'
            : 'Something is down — see the detail below.'}
        </div>
      )}

      {health && (
        <table className="health-table">
          <thead>
            <tr>
              <th>Service</th>
              <th>Status</th>
              <th>Detail</th>
              <th>Latency</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(health.services).map(([key, service]) => (
              <tr key={key}>
                <td>
                  {LABELS[key] || key}
                  {NOTES[key] && (
                    <div className="file-meta" style={{ marginTop: 3 }}>
                      {NOTES[key]}
                    </div>
                  )}
                </td>
                <td>
                  <span
                    className={`pill ${
                      service.status === 'connected' ? 'ok' : 'bad'
                    }`}
                  >
                    {service.status}
                  </span>
                </td>
                <td className="health-detail">{service.detail}</td>
                <td className="health-detail">
                  {service.latency_ms == null ? '—' : `${service.latency_ms} ms`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="actions">
        <span className="hint">
          Rebuilding re-embeds every briefed document from Postgres. No
          summarising is repeated, so it is cheap — and it is how the search
          index is restored if the ChromaDB volume is ever wiped.
        </span>
        <button className="btn-ghost" onClick={check} disabled={checking}>
          {checking ? 'Checking…' : 'Re-check'}
        </button>
        <button className="btn-ghost" onClick={rebuild} disabled={rebuilding}>
          {rebuilding ? 'Rebuilding…' : 'Rebuild search index'}
        </button>
      </div>

      {rebuilt && (
        <div className={`banner ${rebuilt.failed?.length ? 'bad' : 'good'}`}>
          Indexed {rebuilt.chunks} chunk(s) from {rebuilt.documents} document(s).
          {rebuilt.detail ? ` ${rebuilt.detail}` : ''}
          {rebuilt.failed?.length > 0 && (
            <div style={{ marginTop: 6 }}>
              {rebuilt.failed.map((f, i) => (
                <div key={i} style={{ fontSize: 13 }}>
                  {f.filename}: {f.error}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="setup-note">
        Ollama cannot run inside Docker here — it needs direct access to your
        GPU. Install it on your machine, then run{' '}
        <code>ollama pull qwen2.5:7b</code> and{' '}
        <code>ollama pull nomic-embed-text</code> once. Everything else starts
        with <code>docker-compose up --build</code>.
      </div>
    </>
  )
}
