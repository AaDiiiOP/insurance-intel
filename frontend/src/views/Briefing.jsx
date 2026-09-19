import { useCallback, useEffect, useRef, useState } from 'react'

import {
  createSummary,
  deleteCarrier,
  editSummary,
  getCarrier,
  getSummary,
  getSummaryProgress,
} from '../api'

const CATEGORY_LABELS = {
  fire: 'Fire',
  marine: 'Marine',
  liability: 'Liability',
  burglary: 'Burglary',
  'personal accident': 'Personal accident',
  motor: 'Motor',
  health: 'Health',
  life: 'Life',
  other: 'Other',
}

/** Section title / page / file, as a single readable citation line. */
function sourceLine(item) {
  const parts = []
  if (item.filename) parts.push(item.filename)
  if (item.section_title) parts.push(item.section_title)
  if (item.page_start) parts.push(`p.${item.page_start}`)
  return parts.join(' · ')
}

function Progress({ progress }) {
  const done = progress?.done ?? 0
  const total = progress?.total ?? 0
  const pct = total > 0 ? Math.round((done / total) * 100) : 0

  return (
    <div className="progress-wrap">
      <div className="progress-label">
        <span>{progress?.stage || 'Working'}…</span>
        <span>{total > 0 ? `${done} / ${total} sections` : 'starting'}</span>
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      <p className="progress-note">
        Each section is read separately by the local model, then combined into one
        briefing. On a first run this can take several minutes — leave the page open.
      </p>
    </div>
  )
}

