import { useState } from 'react'

import {
  analyzePropertyPolicy,
  correctKnowledgeFact,
  listSubmissions,
} from '../api'
import KnowledgeExplorer from './KnowledgeExplorer'

/**
 * Knowledge Core — Property Policy Intelligence.
 *
 * The first vertical slice built on top of the Knowledge Core
 * (backend/knowledge/): pick a submission (the same submissions the
 * Document Intelligence screen groups documents into), trigger analysis,
 * and see the Business/Asset/Policy entities and the property/policy facts
 * it produced, each traceable to its evidence, with any cross-document
 * contradiction called out rather than silently resolved.
 *
 * Deliberately not polished — this proves the API works end to end from a
 * real browser action, same bar as the rest of this vertical slice.
 */

const STATUS_COPY = {
  needs_review: { label: 'Needs review', tone: '' },
  verified: { label: 'Verified', tone: 'ok' },
  corrected: { label: 'Corrected', tone: 'ok' },
  contradiction_flagged: { label: 'Contradicts another document', tone: 'bad' },
}

function ConfidenceLine({ confidence }) {
  if (confidence.type === 'deterministic') {
    return <span className="confidence-detail">Deterministic — {confidence.method}</span>
  }
  const pct = Math.round(confidence.value * 100)
  const kind = confidence.type === 'ocr_engine' ? 'OCR confidence' : "model's stated confidence"
  return (
    <span className="confidence-detail">
      {pct}% {kind}
      {confidence.note ? ` — ${confidence.note}` : ''}
    </span>
  )
}

