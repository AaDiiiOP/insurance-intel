import { useRef, useState } from 'react'

import {
  getExtractionFact,
  reviewExtractionFact,
  uploadForExtraction,
} from '../api'

/**
 * The Document Intelligence vertical slice.
 *
 * One path only: upload a policy document, see the one fact extracted from
 * it (the coverage limit), with its evidence and typed confidence, and
 * verify or correct it. This is deliberately narrow — the point of a
 * vertical slice is to prove upload -> OCR -> fact -> evidence -> confidence
 * -> review -> audit works end to end on one thing, not to cover every
 * extraction path at once.
 */

// How each confidence TYPE is presented, per the explicit instruction: never
// show a bare percentage. A number means something different depending on
// where it came from, and the label has to say which claim is being made.
const CONFIDENCE_COPY = {
  deterministic: {
    label: 'Verified — deterministic check',
    detail: (c) => c.method.replace(/_/g, ' '),
    tone: 'ok',
  },
  ocr_engine: {
    label: (c) => `${Math.round(c.value * 100)}% OCR confidence`,
    detail: () => 'Tesseract\u2019s own recognition score for this page.',
    tone: (c) => (c.value >= 0.75 ? 'ok' : c.value >= 0.5 ? 'warn' : 'bad'),
  },
  model_self_reported: {
    label: (c) => `${Math.round(c.value * 100)}% model confidence`,
    detail: () => 'The model\u2019s own stated plausibility — not a measurement.',
    tone: (c) => (c.value >= 0.75 ? 'ok' : c.value >= 0.5 ? 'warn' : 'bad'),
  },
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

const STATUS_COPY = {
  needs_review: { label: 'Needs review', tone: 'warn' },
  verified: { label: 'Verified', tone: 'ok' },
  corrected: { label: 'Corrected', tone: 'ok' },
}

export default function DocIntelligence() {
  const [uploading, setUploading] = useState(false)
  const [fact, setFact] = useState(null)
  const [quality, setQuality] = useState(null)
  const [documentId, setDocumentId] = useState(null)
  const [extractionError, setExtractionError] = useState(null)
  const [error, setError] = useState(null)

  const [reviewer, setReviewer] = useState('')
  const [correcting, setCorrecting] = useState(false)
  const [correctedValue, setCorrectedValue] = useState('')
  const [reason, setReason] = useState('')
  const [acting, setActing] = useState(false)

  const fileRef = useRef(null)

  function reset() {
    setFact(null)
    setQuality(null)
    setDocumentId(null)
    setExtractionError(null)
    setError(null)
    setCorrecting(false)
    setCorrectedValue('')
    setReason('')
  }

  async function handleUpload(file) {
    if (!file) return
    reset()
    setUploading(true)
    try {
      const result = await uploadForExtraction(file)
      setDocumentId(result.document_id)
      setQuality(result.quality)
      setFact(result.fact)
      setExtractionError(result.extraction_error)
    } catch (e) {
      setError(e.message)
    } finally {
      setUploading(false)
    }
  }

  async function act(action) {
    if (!fact) return
    if (!reviewer.trim()) {
      setError('Enter your name first.')
      return
    }
    if (action === 'corrected' && !correctedValue.trim()) {
      setError('Enter the corrected value.')
      return
    }

    setActing(true)
    setError(null)
    try {
      const updated = await reviewExtractionFact(
        fact.fact_id,
        reviewer,
        action,
        action === 'corrected' ? correctedValue : null,
        reason,
      )
      setFact(updated)
      setCorrecting(false)
      setCorrectedValue('')
      setReason('')
    } catch (e) {
      setError(e.message)
    } finally {
      setActing(false)
    }
  }

  async function refreshHistory() {
    if (!fact) return
    try {
      setFact(await getExtractionFact(fact.fact_id))
    } catch (e) {
      setError(e.message)
    }
  }

  const statusCopy = fact ? STATUS_COPY[fact.status] : null

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Vertical slice</p>
          <h1>Document Intelligence</h1>
          <p className="subtitle">
            One path, end to end: upload a policy document, extract its
            coverage limit with evidence and a typed confidence, then verify
            or correct it. Everything here stays local — OCR runs with
            Tesseract, extraction with the same local model as the rest of
            the tool.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

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

      {!fact && (
        <button
          type="button"
          className={`dropzone ${uploading ? 'is-over' : ''}`}
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
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
              ? 'Reading the document \u2014 OCR runs automatically if it is scanned…'
              : 'Drop a policy document here \u2014 a scanned copy is fine'}
          </p>
        </button>
      )}

      {quality && (
        <div className="section">
          <div className="section-head">
            <h2>Document quality</h2>
          </div>
          <div className="facts">
            <div className="fact">
              <div className="fact-label">Text source</div>
              <div className="fact-value">
                {quality.used_ocr ? 'OCR (scanned document)' : 'Native text layer'}
              </div>
            </div>
            <div className="fact">
              <div className="fact-label">Pages</div>
              <div className="fact-value">{quality.pages}</div>
            </div>
            {quality.used_ocr && (
              <div className="fact">
                <div className="fact-label">Average OCR confidence</div>
                <div className="fact-value">
                  {quality.avg_ocr_confidence?.toFixed(1)}%
                </div>
              </div>
            )}
          </div>
          {quality.notes?.length > 0 && (
            <div className="banner info" style={{ marginTop: 12 }}>
              {quality.notes.join(' ')}
            </div>
          )}
        </div>
      )}

      {extractionError && (
        <div className="banner bad">
          No fact was recorded. {extractionError}
        </div>
      )}

      {fact && (
        <div className="section">
          <div className="section-head">
            <h2>Extracted fact</h2>
            <span className={`pill ${statusCopy.tone === 'ok' ? 'ok' : ''}`}>
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

          <div className="evidence-block">
            <div className="plan-exclusions-label">Evidence</div>
            <div className="evidence-source">
              {[
                fact.evidence.page ? `Page ${fact.evidence.page}` : null,
                fact.evidence.section,
                fact.evidence.source === 'ocr' ? 'read via OCR' : 'read from text layer',
              ]
                .filter(Boolean)
                .join(' \u00b7 ')}
            </div>
            {fact.evidence.snippet && (
              <blockquote className="evidence-snippet">
                “{fact.evidence.snippet}”
              </blockquote>
            )}
          </div>

          <div className="review-bar">
            <input
              className="approve-name"
              placeholder="Your name"
              value={reviewer}
              onChange={(e) => setReviewer(e.target.value)}
            />

            {!correcting ? (
              <>
                <button
                  className="btn-primary"
                  onClick={() => act('verified')}
                  disabled={acting}
                >
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
                <button
                  className="btn-danger"
                  onClick={() => act('flagged')}
                  disabled={acting}
                >
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
                  placeholder="Reason (optional)"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
                <button className="btn-ghost" onClick={() => setCorrecting(false)}>
                  Cancel
                </button>
                <button
                  className="btn-primary"
                  onClick={() => act('corrected')}
                  disabled={acting}
                >
                  Save correction
                </button>
              </div>
            )}
          </div>

          <div className="history">
            <button className="history-toggle" onClick={refreshHistory}>
              Refresh history
            </button>
            {fact.history?.map((h, i) => (
              <div className="history-row" key={i}>
                <span className={`history-event ${h.event === 'flagged' ? 'edited' : h.event}`}>
                  {h.event}
                </span>
                <span className="history-when">
                  {new Date(h.at).toLocaleString()}
                </span>
                <span className="history-detail">
                  {h.reviewer}
                  {h.event === 'corrected' &&
                    ` — "${h.before_value}" → "${h.after_value}"`}
                  {h.reason && ` (${h.reason})`}
                </span>
              </div>
            ))}
          </div>

          <div className="actions">
            <span className="hint" />
            <button className="btn-ghost" onClick={reset}>
              Upload another document
            </button>
          </div>
        </div>
      )}

      <div className="setup-note">
        This is a vertical slice, not the finished extraction system — it
        proves one path end to end (evidence, typed confidence, review,
        audit) on one fact type before the same shapes are used for anything
        else. Document ID: <code>{documentId || '—'}</code>
      </div>
    </>
  )
}
