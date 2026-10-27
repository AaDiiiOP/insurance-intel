import { useEffect, useState } from 'react'

import { createPackage, downloadFile, getPackage, getRenewal, listPackages, packageAction } from '../api'

/** Phase 2D (v0.17.0): renewal comparison (existing Phase 1C engine) and a
 * submission DRAFT with explicit readiness checks. The server decides every
 * state change; nothing is sent to an insurer. */
const STATE_CLASS = { DRAFT: '', NEEDS_INFORMATION: 'gold', READY_FOR_REVIEW: 'gold', APPROVED: 'ok', EXPORTED: 'ok' }

export default function RenewalPanel({ me, clientId }) {
  const can = (p) => me?.permissions?.includes(p)
  const [ren, setRen] = useState(null)
  const [pkgs, setPkgs] = useState([])
  const [pkg, setPkg] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const [reason, setReason] = useState('')

  useEffect(() => {
    getRenewal(clientId).then(setRen).catch((e) => setErr(e.message))
    listPackages(clientId).then((r) => { setPkgs(r.packages); if (r.packages[0]) getPackage(r.packages[0].package_id).then(setPkg) })
      .catch(() => {})
  }, [clientId])

  async function run(fn) {
    setBusy(true); setErr(null)
    try { setPkg(await fn()); setReason('') } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  if (!ren && !err) return <p className="loading">Loading renewal…</p>
  return (
    <div>
      {err && <div className="banner bad" role="alert">{err}</div>}
      {ren && (
        <div className="ci-card">
          <h3>Renewal comparison</h3>
          {ren.status !== 'compared' && <div className="banner info">{ren.reason}</div>}
          {(ren.policies || []).map((p) => (
            <div key={p.policy_id} className="ci-policy">
              <b>{p.policy_number || 'Policy'}</b> <span className="kx-meta">· {p.insurer || 'insurer not stated'}</span>
              <div className="kx-meta">
                Expiring {p.expiring_period ? `${p.expiring_period.from} → ${p.expiring_period.to}` : 'not stated'} ·
                Renewal {p.renewal_period ? `${p.renewal_period.from} → ${p.renewal_period.to}` : 'pending (not documented)'}
              </div>
              {p.comparison?.status === 'compared' ? p.comparison.changes.filter((c) => !c.change_type.endsWith('_unchanged')).map((c, i) => (
                <div key={i} className="kx-cov">
                  <span className={`pill ${c.change_type === 'unresolved_contradiction' ? 'bad' : c.change_type.endsWith('not_restated') || c.change_type === 'unknown' ? 'gold' : ''}`}>
                    {c.change_type.replace(/_/g, ' ')}</span>{' '}
                  <span className="kx-meta">{c.fact_type}{c.coverage ? ` (${c.coverage})` : ''}</span>{' '}
                  {c.old_value ?? '—'} → {c.new_value ?? '—'}
                  {c.note && <div className="kx-meta">{c.note}</div>}
                </div>
              )) : <div className="kx-meta">{p.comparison?.reason || 'No comparison available.'}</div>}
            </div>
          ))}
          {ren.business_changes && (
            <div className="kx-meta" style={{ marginTop: 8 }}>
              Business changes {ren.business_changes.from || ''} → {ren.business_changes.to || ''}: {JSON.stringify(ren.business_changes.summary || {})} ·{' '}
              {(ren.business_changes.not_reobserved || []).length} not re-observed — {ren.business_changes.not_reobserved_note}
            </div>
          )}
          {(ren.conflicts || []).map((c, i) => <div key={i} className="kx-bad">Conflict {c.key}: {(c.values || []).join(' vs ')}</div>)}
          <div className="kx-meta">{ren.note}</div>
        </div>
      )}

      <div className="ci-card">
        <h3>Submission draft</h3>
        {!pkg && can('drafts.create') && <button className="btn-primary" disabled={busy} onClick={() => run(() => createPackage(clientId))}>Prepare submission draft</button>}
        {!pkg && !can('drafts.create') && <span className="kx-meta">No submission draft yet.</span>}
        {pkg && (
          <>
            <div><span className={`pill ${STATE_CLASS[pkg.state]}`}>{pkg.state.replace(/_/g, ' ').toLowerCase()}</span>
              {pkg.approval.by && <span className="kx-meta"> · approved by {pkg.approval.by}{pkg.approval.current ? '' : ' (no longer current — content changed)'}</span>}</div>
            <div className="kx-meta">{pkg.note}</div>
            <h4 style={{ margin: '10px 0 4px' }}>Readiness checks</h4>
            <ul className="ci-attn">
              {pkg.readiness.checks.map((c) => (
                <li key={c.id}>
                  <span className={`pill ${c.passed ? 'ok' : c.exception ? 'gold' : 'bad'}`}>{c.passed ? 'pass' : c.exception ? 'exception' : 'blocked'}</span>{' '}
                  {c.rule}{c.fix && !c.exception ? <span className="kx-meta"> — {c.fix}</span> : null}
                  {c.exception && <span className="kx-meta"> — {c.exception.by}: “{c.exception.reason}”</span>}
                  {!c.passed && !c.exception && c.waivable && (can('facts.correct') || can('findings.review')) && (
                    <button className="link" style={{ marginLeft: 6 }} disabled={busy || !reason.trim()}
                      onClick={() => run(() => packageAction(pkg.package_id, 'exceptions', { check_id: c.id, reason }))}>record reviewed exception</button>
                  )}
                </li>
              ))}
            </ul>
            <textarea className="approve-note" rows={2} aria-label="Reason" placeholder="Reason (for exceptions and reopening)"
              value={reason} onChange={(e) => setReason(e.target.value)} style={{ width: '100%', maxWidth: 560 }} />
            <div className="actions">
              {['DRAFT', 'NEEDS_INFORMATION'].includes(pkg.state) && can('drafts.create') &&
                <button className="btn-ghost" disabled={busy} onClick={() => run(() => packageAction(pkg.package_id, 'check'))}>Run readiness check</button>}
              {pkg.state === 'READY_FOR_REVIEW' && can('drafts.approve') &&
                <button className="btn-primary" disabled={busy} onClick={() => run(() => packageAction(pkg.package_id, 'approve'))}>Approve draft</button>}
              {['APPROVED', 'EXPORTED'].includes(pkg.state) && can('export') &&
                <button className="btn-primary" disabled={busy} onClick={() => run(async () => {
                  await downloadFile(`/submission-packages/${pkg.package_id}/export`, 'submission_draft.docx')
                  return getPackage(pkg.package_id)
                })}>Export DOCX draft</button>}
              {pkg.state !== 'DRAFT' && <button className="btn-ghost" disabled={busy || !reason.trim()}
                onClick={() => run(() => packageAction(pkg.package_id, 'reopen', { reason }))}>Reopen</button>}
            </div>
            <details><summary className="kx-meta">History ({pkg.history.length})</summary>
              <ol className="rq-history">{pkg.history.map((h, i) => <li key={i}>{h.at?.slice(0, 19)} · {h.actor} · {h.action} {h.from_state ? `${h.from_state} → ` : ''}{h.to_state}{h.reason ? ` — “${h.reason}”` : ''}</li>)}</ol>
            </details>
          </>
        )}
      </div>
    </div>
  )
}