function FactRow({ fact, onError, onCorrected }) {
  const [correcting, setCorrecting] = useState(false)
  const [value, setValue] = useState(fact.value)
  const [reviewer, setReviewer] = useState('')
  const [saving, setSaving] = useState(false)
  const statusCopy = STATUS_COPY[fact.status] || STATUS_COPY.needs_review

  async function save() {
    if (!value.trim()) {
      onError('Enter the corrected value.')
      return
    }
    setSaving(true)
    try {
      const updated = await correctKnowledgeFact(fact.fact_id, value.trim(), reviewer.trim())
      onCorrected(updated)
      setCorrecting(false)
    } catch (e) {
      onError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className={`extracted-fact-block ${fact.status === 'contradiction_flagged' ? 'is-contradiction' : ''}`}>
      <div className="section-head" style={{ marginBottom: 8 }}>
        <h2 style={{ fontSize: 15 }}>{fact.fact_type}</h2>
        <span className={`pill ${statusCopy.tone === 'ok' ? 'ok' : statusCopy.tone === 'bad' ? 'bad' : ''}`}>
          {statusCopy.label}
        </span>
      </div>

      <div className="extracted-fact">
        <div className="extracted-fact-value">
          {fact.value}
          {fact.unit ? ` ${fact.unit}` : ''}
        </div>
      </div>

      <ConfidenceLine confidence={fact.confidence} />

      {fact.status === 'contradiction_flagged' && (
        <div className="banner bad" style={{ marginTop: 10 }}>
          This disagrees with another fact recorded for the same entity — both
          are flagged for review rather than one silently overwriting the
          other.
        </div>
      )}

      {(fact.evidence || []).map((ev) => (
        <div className="evidence-block" key={ev.evidence_id}>
          <div className="plan-exclusions-label">Evidence</div>
          <div className="evidence-source">
            {[
              ev.document_filename,
              ev.page ? `Page ${ev.page}` : null,
              ev.section,
              ev.source === 'ocr' ? 'read via OCR' : 'read from text layer',
            ]
              .filter(Boolean)
              .join(' · ')}
          </div>
          {ev.snippet && <blockquote className="evidence-snippet">"{ev.snippet}"</blockquote>}
        </div>
      ))}

      <div className="review-bar">
        {!correcting ? (
          <button className="btn-ghost" onClick={() => setCorrecting(true)}>
            ✎ Correct
          </button>
        ) : (
          <div className="correction-form">
            <input
              className="approve-note"
              placeholder="Corrected value"
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
            <button className="btn-ghost" onClick={() => setCorrecting(false)} disabled={saving}>
              Cancel
            </button>
            <button className="btn-primary" onClick={save} disabled={saving}>
              {saving ? 'Saving…' : 'Save correction'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function EntityBlock({ title, entity, facts, onError, onFactCorrected }) {
  return (
    <div className="section">
      <div className="section-head">
        <h2>
          {title}: {entity.name}
        </h2>
        <span className="section-count">{facts.length}</span>
      </div>
      {facts.length === 0 && (
        <p className="subtitle">No facts of this kind were confidently found in these documents.</p>
      )}
      {facts.map((f) => (
        <div key={f.fact_id} style={{ marginBottom: 14 }}>
          <FactRow
            fact={f}
            onError={onError}
            onCorrected={(updated) => onFactCorrected(f.fact_id, updated)}
          />
        </div>
      ))}
    </div>
  )
}

export default function Knowledge() {
  const [submissions, setSubmissions] = useState([])
  const [submissionId, setSubmissionId] = useState('')
  const [loadingSubmissions, setLoadingSubmissions] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  async function loadSubmissions() {
    setLoadingSubmissions(true)
    setError(null)
    try {
      const data = await listSubmissions()
      setSubmissions(data.submissions || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoadingSubmissions(false)
    }
  }

  async function handleAnalyze() {
    if (!submissionId) {
      setError('Pick a submission first — create one on the Doc Intelligence screen if none exist yet.')
      return
    }
    setAnalyzing(true)
    setError(null)
    try {
      const data = await analyzePropertyPolicy(submissionId)
      setResult(data)
    } catch (e) {
      setError(e.message)
      setResult(null)
    } finally {
      setAnalyzing(false)
    }
  }

  function updateFact(factId, updated) {
    setResult((current) => {
      if (!current) return current
      return {
        ...current,
        facts: current.facts.map((f) => (f.fact_id === factId ? updated : f)),
      }
    })
  }

  const propertyFacts = (result?.facts || []).filter((f) => f.fact_type.startsWith('property.'))
  const policyFacts = (result?.facts || []).filter((f) => f.fact_type.startsWith('policy.'))

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Knowledge Core — vertical slice</p>
          <h1>Property Policy Intelligence</h1>
          <p className="subtitle">
            Pick a submission (a policy, its coverage schedule, an
            endorsement, a risk survey — grouped on the Doc Intelligence
            screen) and run analysis: a Business, the property it owns, and
            its policy are created as entities, and every property/policy
            fact found in each document becomes a fact with real evidence
            attached. A figure that disagrees across documents is flagged on
            both sides, not silently resolved.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      <div className="section">
        <div className="section-head">
          <h2>Submission</h2>
        </div>
        <div className="scope-row" style={{ marginBottom: 10 }}>
          <label htmlFor="kc-submission-picker">Analyze</label>
          <select
            id="kc-submission-picker"
            value={submissionId}
            onChange={(e) => setSubmissionId(e.target.value)}
            onFocus={loadSubmissions}
          >
            <option value="">
              {loadingSubmissions ? 'Loading submissions…' : 'choose a submission'}
            </option>
            {submissions.map((s) => (
              <option key={s.submission_id} value={s.submission_id}>
                {s.label}
              </option>
            ))}
          </select>
          <button className="btn-ghost" onClick={loadSubmissions} disabled={loadingSubmissions}>
            {loadingSubmissions ? 'Refreshing…' : 'Refresh list'}
          </button>
        </div>
        <div className="actions" style={{ marginTop: 0 }}>
          <button className="btn-primary" onClick={handleAnalyze} disabled={analyzing}>
            {analyzing ? 'Analyzing…' : 'Run property policy analysis'}
          </button>
        </div>
      </div>

      {result && (
        <>
          <div className="section">
            <div className="section-head">
              <h2>Entities</h2>
            </div>
            <div className="listing-item">
              <div className="listing-text">
                Business — {result.business.name}
              </div>
            </div>
            <div className="listing-item">
              <div className="listing-text">
                owns → Asset (property) — {result.asset.name}
              </div>
            </div>
            <div className="listing-item">
              <div className="listing-text">
                has_policy → Policy — {result.policy.name}
              </div>
            </div>
          </div>

          <div className="section">
            <div className="section-head">
              <h2>Documents processed</h2>
              <span className="section-count">{result.documents_processed.length}</span>
            </div>
            {result.documents_processed.map((d) => (
              <div className="listing-item" key={d.document_id}>
                <div className="listing-text">
                  {d.filename} — {d.facts_found} fact(s) found
                  {d.note ? ` (${d.note})` : ''}
                </div>
              </div>
            ))}
          </div>

          {result.contradictions.length > 0 && (
            <div className="section">
              <div className="section-head">
                <h2>Contradictions detected</h2>
                <span className="section-count">{result.contradictions.length}</span>
              </div>
              {result.contradictions.map((c, i) => (
                <div className="banner bad" key={i} style={{ marginBottom: 8 }}>
                  {c.fact_type}: "{c.value}" (from {c.document_filename}) disagrees with a
                  previously recorded value "{c.other_value}" — both flagged for review.
                </div>
              ))}
            </div>
          )}

          <EntityBlock
            title="Property facts"
            entity={result.asset}
            facts={propertyFacts}
            onError={setError}
            onFactCorrected={updateFact}
          />
          <EntityBlock
            title="Policy facts"
            entity={result.policy}
            facts={policyFacts}
            onError={setError}
            onFactCorrected={updateFact}
          />
        </>
      )}

      <KnowledgeExplorer initialEntityId={result?.business?.entity_id} />

      <div className="setup-note">
        This is a vertical slice on top of the Knowledge Core, not the
        finished reasoning layer — it proves entities, facts, evidence,
        relationships, and cross-document reconciliation hold up end to end
        for one concrete shape (Business → Property + Policy) before more
        fact types or relationships are added.
      </div>
    </>
  )
}
