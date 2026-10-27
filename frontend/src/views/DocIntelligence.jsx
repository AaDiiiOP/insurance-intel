import { useEffect, useRef, useState } from 'react'

import {
  createSubmission,
  getExtractionFact,
  getSubmission,
  listSubmissions,
  reviewExtractionFact,
  uploadForExtraction,
} from '../api'

/**
 * The Document Intelligence vertical slice.
 *
 * A submission groups several documents believed to belong to the same
 * case - a policy, its own coverage schedule, an endorsement - so facts
 * extracted from them can be compared to each other. Upload two documents
 * into one submission and, if they state different figures for the same
 * thing, both are flagged rather than one silently winning. Upload a
 * document with no submission and it is scored on its own, same as before.
 *
 * Still deliberately narrow: one fact type (the coverage limit), proving
 * upload -> OCR -> fact -> evidence -> confidence -> cross-document check
 * -> review -> audit works end to end before the same shapes are used for
 * anything else.
 */

const CONFIDENCE_COPY = {
  deterministic: {
    label: 'Verified — deterministic check',
    detail: (c) => c.method.replace(/_/g, ' '),
    tone: 'ok',
  },
  ocr_engine: {
    label: (c) => `${Math.round(c.value * 100)}% OCR confidence`,
    detail: () => "Tesseract's own recognition score for this page.",
    tone: (c) => (c.value >= 0.75 ? 'ok' : c.value >= 0.5 ? 'warn' : 'bad'),
  },
  model_self_reported: {
    label: (c) => `${Math.round(c.value * 100)}% model confidence`,
    detail: () => "The model's own stated plausibility — not a measurement.",
    tone: (c) => (c.value >= 0.75 ? 'ok' : c.value >= 0.5 ? 'warn' : 'bad'),
  },
}

const STATUS_COPY = {
  needs_review: { label: 'Needs review', tone: 'warn' },
  verified: { label: 'Verified', tone: 'ok' },
  corrected: { label: 'Corrected', tone: 'ok' },
  contradiction_flagged: { label: 'Contradicts another document', tone: 'bad' },
}

function ConfidenceBadge({ confidence }) {
  const copy = CONFIDENCE_COPY[confidence.type]
  const label = typeof copy.label === 'function' ? copy.label(confidence) : copy.label
  const tone = typeof copy.tone === 'function' ? copy.tone(confidence) : copy.tone
  const detail = copy.detail(confidence)

  return (
    <div className={`confidence-badge tone-${tone}`}>
      <span className="confidence-label">{label}</span>
      <span className="confidence-detail">{detail}</span>
      {confidence.note && <span className="confidence-note">{confidence.note}</span>}
    </div>
  )
}

