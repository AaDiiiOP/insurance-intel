import { useEffect, useState } from 'react'

import { listCarriers } from '../api'

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    })
  } catch {
    return ''
  }
}

export default function Carriers({ refreshKey, onAddCarrier, onOpen }) {
  const [carriers, setCarriers] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setError(null)

    listCarriers()
      .then((data) => {
        if (!cancelled) setCarriers(data.carriers)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })

    return () => {
      cancelled = true
    }
  }, [refreshKey])

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Library</p>
          <h1>
            Carriers<span className="stop">.</span>
          </h1>
          <p className="subtitle">
            Every insurer whose documents have been read into the system. Open one
            to see its briefing — what it sells, what it requires, what it excludes.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      {carriers === null && !error && <p className="loading">Loading…</p>}

      {carriers?.length === 0 && (
        <div className="empty">
          <p>No carriers yet.</p>
          <button className="btn-primary" onClick={onAddCarrier}>
            Add the first one
          </button>
        </div>
      )}

      {carriers?.length > 0 && (
        <div className="card-grid">
          {carriers.map((carrier) => (
            <button
              key={carrier.carrier_id}
              className="card"
              onClick={() => onOpen(carrier.carrier_id)}
            >
              <h3 className="card-title">{carrier.name}</h3>
              <div className="card-meta">
                <span>
                  {carrier.document_count} document
                  {carrier.document_count === 1 ? '' : 's'}
                </span>
                <span>·</span>
                <span>{formatDate(carrier.created_at)}</span>
              </div>
              <div className="card-meta" style={{ marginTop: 10 }}>
                {carrier.has_summary ? (
                  <span className="pill gold">Briefed</span>
                ) : (
                  <span className="pill">Not briefed</span>
                )}
                {carrier.failed_count > 0 && (
                  <span className="pill bad">{carrier.failed_count} failed</span>
                )}
              </div>
            </button>
          ))}
        </div>
      )}
    </>
  )
}
