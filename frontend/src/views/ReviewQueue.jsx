import { useEffect, useState } from 'react'

import { getReviewItem, getReviewQueue, reviewAction } from '../api'
import { EvidenceRow } from './KnowledgeExplorer'

/**
 * Phase 2C (v0.16.0) — review queue.
 *
 * Items come from GET /review-queue, which first brings the queue in line with
 * what the engines report (conflicts, unverified facts, facts without
 * evidence, coverage findings, failed documents). Every decision is carried
 * out on the server by the existing Knowledge Core / coverage services; the
 * buttons shown are the server's own list of allowed actions for this user.
 */

const KIND_LABEL = {
  fact_conflict: 'Conflict',
  unverified_fact: 'Unverified fact',
  missing_evidence: 'No evidence',
  coverage_finding: 'Coverage finding',
  failed_document: 'Failed document',
}
const STATE_CLASS = { OPEN: 'gold', REOPENED: 'gold', ASSIGNED: '', IN_REVIEW: '', NEEDS_INFO: 'gold', RESOLVED: 'ok', DISMISSED: '' }
const DECISION_LABEL = {
  verify: 'Verify (evidence matches)', correct: 'Correct the value', reject: 'Reject the fact',
  ACCEPTED: 'Accept finding', REVIEWED: 'Mark reviewed', REJECTED: 'Reject (dismiss) finding',
  NEEDS_MORE_EVIDENCE: 'Needs more evidence', resolved: 'Resolved', dismissed: 'Dismiss',
}

function Pill({ state }) {
  return <span className={`pill ${STATE_CLASS[state] || ''}`}>{state.replace('_', ' ').toLowerCase()}</span>
}

function Subject({ d, onError, choice, setChoice }) {
  const s = d.subject
  if (!s.available) return <div className="banner info">The engines no longer report this item (it may have been settled elsewhere).</div>
  if (s.competing_values) {
    return (
      <div>
        <p className="kx-meta">{s.rule}</p>
        {s.competing_values.map((f) => (
          <label key={f.fact_id} className="rq-choice">
            <input type="radio" name="competing" checked={choice === f.fact_id} onChange={() => setChoice(f.fact_id)}
              disabled={!d.allowed_actions.includes('decide:correct')} />
            <span>
              <b>{f.value}</b> <span className="kx-meta">· {f.status.replace('_', ' ')}
                {f.effective_from ? ` · stated for ${f.effective_from}` : ''}{f.current ? '' : ' · historical row'}</span>
              {f.evidence.map((e, i) => <EvidenceRow key={i} ev={e} onError={onError} />)}
              {f.evidence.length === 0 && <span className="kx-meta"> · no evidence recorded</span>}
            </span>
          </label>
        ))}
      </div>
    )
  }
  if (s.fact) {
    const f = s.fact
    return (
      <div>
        <div><span className="kx-meta">{f.fact_type}</span> <b>{f.value}</b> <span className="kx-meta">· {f.status.replace('_', ' ')}</span></div>
        {f.evidence.map((e, i) => <EvidenceRow key={i} ev={e} onError={onError} />)}
        {f.evidence.length === 0 && <div className="banner bad">No evidence recorded — this value cannot be verified.</div>}
      </div>
    )
  }
  if (s.finding) {
    const f = s.finding
    return (
      <div>
        <div><span className="kx-meta">{f.type}</span> {f.statement}</div>
        {f.conclusion && <div className="kx-meta">{f.conclusion}</div>}
        {f.review_stale && <div className="banner bad">An earlier decision applied to different evidence — it needs a new review.</div>}
        {f.evidence_sufficient === false && <div className="banner info">The engine marked the evidence as insufficient.</div>}
        {s.evidence.map((e, i) => <EvidenceRow key={i} ev={e} onError={onError} />)}
        {(f.review_history || []).length > 0 && (
          <div className="kx-meta" style={{ marginTop: 6 }}>
            Coverage decisions: {f.review_history.map((h) => `${h.decision} by ${h.reviewer}${h.applies_to_current_evidence ? '' : ' (earlier evidence)'}`).join(' · ')}
          </div>
        )}
      </div>
    )
  }
  if (s.document) {
    return <div><b>{s.document.filename}</b> <span className="kx-meta">· {s.document.status}</span><div className="banner bad">{s.document.error || 'No error message recorded.'}</div></div>
  }
  return null
}