function FactCard({ fact, filename, onUpdated, onError }) {
  const [reviewer, setReviewer] = useState('')
  const [correcting, setCorrecting] = useState(false)
  const [correctedValue, setCorrectedValue] = useState('')
  const [reason, setReason] = useState('')
  const [acting, setActing] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState(fact.history || null)

  const statusCopy = STATUS_COPY[fact.status] || STATUS_COPY.needs_review

  async function act(action) {
    if (action === 'corrected' && !correctedValue.trim()) {
      onError('Enter the corrected value.')
      return
    }
    if (action === 'corrected' && !reason.trim()) {
      onError('Give a reason for the correction.')
      return
    }

    setActing(true)
    try {
      const updated = await reviewExtractionFact(
        fact.fact_id,
        reviewer,
        action,
        action === 'corrected' ? correctedValue : null,
        reason,
      )
      onUpdated(updated)
      setHistory(updated.history || null)
      setCorrecting(false)
      setCorrectedValue('')
      setReason('')
    } catch (e) {
      onError(e.message)
    } finally {
      setActing(false)
    }
  }

  async function toggleHistory() {
    if (!showHistory && !history) {
      try {
        const full = await getExtractionFact(fact.fact_id)
        setHistory(full.history || [])
      } catch (e) {
        onError(e.message)
      }
    }
    setShowHistory((v) => !v)
  }

  return (
    <div
      className={`extracted-fact-block ${
        fact.status === 'contradiction_flagged' ? 'is-contradiction' : ''
      }`}
    >
      <div className="section-head" style={{ marginBottom: 10 }}>
        <h2 style={{ fontSize: 16 }}>{filename}</h2>
        <span
          className={`pill ${
            statusCopy.tone === 'ok' ? 'ok' : statusCopy.tone === 'bad' ? 'bad' : ''
          }`}
        >
          {statusCopy.label}
        </span>
      </div>

      <div className="extracted-fact">
        <div className="extracted-fact-label">{fact.label}</div>
        <div className="extracted-fact-value">{fact.value}</div>
        {fact.original_value && fact.original_value !== fact.value && (
          <div className="extracted-fact-original">
            Originally extracted as: {fact.original_value}
          </div>
        )}
      </div>

      <ConfidenceBadge confidence={fact.confidence} />

      {fact.status === 'contradiction_flagged' && fact.contradiction_note && (
        <div className="banner bad" style={{ marginTop: 12 }}>
          {fact.contradiction_note}
        </div>
      )}

      <div className="evidence-block">
        <div className="plan-exclusions-label">Evidence</div>
        <div className="evidence-source">
          {[
            fact.evidence.page ? `Page ${fact.evidence.page}` : null,
            fact.evidence.section,
            fact.evidence.source === 'ocr' ? 'read via OCR' : 'read from text layer',
          ]
            .filter(Boolean)
            .join(' · ')}
        </div>
        {fact.evidence.snippet && (
          <blockquote className="evidence-snippet">"{fact.evidence.snippet}"</blockquote>
        )}
      </div>

      <div className="review-bar">

        {!correcting ? (
          <>
            <button className="btn-primary" onClick={() => act('verified')} disabled={acting}>
              ✓ Verify
            </button>
            <button
              className="btn-ghost"
              onClick={() => {
                setCorrectedValue(fact.value)
                setCorrecting(true)
              }}
              disabled={acting}
            >
              ✎ Correct
            </button>
            <button className="btn-danger" onClick={() => act('flagged')} disabled={acting}>
              ⚠ Needs review
            </button>
          </>
        ) : (
          <div className="correction-form">
            <input
              className="approve-note"
              placeholder="Corrected value"
              value={correctedValue}
              onChange={(e) => setCorrectedValue(e.target.value)}
            />
            <input
              className="approve-note"
              placeholder="Reason (required)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <button className="btn-ghost" onClick={() => setCorrecting(false)}>
              Cancel
            </button>
            <button className="btn-primary" onClick={() => act('corrected')} disabled={acting}>
              Save correction
            </button>
          </div>
        )}
      </div>

      <div className="history">
        <button className="history-toggle" onClick={toggleHistory}>
          {showHistory ? 'Hide' : 'Show'} history
        </button>
        {showHistory &&
          history?.map((h, i) => (
            <div className="history-row" key={i}>
              <span className={`history-event ${h.event === 'flagged' ? 'edited' : h.event}`}>
                {h.event}
              </span>
              <span className="history-when">{new Date(h.at).toLocaleString()}</span>
              <span className="history-detail">
                {h.reviewer}
                {h.event === 'corrected' && ` — "${h.before_value}" → "${h.after_value}"`}
                {h.reason && ` (${h.reason})`}
              </span>
            </div>
          ))}
      </div>
    </div>
  )
}

