import { useCallback, useEffect, useRef, useState } from 'react'

import ClientWorkspacePanel from './ClientWorkspacePanel'

import {
  approveDraft,
  createDraft,
  downloadDraft,
  fillProposalForm,
  getDraft,
  listCarriersForPicker,
  listDraftKinds,
  overrideGapItem,
  revokeDraft,
  deleteClient,
  deleteDraft,
  editDraft,
  getClient,
  getGapAnalysis,
  getMatch,
  getRedactedProfile,
  listClientDocuments,
  listDrafts,
  runGapAnalysis,
  runMatch,
  uploadClientDocument,
} from '../api'

/** Who last approved, from the history — the trail is the record, not the row. */
function approvedBy(history) {
  if (!history) return null
  const approvals = history.filter((e) => e.event === 'approved')
  return approvals.length ? approvals[approvals.length - 1].actor : null
}

/** Score bands, so colour and number always agree. */
function scoreBand(score) {
  if (score >= 75) return 'good'
  if (score >= 45) return 'fair'
  return 'poor'
}

const VERDICT_LABEL = {
  verified: 'verified',
  mismatch: 'wrong entity',
  wrong_document: 'wrong document',
  unreadable: 'unreadable',
  check: 'needs a look',
  unchecked: 'not checked',
}

const FIT_LABEL = { strong: 'Strong fit', possible: 'Possible', poor: 'Poor fit' }
const FIT_CLASS = { strong: 'ok', possible: '', poor: 'bad' }

const DETAIL_ROWS = [
  ['entity_type', 'Entity type'],
  ['industry', 'Industry'],
  ['turnover', 'Turnover'],
  ['years_trading', 'Years trading'],
  ['employee_count', 'Employees'],
  ['sum_insured', 'Sum insured sought'],
  ['pan', 'PAN'],
  ['gstin', 'GSTIN'],
  ['contact_name', 'Contact'],
  ['contact_phone', 'Phone'],
  ['contact_email', 'Email'],
]

function sourceLine(source) {
  const parts = [source.carrier_name]
  if (source.filename) parts.push(source.filename)
  if (source.section_title) parts.push(source.section_title)
  if (source.page_start) parts.push(`p.${source.page_start}`)
  return parts.filter(Boolean).join(' · ')
}

