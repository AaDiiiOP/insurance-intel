import { useEffect, useState } from 'react'

import {
  getDocumentText,
  getEntityKnowledge,
  listKnowledgeEntities,
  listPossibleDuplicates,
  reviewKnowledgeFact,
} from '../api'

/**
 * Knowledge explorer (v0.9.0).
 *
 * Entity -> facts -> relationships -> evidence -> source document/page, read
 * from GET /knowledge/entities/{id}/knowledge. Every fact carries one label
 * (Current / Historical / Needs review / Verified / Contradiction /
 * Superseded), its stated policy period, and its evidence; "View page" shows
 * the actual page text the evidence points to with the quote highlighted.
 * Verify / Reject go through POST /knowledge/facts/{id}/review, which keeps
 * every rule of the backend (no verify without evidence, contradictions are
 * settled by correction, rejected facts are kept for audit).
 */

const ENTITY_TYPES = ['', 'business', 'asset', 'policy', 'location', 'coverage', 'carrier']

function factLabel(fact, historical) {
  if (fact.status === 'superseded') return { text: 'Superseded', tone: '' }
  if (fact.status === 'contradiction_flagged') return { text: 'Contradiction', tone: 'bad' }
  if (historical) return { text: 'Historical', tone: '' }
  if (fact.status === 'verified' || fact.status === 'corrected') return { text: 'Verified', tone: 'ok' }
  return { text: 'Needs review', tone: 'gold' }
}

function period(fact) {
  if (!fact.effective_from && !fact.effective_to) return 'no stated period'
  return `${fact.effective_from || '?'} → ${fact.effective_to || '?'}`
}

function pageText(fullText, page) {
  if (!page) return fullText
  const parts = fullText.split(/\[Page (\d+)\]\n?/)
  for (let i = 1; i < parts.length; i += 2) {
    if (Number(parts[i]) === Number(page)) return parts[i + 1] || ''
  }
  return fullText
}

function Highlighted({ text, quote }) {
  if (!quote) return <pre className="kx-page">{text}</pre>
  const norm = (s) => s.replace(/\s+/g, ' ').toLowerCase()
  const lines = text.split('\n')
  const q = norm(quote)
  return (
    <pre className="kx-page">
      {lines.map((line, i) => {
        const hit = line.trim() && (q.includes(norm(line).trim()) || norm(line).includes(q))
        return (
          <span key={i} className={hit ? 'kx-hit' : undefined}>
            {line}
            {'\n'}
          </span>
        )
      })}
    </pre>
  )
}