export default function Briefing({ carrierId, onBack, onDeleted }) {
  const [carrier, setCarrier] = useState(null)
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const pollRef = useRef(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const data = await getCarrier(carrierId)
      setCarrier(data)
      if (data.has_summary) {
        setSummary(await getSummary(carrierId))
      } else {
        setSummary(null)
      }
    } catch (err) {
      setError(err.message)
    }
  }, [carrierId])

  useEffect(() => {
    load()
  }, [load])

  // Stop polling if the component unmounts mid-run, or the interval keeps
  // firing against a carrier that is no longer on screen.
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  async function handleGenerate() {
    setRunning(true)
    setError(null)
    setProgress({ stage: 'starting', done: 0, total: 0 })

    pollRef.current = setInterval(async () => {
      try {
        const p = await getSummaryProgress(carrierId)
        if (p?.state === 'running') setProgress(p)
      } catch {
        // A failed poll is not worth surfacing — the main request below
        // reports the real outcome either way.
      }
    }, 1500)

    try {
      const result = await createSummary(carrierId)
      setSummary(result)
      setCarrier((c) => (c ? { ...c, has_summary: true } : c))
    } catch (err) {
      setError(err.message)
    } finally {
      clearInterval(pollRef.current)
      pollRef.current = null
      setRunning(false)
      setProgress(null)
    }
  }

  async function handleSaveEdit() {
    try {
      const updated = await editSummary(carrierId, draft)
      setSummary(updated)
      setEditing(false)
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleDelete() {
    if (!window.confirm('Delete this carrier and everything read from it?')) return
    try {
      await deleteCarrier(carrierId)
      onDeleted?.()
    } catch (err) {
      setError(err.message)
    }
  }

  if (!carrier && !error) return <p className="loading">Loading…</p>

  const overview = summary?.company_overview || {}
  const overviewEntries = Object.entries(overview).filter(([, v]) => v)

  return (
    <>
      <button className="back-link" onClick={onBack}>
        ← All carriers
      </button>

      <header className="page-head">
        <div>
          <p className="eyebrow">Carrier briefing</p>
          <h1>
            {carrier?.name}
            <span className="stop">.</span>
          </h1>
          <p className="subtitle">
            {carrier?.documents?.length} document
            {carrier?.documents?.length === 1 ? '' : 's'} read
            {summary && summary.stats?.elapsed_seconds
              ? ` · briefing generated in ${summary.stats.elapsed_seconds}s using ${summary.model}`
              : ''}
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      {summary?.recognised === false && (
        <div className="banner bad">
          These documents do not look like insurance policy paperwork — almost
          no plans, requirements or exclusions could be found in them. Check you
          uploaded the right files. Whatever was found is below.
        </div>
      )}

      {summary?.index_error && (
        <div className="banner bad">
          The briefing saved, but it could not be added to the search index, so
          it will not appear in matching or Ask yet. {summary.index_error}
        </div>
      )}

      {summary?.indexed_chunks > 0 && (
        <div className="banner good">
          Briefing saved and {summary.indexed_chunks} section(s) added to the
          search index.
        </div>
      )}

      {/* Persistent version of the same warning: shown on every visit when a
          carrier has a briefing but nothing in the index, which is what an
          indexing failure looks like once the banner above has gone. */}
      {!summary?.index_error &&
        carrier?.has_summary &&
        carrier?.indexed_chunks === 0 && (
          <div className="banner bad">
            This carrier is briefed but none of it is in the search index, so
            it will be skipped by matching and Ask. Press Rebuild search index
            in Settings.
          </div>
        )}

      {/* ---- Documents ---- */}
      <div className="file-list">
        {carrier?.documents?.map((doc) => (
          <div
            key={doc.document_id}
            className={`file-row ${doc.status === 'failed' ? 'is-failed' : ''}`}
          >
            <span
              className="swatch"
              style={{
                background: doc.status === 'parsed' ? 'var(--ok)' : 'var(--bad)',
              }}
            />
            <div className="file-main">
              <div className="file-name">{doc.name}</div>
              <div className="file-meta">
                {doc.status === 'parsed' && (
                  <>
                    {doc.chars.toLocaleString()} characters
                    {doc.pages > 0 && ` · ${doc.pages} pages`}
                  </>
                )}
              </div>
              {doc.error && <div className="file-error">{doc.error}</div>}
            </div>
            <span className={`file-status ${doc.status === 'parsed' ? 'ok' : 'bad'}`}>
              {doc.status}
            </span>
          </div>
        ))}
      </div>

      {running && <Progress progress={progress} />}

      <div className="actions">
        <span className="hint">
          {!carrier?.can_summarise &&
            'No readable documents — nothing to brief on.'}
          {carrier?.can_summarise &&
            !summary &&
            'Reads every section, then writes one briefing.'}
          {summary &&
            summary.stats?.chunks_failed > 0 &&
            `${summary.stats.chunks_failed} section${
              summary.stats.chunks_failed === 1 ? '' : 's'
            } could not be read.`}
        </span>
        <button className="btn-danger" onClick={handleDelete} disabled={running}>
          Delete
        </button>
        <button
          className="btn-primary"
          onClick={handleGenerate}
          disabled={running || !carrier?.can_summarise}
        >
          {running
            ? 'Reading…'
            : summary
              ? 'Regenerate briefing'
              : 'Generate briefing'}
        </button>
      </div>

      {!summary && !running && carrier?.can_summarise && (
        <div className="setup-note">
          The briefing needs Ollama running on your host machine with{' '}
          <code>qwen2.5:7b</code> pulled. If the sidebar light is amber, check
          Settings first.
        </div>
      )}

      {/* ---- Summary ---- */}
      {summary && (
        <>
          {overviewEntries.length > 0 && (
            <section className="section">
              <div className="section-head">
                <h2>At a glance</h2>
              </div>
              <div className="facts">
                {overviewEntries.map(([label, value]) => (
                  <div className="fact" key={label}>
                    <div className="fact-label">{label.replace(/_/g, ' ')}</div>
                    <div className="fact-value">{value}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="section">
            <div className="section-head">
              <h2>Overview</h2>
              {!editing && (
                <button
                  className="back-link"
                  style={{ margin: 0 }}
                  onClick={() => {
                    setDraft(summary.summary)
                    setEditing(true)
                  }}
                >
                  Edit
                </button>
              )}
            </div>

            {editing ? (
              <>
                <textarea
                  className="edit-area"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                />
                <div className="actions">
                  <span className="hint">
                    Editing the prose only — the lists below stay as the model
                    extracted them.
                  </span>
                  <button className="btn-ghost" onClick={() => setEditing(false)}>
                    Cancel
                  </button>
                  <button className="btn-primary" onClick={handleSaveEdit}>
                    Save
                  </button>
                </div>
              </>
            ) : (
              <p className="prose">{summary.summary}</p>
            )}
            {summary.edited_at && !editing && (
              <p className="progress-note">Edited by hand after generation.</p>
            )}
          </section>

          {summary.key_points?.length > 0 && (
            <section className="section">
              <div className="section-head">
                <h2>Key facts</h2>
                <span className="section-count">{summary.key_points.length}</span>
              </div>
              <div className="facts">
                {summary.key_points.map((point, i) => (
                  <div className="fact" key={`${point.label}-${i}`}>
                    <div className="fact-label">{point.label}</div>
                    <div className="fact-value">{point.value}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {summary.plans?.length > 0 && (
            <section className="section">
              <div className="section-head">
                <h2>Plans &amp; covers</h2>
                <span className="section-count">{summary.plans.length}</span>
              </div>
              {summary.plans.map((plan, i) => (
                <div className="plan" key={`${plan.name}-${i}`}>
                  <div className="plan-head">
                    <span className="plan-name">{plan.name}</span>
                    <span className="pill gold">
                      {CATEGORY_LABELS[plan.category] || plan.category}
                    </span>
                  </div>
                  {plan.detail && <div className="plan-detail">{plan.detail}</div>}
                  {plan.exclusions?.length > 0 && (
                    <div className="plan-exclusions">
                      <div className="plan-exclusions-label">Not covered</div>
                      <ul>
                        {plan.exclusions.map((ex, j) => (
                          <li key={j}>{ex}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ))}
            </section>
          )}

          {summary.requirements?.length > 0 && (
            <section className="section">
              <div className="section-head">
                <h2>What the buyer must provide</h2>
                <span className="section-count">{summary.requirements.length}</span>
              </div>
              <div className="listing">
                {summary.requirements.map((req, i) => (
                  <div className="listing-item" key={i}>
                    <div className="listing-text">{req.text}</div>
                    {sourceLine(req) && (
                      <div className="listing-source">{sourceLine(req)}</div>
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {summary.exclusions?.length > 0 && (
            <section className="section">
              <div className="section-head">
                <h2>Exclusions found</h2>
                <span className="section-count">{summary.exclusions.length}</span>
              </div>
              <div className="listing">
                {summary.exclusions.map((ex, i) => (
                  <div className="listing-item" key={i}>
                    <div className="listing-text">{ex.text}</div>
                    {sourceLine(ex) && (
                      <div className="listing-source">{sourceLine(ex)}</div>
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </>
  )
}