export default function ClientDetail({ me, clientId, onBack, onDeleted }) {
  const [client, setClient] = useState(null)
  const [match, setMatch] = useState(null)
  const [profile, setProfile] = useState(null)
  const [showProfile, setShowProfile] = useState(false)
  const [error, setError] = useState(null)
  const [running, setRunning] = useState(false)
  const [gaps, setGaps] = useState(null)
  const [checkingGaps, setCheckingGaps] = useState(false)
  // Attachments keyed by requirement text, newest kept — the same key the
  // backend matches on when deciding whether a requirement is satisfied.
  const [attachments, setAttachments] = useState({})
  const [uploadingFor, setUploadingFor] = useState(null)
  const [carriers, setCarriers] = useState([])
  const [gapCarrier, setGapCarrier] = useState('')
  const [drafts, setDrafts] = useState([])
  const [draftKinds, setDraftKinds] = useState([])
  const [draftKind, setDraftKind] = useState('submission')
  const [drafting, setDrafting] = useState(false)
  const [editingDraft, setEditingDraft] = useState(null)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftBody, setDraftBody] = useState('')
  // The approver's name is remembered across drafts in one sitting — a broker
  // signing off three submissions should not retype it three times.
  const [approver, setApprover] = useState('')
  const [approveNote, setApproveNote] = useState('')
  const [openHistory, setOpenHistory] = useState(null)
  const [histories, setHistories] = useState({})
  const fileRef = useRef(null)
  const formRef = useRef(null)
  const pendingRequirement = useRef(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const data = await getClient(clientId)
      setClient(data)
      if (data.has_match) {
        try {
          setMatch(await getMatch(clientId))
        } catch {
          setMatch(null)
        }
      }
      try {
        const stored = await getGapAnalysis(clientId)
        setGaps(stored.reports?.length ? stored.reports : null)
      } catch {
        setGaps(null)
      }
      try {
        const d = await listDrafts(clientId)
        setDrafts(d.drafts || [])
      } catch {
        setDrafts([])
      }
      try {
        const docs = await listClientDocuments(clientId)
        const byRequirement = {}
        // Sorted newest first by the API, so the first seen per requirement
        // is the current one.
        for (const doc of docs.documents || []) {
          if (!byRequirement[doc.requirement]) byRequirement[doc.requirement] = doc
        }
        setAttachments(byRequirement)
      } catch {
        setAttachments({})
      }
    } catch (e) {
      setError(e.message)
    }
  }, [clientId])

  useEffect(() => {
    load()
  }, [load])

  // Carriers with a briefing, for checking paperwork against a specific one
  // rather than relying on the last match to name it.
  useEffect(() => {
    listCarriersForPicker()
      .then((d) => setCarriers((d.carriers || []).filter((c) => c.has_summary)))
      .catch(() => setCarriers([]))
    listDraftKinds()
      .then((d) => setDraftKinds(d.kinds || []))
      .catch(() => setDraftKinds([]))
  }, [])

  async function handleMatch() {
    setRunning(true)
    setError(null)
    try {
      setMatch(await runMatch(clientId))
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  async function toggleProfile() {
    if (!showProfile && !profile) {
      try {
        setProfile(await getRedactedProfile(clientId))
      } catch (e) {
        setError(e.message)
        return
      }
    }
    setShowProfile((v) => !v)
  }

  async function handleGaps() {
    setCheckingGaps(true)
    setError(null)
    try {
      const result = await runGapAnalysis(clientId, gapCarrier || null)
      setGaps(result.reports)
      if (result.failures?.length) {
        setError(
          result.failures
            .map((f) => `${f.carrier_name}: ${f.error}`)
            .join(' · '),
        )
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setCheckingGaps(false)
    }
  }

  async function handleFormFile(file) {
    if (!file) return
    setDrafting(true)
    setError(null)
    try {
      const created = await fillProposalForm(clientId, file, gapCarrier || null)
      setDrafts((current) => [created, ...current])
    } catch (e) {
      setError(e.message)
    } finally {
      setDrafting(false)
    }
  }

  async function handleDraft() {
    // "Fill in a proposal form" answers a document the carrier supplied, so
    // it needs that document rather than writing from scratch like the rest.
    if (draftKind === 'form') {
      formRef.current?.click()
      return
    }
    setDrafting(true)
    setError(null)
    try {
      const created = await createDraft(clientId, gapCarrier || null, draftKind)
      setDrafts((current) => [created, ...current])
    } catch (e) {
      setError(e.message)
    } finally {
      setDrafting(false)
    }
  }

  async function saveDraftEdit(draftId) {
    try {
      const updated = await editDraft(draftId, draftTitle, draftBody)
      setDrafts((current) =>
        current.map((d) => (d.draft_id === draftId ? updated : d)),
      )
      setEditingDraft(null)
    } catch (e) {
      setError(e.message)
    }
  }

  function replaceDraft(updated) {
    setDrafts((current) =>
      current.map((d) => (d.draft_id === updated.draft_id ? updated : d)),
    )
    if (updated.history) {
      setHistories((current) => ({ ...current, [updated.draft_id]: updated.history }))
    }
  }

  async function handleApprove(draftId) {
    setError(null)
    try {
      replaceDraft(await approveDraft(draftId, approver, approveNote))
      setApproveNote('')
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleOverride(reportId, requirement, status) {
    setError(null)
    try {
      await overrideGapItem(reportId, requirement, status, 'Corrected by hand.')
      // Update in place rather than refetching: the report is long and a
      // full reload would scroll the page away from what was just corrected.
      setGaps((current) =>
        (current || []).map((report) =>
          report.report_id !== reportId
            ? report
            : {
                ...report,
                items: report.items.map((item) =>
                  item.requirement === requirement
                    ? { ...item, status, reason: 'Set by hand.', ask: '', overridden: true }
                    : item,
                ),
              },
        ),
      )
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleDownload(draftId) {
    setError(null)
    try {
      await downloadDraft(draftId)
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleRevoke(draftId) {
    const reason = window.prompt('Why is this sign-off being withdrawn?')
    if (reason === null) return
    setError(null)
    try {
      replaceDraft(await revokeDraft(draftId, reason))
    } catch (e) {
      setError(e.message)
    }
  }

  async function toggleHistory(draftId) {
    if (openHistory === draftId) {
      setOpenHistory(null)
      return
    }
    setOpenHistory(draftId)
    if (!histories[draftId]) {
      try {
        const full = await getDraft(draftId)
        setHistories((current) => ({ ...current, [draftId]: full.history || [] }))
      } catch (e) {
        setError(e.message)
      }
    }
  }

  async function removeDraft(draftId) {
    if (!window.confirm('Delete this draft?')) return
    try {
      await deleteDraft(draftId)
      setDrafts((current) => current.filter((d) => d.draft_id !== draftId))
    } catch (e) {
      setError(e.message)
    }
  }

  function pickFileFor(requirement) {
    pendingRequirement.current = requirement
    fileRef.current?.click()
  }

  async function handleAttach(file) {
    const requirement = pendingRequirement.current
    if (!file || !requirement) return

    setUploadingFor(requirement)
    setError(null)
    try {
      const result = await uploadClientDocument(clientId, requirement, file)
      setAttachments((current) => ({ ...current, [requirement]: result }))
    } catch (e) {
      setError(e.message)
    } finally {
      setUploadingFor(null)
      pendingRequirement.current = null
    }
  }

  async function handleDelete() {
    if (!window.confirm('Delete this client and their match history?')) return
    try {
      await deleteClient(clientId)
      onDeleted?.()
    } catch (e) {
      setError(e.message)
    }
  }

  if (!client && !error) return <p className="loading">Loading…</p>

  const rows = DETAIL_ROWS.filter(([key]) => client?.[key])

  return (
    <>
      <button className="back-link" onClick={onBack}>
        ← All clients
      </button>

      {/* One input reused by every Attach button; the requirement it belongs
          to is held in a ref rather than rendering an input per row. */}
      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.docx,.xlsx,.pptx"
        style={{ display: 'none' }}
        onChange={(e) => {
          handleAttach(e.target.files?.[0])
          e.target.value = ''
        }}
      />

      <input
        ref={formRef}
        type="file"
        accept=".pdf,.docx,.xlsx,.pptx"
        style={{ display: 'none' }}
        onChange={(e) => {
          handleFormFile(e.target.files?.[0])
          e.target.value = ''
        }}
      />

      <header className="page-head">
        <div>
          <p className="eyebrow">Client</p>
          <h1>{client?.business_name}</h1>
          <p className="subtitle">
            {[client?.industry, client?.city, client?.state]
              .filter(Boolean)
              .join(' · ')}
          </p>
        </div>
      </header>

      <ClientWorkspacePanel me={me} clientId={clientId} />

      {error && <div className="banner bad">{error}</div>}

      {rows.length > 0 && (
        <div className="facts">
          {rows.map(([key, label]) => (
            <div className="fact" key={key}>
              <div className="fact-label">{label}</div>
              <div className="fact-value">{client[key]}</div>
            </div>
          ))}
        </div>
      )}

      {client?.categories?.length > 0 && (
        <div className="chip-row" style={{ marginTop: 14 }}>
          {client.categories.map((c) => (
            <span className="chip is-static" key={c}>
              {c}
            </span>
          ))}
        </div>
      )}

      {client?.risk_notes && (
        <div className="section">
          <div className="section-head">
            <h2>Broker notes</h2>
          </div>
          <p className="prose">{client.risk_notes}</p>
        </div>
      )}

      <div className="actions">
        <span className="hint">
          {match
            ? `Last matched ${new Date(match.created_at).toLocaleString()} · ${
                match.stats?.extracts_retrieved ?? 0
              } extracts from ${match.stats?.carriers_seen ?? 0} carrier(s)`
            : 'Searches every briefed carrier and ranks what fits.'}
        </span>
        <button className="btn-ghost" onClick={toggleProfile}>
          {showProfile ? 'Hide' : 'What the AI sees'}
        </button>
        <button className="btn-danger" onClick={handleDelete} disabled={running}>
          Delete
        </button>
        {carriers.length > 0 && (
          <select
            className="gap-carrier-picker"
            value={gapCarrier}
            onChange={(e) => setGapCarrier(e.target.value)}
            title="Which carrier's requirements to check against"
          >
            <option value="">carriers from the match</option>
            {carriers.map((c) => (
              <option key={c.carrier_id} value={c.carrier_id}>
                {c.name}
              </option>
            ))}
          </select>
        )}
        <button
          className="btn-ghost"
          onClick={handleGaps}
          disabled={checkingGaps || running}
        >
          {checkingGaps ? 'Checking…' : gaps ? 'Re-check paperwork' : 'Check paperwork'}
        </button>
        {draftKinds.length > 0 && (
          <select
            className="gap-carrier-picker"
            value={draftKind}
            onChange={(e) => setDraftKind(e.target.value)}
            title="What to write"
          >
            {draftKinds.map((k) => (
              <option key={k.id} value={k.id}>
                {k.label}
              </option>
            ))}
          </select>
        )}
        <button
          className="btn-ghost"
          onClick={handleDraft}
          disabled={drafting || running}
        >
          {drafting
            ? 'Working…'
            : draftKind === 'form'
              ? 'Upload form to fill'
              : 'Draft'}
        </button>
        <button className="btn-primary" onClick={handleMatch} disabled={running}>
          {running ? 'Matching…' : match ? 'Re-run match' : 'Find cover'}
        </button>
      </div>

      {showProfile && profile && (
        <div className="section">
          <div className="section-head">
            <h2>What the AI sees</h2>
            <span className="section-count">
              {profile.placeholders.length} redacted
            </span>
          </div>
          <pre className="profile-box">{profile.redacted_profile}</pre>
          <p className="progress-note">
            Identifying details are replaced before the text leaves the
            database. The map back to real values is never sent anywhere and is
            applied only to finished output.
          </p>
        </div>
      )}

      {running && (
        <div className="progress-wrap">
          <div className="progress-label">
            <span>Searching every carrier, then ranking…</span>
          </div>
          <div className="progress-track">
            <div className="progress-fill is-indeterminate" />
          </div>
          <p className="progress-note">
            Several searches run first, then one model call weighs them. On a
            local model this takes a minute or two.
          </p>
        </div>
      )}

      {(client?.documents_held?.length > 0 ||
        client?.documents_missing?.length > 0) && (
        <div className="section">
          <div className="section-head">
            <h2>Paperwork on file</h2>
            <span className="section-count">
              {client.documents_held?.length || 0} held ·{' '}
              {client.documents_missing?.length || 0} not held
            </span>
          </div>
          <div className="doc-chips">
            {(client.documents_held || []).map((d, i) => (
              <span className="doc-chip" key={`h-${i}`}>
                {d}
              </span>
            ))}
            {(client.documents_missing || []).map((d, i) => (
              <span className="doc-chip lacking" key={`m-${i}`}>
                {d}
              </span>
            ))}
          </div>
        </div>
      )}

      {checkingGaps && (
        <div className="progress-wrap">
          <div className="progress-label">
            <span>Checking the file against each carrier's requirements…</span>
          </div>
          <div className="progress-track">
            <div className="progress-fill is-indeterminate" />
          </div>
        </div>
      )}

      {drafting && (
        <div className="progress-wrap">
          <div className="progress-label">
            <span>Writing from the file…</span>
          </div>
          <div className="progress-track">
            <div className="progress-fill is-indeterminate" />
          </div>
        </div>
      )}

      {drafts.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>Drafted paperwork</h2>
            <span className="section-count">
              {drafts.length} draft{drafts.length === 1 ? '' : 's'}
            </span>
          </div>

          {drafts.map((draft) => (
            <div className="draft" key={draft.draft_id}>
              <div className="draft-head">
                <span className="draft-title">{draft.title}</span>
                <span className="pill">
                  {draftKinds.find((k) => k.id === draft.kind)?.label || draft.kind}
                </span>
                <span className={`pill ${draft.status === 'approved' ? 'ok' : ''}`}>
                  {draft.status}
                </span>
              </div>

              <div className="draft-meta">
                {[
                  draft.carrier_name,
                  draft.stats?.words ? `${draft.stats.words} words` : null,
                  draft.stats?.documents_enclosed != null
                    ? `${draft.stats.documents_enclosed} enclosed`
                    : null,
                  draft.stats?.outstanding
                    ? `${draft.stats.outstanding} still to follow`
                    : null,
                  new Date(draft.updated_at).toLocaleString(),
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </div>

              {(draft.warnings || []).map((w, i) => (
                <div className={`draft-warning ${w.level}`} key={i}>
                  {w.text}
                </div>
              ))}

              <div className="draft-notice">
                This is a draft. Nothing is sent from here — read it through,
                edit anything that needs it, and send it yourself.
              </div>

              {editingDraft === draft.draft_id ? (
                <>
                  <input
                    className="name-field"
                    style={{ marginBottom: 8 }}
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                  />
                  <textarea
                    className="draft-edit"
                    value={draftBody}
                    onChange={(e) => setDraftBody(e.target.value)}
                  />
                  <div className="actions">
                    <span className="hint" />
                    <button
                      className="btn-ghost"
                      onClick={() => setEditingDraft(null)}
                    >
                      Cancel
                    </button>
                    <button
                      className="btn-primary"
                      onClick={() => saveDraftEdit(draft.draft_id)}
                    >
                      Save
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className="draft-body">{draft.body}</div>

                  <div className="actions">
                    <span className="hint" />
                    <button
                      className="btn-ghost"
                      onClick={() => {
                        navigator.clipboard?.writeText(draft.body)
                      }}
                    >
                      Copy
                    </button>
                    <button
                      className="btn-ghost"
                      onClick={() => handleDownload(draft.draft_id)}
                      title={
                        draft.status === 'approved'
                          ? 'Download as Word'
                          : 'Downloads stamped as a draft'
                      }
                    >
                      Download .docx
                    </button>
                    <button
                      className="btn-danger"
                      onClick={() => removeDraft(draft.draft_id)}
                    >
                      Delete
                    </button>
                    <button
                      className="btn-ghost"
                      onClick={() => {
                        setEditingDraft(draft.draft_id)
                        setDraftTitle(draft.title)
                        setDraftBody(draft.body)
                      }}
                    >
                      Edit
                    </button>
                  </div>

                  {draft.status === 'approved' ? (
                    <div className="approved-banner">
                      <span>
                        Signed off
                        {approvedBy(histories[draft.draft_id]) &&
                          ` by ${approvedBy(histories[draft.draft_id])}`}
                        . You still send it yourself.
                      </span>
                      <button
                        className="btn-ghost"
                        onClick={() => handleRevoke(draft.draft_id)}
                      >
                        Withdraw sign-off
                      </button>
                    </div>
                  ) : (
                    <div className="approve-bar">
                      <input
                        className="approve-note"
                        placeholder="Note (optional) — what you checked"
                        value={approveNote}
                        onChange={(e) => setApproveNote(e.target.value)}
                      />
                      <button
                        className="btn-primary"
                        onClick={() => handleApprove(draft.draft_id)}
                        disabled={
                          (draft.warnings || []).some((w) => w.level === 'error')
                        }
                        title={
                          (draft.warnings || []).some((w) => w.level === 'error')
                            ? 'Fix the problem above first'
                            : undefined
                        }
                      >
                        Sign off
                      </button>
                    </div>
                  )}

                  <div className="history">
                    <button
                      className="history-toggle"
                      onClick={() => toggleHistory(draft.draft_id)}
                    >
                      {openHistory === draft.draft_id ? 'Hide' : 'Show'} history
                    </button>

                    {openHistory === draft.draft_id &&
                      (histories[draft.draft_id] || []).map((e, i) => (
                        <div className="history-row" key={i}>
                          <span className={`history-event ${e.event}`}>
                            {e.event}
                          </span>
                          <span className="history-when">
                            {new Date(e.at).toLocaleString()}
                          </span>
                          <span className="history-detail">
                            {[e.actor, e.detail].filter(Boolean).join(' — ')}
                          </span>
                        </div>
                      ))}
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {gaps?.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>Still needed before quoting</h2>
            <span className="section-count">
              {gaps.length} carrier{gaps.length === 1 ? '' : 's'}
            </span>
          </div>

          {(() => {
            // Everything outstanding across carriers, and how much of it has
            // been uploaded. Shown before the per-carrier detail so the first
            // thing on screen is "here is what to collect".
            const all = gaps.flatMap((r) => r.items || [])
            const needed = all.filter((i) => i.status !== 'held')
            const uploaded = needed.filter((i) => attachments[i.requirement])
            const scored = uploaded
              .map((i) => attachments[i.requirement]?.score)
              .filter((v) => v != null)
            const avg = scored.length
              ? Math.round(scored.reduce((a, b) => a + b, 0) / scored.length)
              : null
            return (
              <div className="collect-banner">
                <div className="collect-count">
                  {uploaded.length} / {needed.length}
                </div>
                <div style={{ marginTop: 4 }}>
                  documents collected of those still needed
                </div>
                <div className="collect-note">
                  {needed.length === 0
                    ? 'Everything is in. Draft the submission when you are ready.'
                    : uploaded.length === 0
                      ? 'Attach each document below and it will be read, checked against this client, and scored.'
                      : avg != null
                        ? `Average score so far ${avg}/100. Attach the rest below.`
                        : 'Attach the rest below.'}
                </div>
              </div>
            )
          })()}

          {gaps.map((report) => (
            <div key={report.report_id}>
              <div className="gap-carrier-head">
                <span className="gap-carrier-name">{report.carrier_name}</span>
                <span className="gap-counts">
                  {report.stats?.missing ?? 0} missing ·{' '}
                  {report.stats?.unclear ?? 0} to confirm ·{' '}
                  {report.stats?.held ?? 0} held
                </span>
              </div>

              {report.summary && (
                <p className="prose" style={{ fontSize: 14, marginBottom: 10 }}>
                  {report.summary}
                </p>
              )}

              <div className="listing">
                {/* Outstanding first: a checklist read top-down should start
                    with what still has to be done. */}
                {[...report.items]
                  .sort((a, b) => {
                    const order = { missing: 0, unclear: 1, held: 2 }
                    return order[a.status] - order[b.status]
                  })
                  .map((item, i) => (
                    <div
                      className={`gap-row ${
                        item.status === 'missing' ? 'is-missing' : ''
                      }`}
                      key={i}
                    >
                      <span className={`gap-status ${item.status}`}>
                        {item.status === 'held'
                          ? 'have it'
                          : item.status === 'missing'
                            ? 'missing'
                            : 'confirm'}
                      </span>
                      <div className="gap-main">
                        <div className="gap-req">{item.requirement}</div>
                        {item.reason && (
                          <div className="gap-reason">{item.reason}</div>
                        )}
                        {item.ask && <div className="gap-ask">{item.ask}</div>}
                        {(item.section_title || item.page_start) && (
                          <div className="listing-source">
                            {[
                              item.filename,
                              item.section_title,
                              item.page_start ? `p.${item.page_start}` : null,
                            ]
                              .filter(Boolean)
                              .join(' · ')}
                          </div>
                        )}

                        <div className="attach-row">
                          {item.status !== 'held' && (
                            <button
                              className="attach-btn"
                              onClick={() =>
                                handleOverride(
                                  report.report_id,
                                  item.requirement,
                                  'held',
                                )
                              }
                              title="Mark as held — recorded so the tool can learn from it"
                            >
                              Mark as held
                            </button>
                          )}
                          {item.status === 'held' && item.overridden && (
                            <span className="attachment-name">
                              set by hand
                            </span>
                          )}
                          <button
                            className="attach-btn"
                            onClick={() => pickFileFor(item.requirement)}
                            disabled={uploadingFor === item.requirement}
                          >
                            {uploadingFor === item.requirement
                              ? 'Checking…'
                              : attachments[item.requirement]
                                ? 'Replace'
                                : 'Attach document'}
                          </button>

                          {attachments[item.requirement] && (
                            <div className="attachment">
                              <div className="attachment-file">
                                <span className="attachment-name">
                                  {attachments[item.requirement].filename}
                                </span>
                                <span
                                  className={`verdict ${attachments[item.requirement].verdict}`}
                                >
                                  {VERDICT_LABEL[
                                    attachments[item.requirement].verdict
                                  ] || attachments[item.requirement].verdict}
                                </span>
                                {attachments[item.requirement].doc_type && (
                                  <span className="attachment-name">
                                    read as: {attachments[item.requirement].doc_type}
                                  </span>
                                )}
                              </div>

                              {attachments[item.requirement].score != null && (
                                <>
                                  <div
                                    className={`score ${scoreBand(
                                      attachments[item.requirement].score,
                                    )}`}
                                    style={{ marginTop: 6 }}
                                  >
                                    <span className="score-value">
                                      {attachments[item.requirement].score}
                                    </span>
                                    <span className="score-max">/ 100</span>
                                  </div>
                                  <div className="score-bars">
                                    {Object.entries(
                                      attachments[item.requirement].score_parts || {},
                                    ).map(([part, value]) => (
                                      <div className="score-bar" key={part}>
                                        <span className="score-bar-label">{part}</span>
                                        <span className="score-bar-track">
                                          <span
                                            className="score-bar-fill"
                                            style={{ width: `${value}%` }}
                                          />
                                        </span>
                                        <span className="score-bar-num">{value}</span>
                                      </div>
                                    ))}
                                  </div>
                                </>
                              )}

                              {attachments[item.requirement].reason && (
                                <div className="attachment-note ok">
                                  {attachments[item.requirement].reason}
                                </div>
                              )}

                              {(attachments[item.requirement].improvements || [])
                                .length > 0 && (
                                <div className="improvements">
                                  <div className="improvements-label">
                                    To improve this document
                                  </div>
                                  {attachments[item.requirement].improvements.map(
                                    (imp, n) => (
                                      <div className="improvement" key={n}>
                                        — {imp}
                                      </div>
                                    ),
                                  )}
                                </div>
                              )}

                              {(attachments[item.requirement].notes || []).map(
                                (note, n) => (
                                  <div
                                    className={`attachment-note ${note.level}`}
                                    key={n}
                                  >
                                    {note.text}
                                  </div>
                                ),
                              )}

                              {(attachments[item.requirement].key_facts || []).length >
                                0 && (
                                <div className="attachment-facts">
                                  {attachments[item.requirement].key_facts.map(
                                    (fact, f) => (
                                      <span className="attachment-fact" key={f}>
                                        {fact.label}: {fact.value}
                                      </span>
                                    ),
                                  )}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {match?.matches?.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>Recommended cover</h2>
            <span className="section-count">{match.matches.length}</span>
          </div>

          {match.matches.map((m, i) => (
            <div className="plan" key={`${m.carrier}-${m.plan}-${i}`}>
              <div className="plan-head">
                <span className="plan-name">{m.plan}</span>
                <span className={`pill ${FIT_CLASS[m.fit] || ''}`}>
                  {FIT_LABEL[m.fit] || m.fit}
                </span>
                <span className="pill">{m.category}</span>
              </div>
              <div className="card-meta" style={{ marginBottom: 7 }}>
                {m.carrier}
              </div>
              {m.why && <div className="plan-detail">{m.why}</div>}

              {m.watch_outs?.length > 0 && (
                <div className="plan-exclusions">
                  <div className="plan-exclusions-label">Watch out</div>
                  <ul>
                    {m.watch_outs.map((w, j) => (
                      <li key={j}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}

              {m.sources?.length > 0 && (
                <div className="plan-exclusions">
                  <div className="plan-exclusions-label">Based on</div>
                  {m.sources.map((s, j) => (
                    <div className="listing-source" key={j}>
                      {sourceLine(s)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {match?.gaps?.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>Not covered by any carrier</h2>
            <span className="section-count">{match.gaps.length}</span>
          </div>
          <div className="listing">
            {match.gaps.map((g, i) => (
              <div className="listing-item" key={i}>
                <div className="listing-text">{g}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {match?.questions?.length > 0 && (
        <div className="section">
          <div className="section-head">
            <h2>Ask the client</h2>
            <span className="section-count">{match.questions.length}</span>
          </div>
          <div className="listing">
            {match.questions.map((q, i) => (
              <div className="listing-item" key={i}>
                <div className="listing-text">{q}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