function EvidenceRow({ ev, onError }) {
  const [page, setPage] = useState(null)
  const [loading, setLoading] = useState(false)

  async function toggle() {
    if (page !== null) {
      setPage(null)
      return
    }
    setLoading(true)
    try {
      const doc = await getDocumentText(ev.document_id)
      setPage(pageText(doc.text || '', ev.page))
    } catch (e) {
      onError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="evidence-block">
      <div className="evidence-source">
        {[ev.document_filename, ev.page ? `Page ${ev.page}` : null, ev.section,
          ev.source === 'ocr' ? 'read via OCR' : 'text layer']
          .filter(Boolean)
          .join(' · ')}
        <button className="btn-ghost kx-small" onClick={toggle} disabled={loading}>
          {loading ? 'Loading…' : page !== null ? 'Hide page' : 'View page'}
        </button>
      </div>
      {ev.snippet && <blockquote className="evidence-snippet">"{ev.snippet}"</blockquote>}
      {page !== null && <Highlighted text={page} quote={ev.snippet} />}
    </div>
  )
}

function FactCard({ fact, historical, reviewer, onReviewed, onError }) {
  const label = factLabel(fact, historical)
  const [busy, setBusy] = useState(false)
  const canReview = !historical && fact.status === 'needs_review'

  async function review(action) {
    if (!reviewer.trim()) {
      onError('Enter your name (top of the explorer) to record a review.')
      return
    }
    setBusy(true)
    try {
      await reviewKnowledgeFact(fact.id, action, reviewer.trim())
      onReviewed()
    } catch (e) {
      onError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`kx-fact ${fact.status === 'contradiction_flagged' ? 'is-contradiction' : ''}`}>
      <div className="kx-fact-head">
        <span className="kx-value">{fact.value}{fact.unit ? ` ${fact.unit}` : ''}</span>
        <span className={`pill ${label.tone}`}>{label.text}</span>
      </div>
      <div className="kx-meta">
        Period: {period(fact)}
        {fact.reviewed_by ? ` · reviewed by ${fact.reviewed_by}` : ''}
        {historical && fact.valid_to ? ` · closed ${String(fact.valid_to).slice(0, 10)}` : ''}
      </div>
      {(fact.evidence || []).map((ev) => (
        <EvidenceRow key={ev.id} ev={ev} onError={onError} />
      ))}
      {(fact.evidence || []).length === 0 && <div className="kx-meta">No evidence recorded.</div>}
      {canReview && (
        <div className="kx-actions">
          <button className="btn-ghost kx-small" disabled={busy} onClick={() => review('verify')}>
            ✓ Verify
          </button>
          <button className="btn-ghost kx-small" disabled={busy} onClick={() => review('reject')}>
            ✕ Reject
          </button>
        </div>
      )}
      {fact.status === 'contradiction_flagged' && (
        <div className="kx-meta">Contradictions are settled with “Correct” on the analysis view, not verified.</div>
      )}
    </div>
  )
}

function groupByType(facts) {
  const out = {}
  for (const f of facts) (out[f.fact_type] ||= []).push(f)
  return Object.entries(out).sort(([a], [b]) => a.localeCompare(b))
}

export default function KnowledgeExplorer({ initialEntityId }) {
  const [entityType, setEntityType] = useState('')
  const [entities, setEntities] = useState([])
  const [entityId, setEntityId] = useState(initialEntityId || '')
  const [asOf, setAsOf] = useState('')
  const [data, setData] = useState(null)
  const [dupes, setDupes] = useState([])
  const [reviewer, setReviewer] = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  async function loadEntities() {
    setError(null)
    try {
      const [list, d] = await Promise.all([
        listKnowledgeEntities(entityType || undefined),
        listPossibleDuplicates(entityType || undefined),
      ])
      setEntities(list.entities || [])
      setDupes(d.pairs || [])
    } catch (e) {
      setError(e.message)
    }
  }

  async function loadEntity(id = entityId, date = asOf) {
    if (!id) return
    setLoading(true)
    setError(null)
    try {
      setData(await getEntityKnowledge(id, date || undefined))
    } catch (e) {
      setError(e.message)
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadEntities()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType])

  useEffect(() => {
    if (initialEntityId) {
      setEntityId(initialEntityId)
      loadEntity(initialEntityId, asOf)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialEntityId])

  function open(id) {
    setEntityId(id)
    loadEntity(id, asOf)
  }

  const aliases = data?.entity?.attributes?.aliases || []
  const conflictTypes = new Set((data?.unresolved_conflicts || []).map((c) => c.fact_type))

  return (
    <div className="section">
      <div className="section-head">
        <h2>Knowledge explorer</h2>
      </div>
      {error && <div className="banner bad">{error}</div>}

      <div className="scope-row kx-controls">
        <label htmlFor="kx-type">Type</label>
        <select id="kx-type" value={entityType} onChange={(e) => setEntityType(e.target.value)}>
          {ENTITY_TYPES.map((t) => (
            <option key={t} value={t}>{t || 'all'}</option>
          ))}
        </select>
        <label htmlFor="kx-entity">Entity</label>
        <select id="kx-entity" value={entityId} onChange={(e) => open(e.target.value)}>
          <option value="">choose an entity</option>
          {entities.map((e) => (
            <option key={e.entity_id} value={e.entity_id}>
              {e.entity_type}: {e.name}
            </option>
          ))}
        </select>
        <button className="btn-ghost" onClick={loadEntities}>Refresh</button>
      </div>
      <div className="scope-row kx-controls">
        <label htmlFor="kx-asof">In force on</label>
        <input id="kx-asof" type="date" value={asOf}
          onChange={(e) => { setAsOf(e.target.value); loadEntity(entityId, e.target.value) }} />
        {asOf && <button className="btn-ghost kx-small" onClick={() => { setAsOf(''); loadEntity(entityId, '') }}>Clear date</button>}
        <label htmlFor="kx-reviewer">Your name</label>
        <input id="kx-reviewer" className="approve-name" placeholder="for Verify / Reject"
          value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
      </div>

      {dupes.length > 0 && (
        <details className="banner info kx-dupes">
          <summary>{dupes.length} possible duplicate name(s) to review — not merged</summary>
          <ul>
            {dupes.slice(0, 50).map((p) => (
              <li key={`${p.a}-${p.b}`}>
                <button className="kx-link" onClick={() => open(p.a)}>{p.a_name}</button>
                {' ↔ '}
                <button className="kx-link" onClick={() => open(p.b)}>{p.b_name}</button>
              </li>
            ))}
          </ul>
        </details>
      )}

      {loading && <p className="subtitle">Loading…</p>}

      {data && !loading && (
        <>
          <div className="kx-entity">
            <div className="kx-entity-name">{data.entity.name}</div>
            <div className="kx-meta">
              {data.entity.entity_type}
              {aliases.length > 0 ? ` · also known as: ${aliases.join(', ')}` : ''}
            </div>
          </div>

          <div className="kx-rels">
            {data.relationships.outgoing.map((r) => (
              <button key={r.relationship_id} className="kx-rel" onClick={() => open(r.entity_id)}>
                {r.type} → {r.entity_type}: {r.name}
              </button>
            ))}
            {data.relationships.incoming.map((r) => (
              <button key={r.relationship_id} className="kx-rel" onClick={() => open(r.entity_id)}>
                ← {r.type} · {r.entity_type}: {r.name}
              </button>
            ))}
            {data.relationships.outgoing.length + data.relationships.incoming.length === 0 && (
              <span className="kx-meta">No relationships.</span>
            )}
          </div>

          {data.unresolved_conflicts.length > 0 && (
            <div className="banner bad">
              Unresolved conflicts: {data.unresolved_conflicts.map((c) => c.fact_type).join(', ')} —
              no current value is given for these until a person corrects one side.
            </div>
          )}

          {asOf && data.in_force && (
            <div className="kx-block">
              <h3>In force on {data.in_force.as_of}</h3>
              {data.in_force.in_force.length === 0 && <p className="kx-meta">No dated fact covers this date.</p>}
              {data.in_force.in_force.map((f) => (
                <div key={f.id} className="kx-meta">{f.fact_type}: <b>{f.value}</b> ({period(f)})</div>
              ))}
              {data.in_force.undated_current.length > 0 && (
                <p className="kx-meta">
                  {data.in_force.undated_current.length} current fact(s) have no stated period and are not assumed in force.
                </p>
              )}
            </div>
          )}

          <div className="kx-block">
            <h3>Current facts ({data.current_facts.length})</h3>
            {data.current_facts.length === 0 && <p className="kx-meta">No current facts.</p>}
            {groupByType(data.current_facts).map(([type, facts]) => (
              <div key={type} className="kx-type">
                <div className="kx-type-name">
                  {type}
                  {conflictTypes.has(type) && <span className="pill bad">conflict</span>}
                </div>
                {facts.map((f) => (
                  <FactCard key={f.id} fact={f} historical={false} reviewer={reviewer}
                    onReviewed={() => loadEntity()} onError={setError} />
                ))}
              </div>
            ))}
          </div>

          <div className="kx-block">
            <h3>
              History ({data.historical_facts.length}){' '}
              <button className="btn-ghost kx-small" onClick={() => setShowHistory(!showHistory)}>
                {showHistory ? 'Hide' : 'Show'}
              </button>
            </h3>
            {showHistory && groupByType(data.historical_facts).map(([type, facts]) => (
              <div key={type} className="kx-type">
                <div className="kx-type-name">{type}</div>
                {facts.map((f) => (
                  <FactCard key={f.id} fact={f} historical reviewer={reviewer}
                    onReviewed={() => loadEntity()} onError={setError} />
                ))}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
