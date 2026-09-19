import { useCallback, useEffect, useState } from 'react'

import {
  getHouseStyle,
  getInsights,
  learnHouseStyle,
  saveHouseStyle,
} from '../api'

const KINDS = [
  { id: 'submission', label: 'Submission to underwriter' },
  { id: 'proposal', label: 'Risk summary' },
  { id: 'chase', label: 'Chase email' },
]

const VERDICT_LABEL = {
  verified: 'verified',
  mismatch: 'wrong entity',
  wrong_document: 'wrong document',
  unreadable: 'unreadable',
  check: 'needed a look',
  unchecked: 'not checked',
}

export default function Review() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [kind, setKind] = useState('submission')
  const [style, setStyle] = useState(null)
  const [learning, setLearning] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draftStyle, setDraftStyle] = useState('')
  const [observations, setObservations] = useState([])
  const [note, setNote] = useState('')

  useEffect(() => {
    getInsights()
      .then(setData)
      .catch((e) => setError(e.message))
  }, [])

  const loadStyle = useCallback(async () => {
    setObservations([])
    setNote('')
    try {
      setStyle(await getHouseStyle(kind))
    } catch (e) {
      setError(e.message)
    }
  }, [kind])

  useEffect(() => {
    loadStyle()
  }, [loadStyle])

  async function handleLearn() {
    setLearning(true)
    setError(null)
    try {
      const result = await learnHouseStyle(kind)
      setStyle((current) => ({
        ...(current || {}),
        guidance: result.guidance,
        derived_from: result.derived_from,
        edited_by_hand: false,
      }))
      setObservations(result.observations || [])
      setNote(result.note || '')
    } catch (e) {
      setError(e.message)
    } finally {
      setLearning(false)
    }
  }

  async function handleSaveStyle() {
    try {
      const saved = await saveHouseStyle(kind, draftStyle)
      setStyle(saved)
      setEditing(false)
      if (saved.dropped?.length) {
        setError(
          `${saved.dropped.length} line(s) were not saved because they would make ` +
            'drafts less complete or less candid.',
        )
      }
    } catch (e) {
      setError(e.message)
    }
  }

  const verdicts = data?.verdicts || {}
  const badVerdicts =
    (verdicts.mismatch || 0) + (verdicts.wrong_document || 0)

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Review</p>
          <h1>How it is doing</h1>
          <p className="subtitle">
            Counted from what actually happened, not estimated. Every figure
            here traces back to real records, so one that looks wrong can be
            chased down.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}
      {!data && !error && <p className="loading">Loading…</p>}

      {data && (
        <>
          <div className="section">
            <div className="section-head">
              <h2>The work so far</h2>
            </div>
            <div className="metric-grid">
              {[
                [data.carriers_briefed, 'carriers briefed'],
                [data.clients, 'clients'],
                [data.matches_run, 'matches run'],
                [data.gap_reports, 'paperwork checks'],
                [data.drafts, 'documents drafted'],
                [data.drafts_approved, 'signed off'],
              ].map(([value, label]) => (
                <div className="metric" key={label}>
                  <div className="metric-value">{value ?? 0}</div>
                  <div className="metric-label">{label}</div>
                </div>
              ))}
            </div>
          </div>

          {Object.keys(verdicts).length > 0 && (
            <div className="section">
              <div className="section-head">
                <h2>Documents checked</h2>
                <span className="section-count">
                  {badVerdicts > 0
                    ? `${badVerdicts} caught before they reached an insurer`
                    : 'none rejected yet'}
                </span>
              </div>
              <div className="metric-grid">
                {Object.entries(verdicts).map(([verdict, count]) => (
                  <div
                    className={`metric ${
                      verdict === 'mismatch' || verdict === 'wrong_document'
                        ? 'is-alert'
                        : ''
                    }`}
                    key={verdict}
                  >
                    <div className="metric-value">{count}</div>
                    <div className="metric-label">
                      {VERDICT_LABEL[verdict] || verdict}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {data.thin_briefings?.length > 0 && (
            <div className="section">
              <div className="section-head">
                <h2>Briefings worth re-running</h2>
                <span className="section-count">
                  fewest requirements found
                </span>
              </div>
              <p className="subtitle" style={{ marginBottom: 11 }}>
                Paperwork checks are only as good as the requirements pulled
                out of a carrier's wording. A carrier near zero here will
                produce an empty checklist — regenerate its briefing.
              </p>
              <div className="listing">
                {data.thin_briefings.map((row) => (
                  <div className="listing-item" key={row.carrier}>
                    <div className="listing-text">
                      {row.carrier}
                      <span
                        className={`pill ${row.requirements < 3 ? 'bad' : ''}`}
                        style={{ marginLeft: 9 }}
                      >
                        {row.requirements} requirement
                        {row.requirements === 1 ? '' : 's'}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {data.overridden_requirements?.length > 0 && (
            <div className="section">
              <div className="section-head">
                <h2>Requirements you keep correcting</h2>
              </div>
              <p className="subtitle" style={{ marginBottom: 11 }}>
                The tool reads these wrong often enough that you have fixed
                them by hand. Worth checking how the carrier's wording states
                them.
              </p>
              <div className="listing">
                {data.overridden_requirements.map((row) => (
                  <div className="listing-item" key={row.requirement}>
                    <div className="listing-text">{row.requirement}</div>
                    <div className="listing-source">
                      corrected {row.times} time{row.times === 1 ? '' : 's'}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="section">
            <div className="section-head">
              <h2>How you write</h2>
              <span className="section-count">
                {data.draft_edits || 0} edit{data.draft_edits === 1 ? '' : 's'}
                {data.avg_draft_change_pct != null &&
                  ` · ${data.avg_draft_change_pct}% rewritten on average`}
              </span>
            </div>

            <p className="subtitle" style={{ marginBottom: 12 }}>
              Learned from how you rewrite the tool's drafts, and applied to
              future ones. Nothing is trained — this is guidance you can read
              and change. Rules that would make a document less complete or
              less candid are never kept.
            </p>

            <div className="scope-row">
              <label htmlFor="style-kind">For</label>
              <select
                id="style-kind"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
              >
                {KINDS.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.label}
                  </option>
                ))}
              </select>
            </div>

            {editing ? (
              <>
                <textarea
                  className="style-edit"
                  value={draftStyle}
                  onChange={(e) => setDraftStyle(e.target.value)}
                  placeholder="One instruction per line"
                />
                <div className="actions">
                  <span className="hint">
                    One instruction per line, at most six.
                  </span>
                  <button
                    className="btn-ghost"
                    onClick={() => setEditing(false)}
                  >
                    Cancel
                  </button>
                  <button className="btn-primary" onClick={handleSaveStyle}>
                    Save
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="style-box">
                  {style?.guidance ? (
                    style.guidance
                      .split('\n')
                      .filter(Boolean)
                      .map((line, i) => (
                        <div className="style-line" key={i}>
                          — {line}
                        </div>
                      ))
                  ) : (
                    <div className="style-line" style={{ color: 'var(--ink-faint)' }}>
                      Nothing learned yet. Edit a few drafts, then press Learn.
                    </div>
                  )}
                </div>

                {style?.derived_from > 0 && (
                  <p className="progress-note">
                    From {style.derived_from} edit
                    {style.derived_from === 1 ? '' : 's'}
                    {style.edited_by_hand && ' · edited by hand since'}
                  </p>
                )}

                {note && <p className="progress-note">{note}</p>}

                {observations.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <div className="plan-exclusions-label">What it noticed</div>
                    {observations.map((o, i) => (
                      <div className="observation" key={i}>
                        {o}
                      </div>
                    ))}
                  </div>
                )}

                <div className="actions">
                  <span className="hint" />
                  <button
                    className="btn-ghost"
                    onClick={() => {
                      setDraftStyle(style?.guidance || '')
                      setEditing(true)
                    }}
                  >
                    Edit by hand
                  </button>
                  <button
                    className="btn-primary"
                    onClick={handleLearn}
                    disabled={learning}
                  >
                    {learning ? 'Reading your edits…' : 'Learn from my edits'}
                  </button>
                </div>
              </>
            )}
          </div>
        </>
      )}
    </>
  )
}