function Detail({ itemId, me, onChanged, onOpenClient }) {
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const [reason, setReason] = useState('')
  const [value, setValue] = useState('')
  const [choice, setChoice] = useState('')
  const [assignee, setAssignee] = useState('')

  function load() {
    setErr(null)
    getReviewItem(itemId).then((r) => { setD(r); setAssignee(r.item.assignee?.user_id || '') }).catch((e) => setErr(e))
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setD(null); setReason(''); setValue(''); setChoice(''); load() }, [itemId])

  async function act(action, body = {}) {
    setBusy(true)
    setErr(null)
    try {
      const r = await reviewAction(itemId, action, body)
      setD(r)
      setReason('')
      setValue('')
      onChanged()
    } catch (e) {
      setErr(e)
    } finally {
      setBusy(false)
    }
  }

  if (err && !d) {
    return <div className="banner bad" role="alert">{err.status === 404 ? 'Review item not found in your organization.' : err.status === 403 ? 'Your role cannot open this item.' : err.message}</div>
  }
  if (!d) return <p className="loading">Loading item…</p>
  const it = d.item
  const can = (a) => d.allowed_actions.includes(a)
  const decisions = Object.entries(d.decisions).filter(([k]) => can(`decide:${k}`))
  return (
    <div className="rq-detail">
      <div className="rq-detail-head">
        <span className="pill">{KIND_LABEL[it.kind]}</span> <Pill state={it.state} />
        {it.assignee && <span className="kx-meta"> · assigned to {it.assignee.display_name}</span>}
      </div>
      <h3 style={{ margin: '8px 0' }}>{it.title}</h3>
      <div className="kx-meta">
        {it.client_id ? (
          <button className="link" onClick={() => onOpenClient(it.client_id, it.client_tab)}>
            Open {it.client_name || 'client'} → {it.client_tab} tab
          </button>
        ) : 'Not linked to a client yet.'}
      </div>
      <div className="banner info" style={{ marginTop: 8 }}>{d.note}</div>
      {err && <div className="banner bad" role="alert">{err.message}</div>}

      <div className="ci-card"><h3>System finding and evidence</h3>
        <Subject d={d} onError={(m) => setErr(new Error(m))} choice={choice} setChoice={setChoice} />
      </div>

      <div className="ci-card">
        <h3>Your action</h3>
        {d.allowed_actions.length === 0 && <p className="kx-meta">No action available to your role on this item in its current state.</p>}
        {(can('assign') || can('unassign')) && (
          <div className="actions" style={{ marginTop: 0 }}>
            <select aria-label="Assign to" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              <option value="">— unassigned —</option>
              {d.assignable.map((u) => <option key={u.user_id} value={u.user_id}>{u.display_name} ({u.role})</option>)}
            </select>
            <button className="btn-ghost" disabled={busy || (!assignee && !can('unassign'))}
              onClick={() => act('assign', { user_id: assignee || null })}>{assignee ? 'Assign' : 'Unassign'}</button>
          </div>
        )}
        {can('start') && <button className="btn-primary" disabled={busy} onClick={() => act('start')}>Start review</button>}
        {(decisions.length > 0 || can('needs_info') || can('reopen')) && (
          <div className="rq-form">
            {d.decisions.correct && can('decide:correct') && (
              <input className="approve-note" aria-label="Corrected value"
                placeholder={it.kind === 'fact_conflict' ? 'Value (leave empty to keep the chosen value)' : 'Corrected value'}
                value={value} onChange={(e) => setValue(e.target.value)} />
            )}
            <textarea className="approve-note" rows={2} aria-label="Reason" placeholder="Reason (required for corrections, rejections, dismissals, more-info and reopening)"
              value={reason} onChange={(e) => setReason(e.target.value)} />
            <div className="actions" style={{ marginTop: 6 }}>
              {decisions.map(([k, meta]) => (
                <button key={k} className={k === 'verify' || k === 'ACCEPTED' || k === 'resolved' ? 'btn-primary' : 'btn-ghost'}
                  disabled={busy || (meta.reason_required && !reason.trim()) || (it.kind === 'fact_conflict' && !choice)}
                  onClick={() => act('decide', { decision: k, reason, value: value || null, fact_id: choice || null })}>
                  {DECISION_LABEL[k] || k}
                </button>
              ))}
              {can('needs_info') && <button className="btn-ghost" disabled={busy || !reason.trim()} onClick={() => act('needs-info', { reason })}>Needs more information</button>}
              {can('reopen') && <button className="btn-ghost" disabled={busy || !reason.trim()} onClick={() => act('reopen', { reason })}>Reopen</button>}
            </div>
            {it.kind === 'fact_conflict' && can('decide:correct') && !choice && <div className="kx-meta">Choose the value that is right first.</div>}
          </div>
        )}
      </div>

      <div className="ci-card">
        <h3>History</h3>
        <ol className="rq-history">
          {d.history.map((h, i) => (
            <li key={i}>
              <span className="kx-meta">{new Date(h.at).toLocaleString()}</span> · <b>{h.by_system ? 'System' : h.actor}</b> · {h.action}
              {h.from_state ? ` ${h.from_state} → ${h.to_state}` : ` → ${h.to_state}`}
              {h.decision && <> · decision <b>{h.decision}</b></>}
              {h.reason && <div className="kx-meta">“{h.reason}”</div>}
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}

export default function ReviewQueue({ me, onOpenClient }) {
  const [filters, setFilters] = useState({ state: 'open', kind: '', assigned: '' })
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let live = true
    setErr(null)
    getReviewQueue({ ...filters, page, page_size: 50 })
      .then((r) => live && setData(r))
      .catch((e) => live && setErr(e))
    return () => { live = false }
  }, [filters, page, tick])

  const set = (k) => (e) => { setPage(1); setFilters({ ...filters, [k]: e.target.value }) }
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1
  return (
    <div>
      <header className="page-head">
        <div>
          <p className="eyebrow">{me.organization.name}</p>
          <h1>Review queue</h1>
          <p className="subtitle">Conflicts, unverified facts, missing evidence, coverage findings and failed documents — each decided by a person, with history.</p>
        </div>
      </header>
      <div className="rq-filters">
        <label>Status <select value={filters.state} onChange={set('state')}>
          <option value="open">Open</option><option value="closed">Closed</option><option value="">All</option>
          {['OPEN', 'ASSIGNED', 'IN_REVIEW', 'NEEDS_INFO', 'REOPENED', 'RESOLVED', 'DISMISSED'].map((s) => <option key={s} value={s}>{s.replace('_', ' ').toLowerCase()}</option>)}
        </select></label>
        <label>Kind <select value={filters.kind} onChange={set('kind')}>
          <option value="">All</option>
          {Object.entries(KIND_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select></label>
        <label>Assigned <select value={filters.assigned} onChange={set('assigned')}>
          <option value="">Anyone</option><option value="me">To me</option><option value="unassigned">Unassigned</option>
        </select></label>
      </div>
      {err && <div className="banner bad" role="alert">{err.status === 403 ? 'Your role cannot see the review queue.' : err.message}</div>}
      {!data && !err && <p className="loading">Refreshing the queue from the engines…</p>}
      {data && (
        <div className="rq-layout">
          <div className="rq-list" role="list">
            <div className="kx-meta" style={{ marginBottom: 6 }}>
              {data.total} item(s){data.refresh && (data.refresh.created || data.refresh.reopened || data.refresh.closed)
                ? ` · just now: ${data.refresh.created} new, ${data.refresh.reopened} reopened, ${data.refresh.closed} closed by the system` : ''}
            </div>
            {data.items.length === 0 && <div className="ci-card"><span className="kx-meta">Nothing here. {filters.state === 'open' ? 'No open review items for these filters.' : ''}</span></div>}
            {data.items.map((i) => (
              <button key={i.item_id} role="listitem" className={`rq-row ${openId === i.item_id ? 'on' : ''}`} onClick={() => setOpenId(i.item_id)}>
                <div><span className="pill">{KIND_LABEL[i.kind]}</span> <Pill state={i.state} /></div>
                <div className="rq-title">{i.title}</div>
                <div className="kx-meta">{i.client_name || 'no client'}{i.assignee ? ` · ${i.assignee.display_name}` : ' · unassigned'}</div>
              </button>
            ))}
            {pages > 1 && (
              <div className="actions">
                <button className="btn-ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
                <span className="kx-meta">page {page} of {pages}</span>
                <button className="btn-ghost" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
              </div>
            )}
          </div>
          <div className="rq-pane">
            {openId ? <Detail itemId={openId} me={me} onChanged={() => setTick((t) => t + 1)} onOpenClient={onOpenClient} />
              : <div className="ci-card"><span className="kx-meta">Select an item to see its evidence, history and the actions open to you.</span></div>}
          </div>
        </div>
      )}
    </div>
  )
}