export default function DocIntelligence() {
  // The pilot-stage tenant boundary, made visible and switchable rather
  // than an invisible default every request silently used. '' means "no
  // organization selected" — api.js then sends no header at all, which the
  // backend treats exactly like every request made before this existed.

  const [submissions, setSubmissions] = useState([])
  const [submissionId, setSubmissionId] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const [creating, setCreating] = useState(false)

  const [documents, setDocuments] = useState([])
  const [uploading, setUploading] = useState(false)
  const [extractionError, setExtractionError] = useState(null)
  const [error, setError] = useState(null)

  const fileRef = useRef(null)

  useEffect(() => {
    listSubmissions()
      .then((d) => setSubmissions(d.submissions || []))
      .catch(() => setSubmissions([]))
  }, [])

  async function handleCreateSubmission() {
    if (!newLabel.trim()) {
      setError('Give the submission a name — e.g. the company or case it belongs to.')
      return
    }
    setCreating(true)
    setError(null)
    try {
      const created = await createSubmission(newLabel.trim())
      setSubmissions((current) => [created, ...current])
      setSubmissionId(created.submission_id)
      setNewLabel('')
      setDocuments([])
    } catch (e) {
      setError(e.message)
    } finally {
      setCreating(false)
    }
  }

  async function loadSubmission(id) {
    if (!id) {
      setDocuments([])
      return
    }
    try {
      const full = await getSubmission(id)
      const byDocument = {}
      for (const doc of full.documents) {
        byDocument[doc.document_id] = {
          document_id: doc.document_id,
          filename: doc.filename,
          fact: null,
        }
      }
      for (const fact of full.facts) {
        if (byDocument[fact.document_id]) byDocument[fact.document_id].fact = fact
      }
      setDocuments(Object.values(byDocument))
    } catch (e) {
      setError(e.message)
    }
  }

  function selectSubmission(id) {
    setSubmissionId(id)
    setError(null)
    setExtractionError(null)
    loadSubmission(id)
  }

  async function handleUpload(file) {
    if (!file) return
    setError(null)
    setExtractionError(null)
    setUploading(true)
    try {
      const result = await uploadForExtraction(file, submissionId || null)
      setDocuments((current) => [
        ...current,
        {
          document_id: result.document_id,
          filename: file.name,
          quality: result.quality,
          fact: result.fact,
        },
      ])
      setExtractionError(result.extraction_error)

      if (result.contradiction_detected && submissionId) {
        loadSubmission(submissionId)
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }

  function updateFact(documentId, updatedFact) {
    setDocuments((current) =>
      current.map((d) => (d.document_id === documentId ? { ...d, fact: updatedFact } : d)),
    )
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Vertical slice</p>
          <h1>Document Intelligence</h1>
          <p className="subtitle">
            Group a few documents into one submission — a policy and its own
            coverage schedule, say — and each one's coverage limit is
            extracted and checked against the others. If two disagree, both
            are flagged rather than one silently winning. Upload with no
            submission selected to score a single document on its own.
            Everything stays local — OCR runs with Tesseract, extraction
            with the same local model as the rest of the tool.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      <p className="subtitle" style={{ marginBottom: 12 }}>
        Everything below is scoped to the organization you are signed in to (shown in the sidebar).
        Switch organization there; another organization's submissions are treated as not existing.
      </p>

      <div className="section">
        <div className="section-head">
          <h2>Submission</h2>
        </div>

        <div className="scope-row" style={{ marginBottom: 10 }}>
          <label htmlFor="submission-picker">Working in</label>
          <select
            id="submission-picker"
            value={submissionId}
            onChange={(e) => selectSubmission(e.target.value)}
          >
            <option value="">no submission — score documents one at a time</option>
            {submissions.map((s) => (
              <option key={s.submission_id} value={s.submission_id}>
                {s.label}
              </option>
            ))}
          </select>
        </div>

        <div className="actions" style={{ marginTop: 0 }}>
          <input
            className="approve-note"
            style={{ maxWidth: 260 }}
            placeholder="New submission name — e.g. a company name"
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
          />
          <button className="btn-ghost" onClick={handleCreateSubmission} disabled={creating}>
            {creating ? 'Creating…' : '+ New submission'}
          </button>
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.docx,.xlsx,.pptx"
        style={{ display: 'none' }}
        onChange={(e) => {
          handleUpload(e.target.files?.[0])
          e.target.value = ''
        }}
      />

      <button
        type="button"
        className={`dropzone ${uploading ? 'is-over' : ''}`}
        onClick={() => fileRef.current?.click()}
        disabled={uploading}
        style={{ marginTop: 18 }}
      >
        <div className="dropzone-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 20 20" fill="none">
            <path
              d="M10 14V4m0 0L6 8m4-4l4 4M3 15v1a2 2 0 002 2h10a2 2 0 002-2v-1"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>
        <p className="dropzone-hint">
          {uploading
            ? 'Reading the document — OCR runs automatically if it is scanned…'
            : submissionId
              ? 'Drop the next document for this submission here'
              : 'Drop a policy document here — a scanned copy is fine'}
        </p>
      </button>

      {extractionError && (
        <div className="banner bad" style={{ marginTop: 14 }}>
          No fact was recorded for that upload. {extractionError}
        </div>
      )}

      {documents.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>{submissionId ? 'Documents in this submission' : 'Extracted fact'}</h2>
            <span className="section-count">{documents.length}</span>
          </div>

          {documents.map((d) =>
            d.fact ? (
              <div key={d.document_id} style={{ marginBottom: 16 }}>
                <FactCard
                  fact={d.fact}
                  filename={d.filename}
                  onUpdated={(updated) => updateFact(d.document_id, updated)}
                  onError={setError}
                />
              </div>
            ) : (
              <div className="listing-item" key={d.document_id}>
                <div className="listing-text">{d.filename} — no fact extracted</div>
              </div>
            ),
          )}
        </div>
      )}

      <div className="setup-note">
        This is a vertical slice, not the finished extraction system — it
        proves one path end to end (evidence, typed confidence,
        cross-document comparison, review, audit) on one fact type before
        the same shapes are used for anything else.
      </div>
    </>
  )
}
