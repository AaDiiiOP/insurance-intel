import { useEffect, useState } from 'react'

import { getCoverageAudit, reviewCoverageFinding } from '../api'

/**
 * Business Coverage Audit (Phase 1E, v0.13.0) — shown under the Business panel.
 * Read-only except for broker review decisions. Every row shows its evidence.
 * Wording comes from the backend: it reports what documents state and how the
 * stated amounts compare; it never says a business is (under)insured.
 */

function Ev({ items }) {
  const ev = (items || []).filter((e) => e && e.document_filename)
  if (!ev.length) return null
  const seen = new Set()
  const uniq = ev.filter((e) => {
    const k = `${e.document_filename}|${e.page}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
  return (
    <div className="kx-meta">
      {uniq.slice(0, 4).map((e, i) => (
        <span key={i} title={e.snippet || ''}>
          {i ? ' · ' : ''}
          {e.document_filename} p{e.page ?? '?'}
        </span>
      ))}
    </div>
  )
}

const STATUS_CLASS = {
  MAPPED: 'ok', PARTIALLY_MAPPED: 'gold', UNMAPPED: 'bad', CONFLICTING: 'bad', UNCERTAIN: 'gold',
  NEEDS_REVIEW: 'gold', NOT_APPLICABLE: '',
}
const DECISIONS = ['REVIEWED', 'ACCEPTED', 'REJECTED', 'NEEDS_MORE_EVIDENCE']

function Row({ label, children }) {
  return (
    <div className="kx-prow">
      <div className="kx-plabel">{label}</div>
      <div>{children}</div>
    </div>
  )
}

export default function CoveragePanel({ businessId, asOf, canReview = true }) {
  const [a, setA] = useState(null)
  const [err, setErr] = useState(null)
  const [reviewer, setReviewer] = useState('')
  const [busy, setBusy] = useState(null)

  function load() {
    setErr(null)
    getCoverageAudit(businessId, asOf || undefined).then(setA).catch((e) => setErr(e.message))
  }
  useEffect(() => {
    setA(null)
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [businessId, asOf])

  async function decide(f, decision) {
    setBusy(f.finding_id)
    try {
      await reviewCoverageFinding(f.finding_id, businessId, decision, reviewer.trim())
      load()
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(null)
    }
  }

  if (err && !a) return <div className="banner bad">{err}</div>
  if (!a) return <p className="subtitle">Loading coverage audit…</p>
  const m = a.coverage_map
  const findings = a.findings || []
  const byType = (t) => findings.filter((f) => t.includes(f.type))
  return (
    <div className="kx-block kx-policy">
      <h3>Coverage audit</h3>
      <div className="kx-meta">as of {a.as_of} · {a.note}</div>
      {err && <div className="banner bad">{err}</div>}

      <Row label="Policies in force">
        {m.policies_in_force.length === 0 ? (
          <span className="kx-meta">No policy period in the available documents includes this date.</span>
        ) : (
          m.policies_in_force.map((p) => (
            <div key={p.policy_id}>
              {p.policy_number || p.name} <span className="kx-meta">({p.periods.map((x) => `${x.from} → ${x.to}`).join(', ')})</span>
            </div>
          ))
        )}
      </Row>

      <Row label="Risks / exposures">
        <span className="kx-meta">
          {a.risks.length} documented risk(s){a.risks.length ? `: ${a.risks.map((r) => r.name).join(' · ')}` : ''} ·{' '}
          {a.exposures.length} explicit exposure(s)
        </span>
      </Row>

      <Row label="Coverage mapping">
        {m.mappings.length === 0 && <span className="kx-meta">Nothing documented to map on this date.</span>}
        {m.mappings.map((r) => (
          <div key={r.mapping_id} className="kx-cov">
            <span className={`pill ${STATUS_CLASS[r.status] || ''}`}>{r.status}</span>{' '}
            <b>{r.unit.name}</b>
            {r.unit.amount != null && <span className="kx-meta"> ({r.unit.amount.toLocaleString('en-US')})</span>} →{' '}
            {r.coverage ? `${r.coverage.name} (${r.policy.policy_number || r.policy.name})` : 'no coverage identified'}
            {r.mapping_type && <span className="kx-meta"> · {r.mapping_type}</span>}
            <div className="kx-meta">{r.limit_comparison ? r.limit_comparison.statement : r.rationale}</div>
            <Ev items={r.evidence} />
          </div>
        ))}
        {m.coverages_without_documented_exposure.length > 0 && (
          <div className="kx-meta">
            Coverages with no documented related exposure:{' '}
            {m.coverages_without_documented_exposure.map((c) => `${c.coverage}${c.limit ? ` (${c.limit})` : ''}`).join(' · ')}
          </div>
        )}
      </Row>

      <Row label="Deductibles">
        {m.deductibles.map((d, i) => (
          <div key={i}>
            {d.value ?? (d.review_status === 'contradiction' ? `Stated differently: ${d.values.join(' vs ')}` : 'Not stated')}{' '}
            <span className="kx-meta">· {d.applicability}</span>
            <Ev items={d.evidence} />
          </div>
        ))}
      </Row>

      <Row label="Endorsements">
        {m.endorsements.filter((e) => e.endorsement_id).map((e, i) => (
          <div key={i}>
            {e.endorsement_id} — effective {e.effective_from || 'date not readable'}{' '}
            <span className={`pill ${e.in_effect_on_date ? 'ok' : ''}`}>{e.in_effect_on_date ? 'in effect' : 'not in effect'}</span>
          </div>
        ))}
        {m.endorsements.filter((e) => e.text).map((e, i) => (
          <div key={`t${i}`} className="kx-meta">
            {e.text}
            {e.relates_to_exposure.length > 0 && ` (relates to: ${e.relates_to_exposure.join(', ')})`}
          </div>
        ))}
      </Row>

      <Row label="Findings for review">
        <div className="kx-meta" style={{ marginBottom: 6 }}>
          {canReview ? 'Decisions are recorded under your signed-in name.' : 'Your role can read findings but not record review decisions.'}
        </div>
        {findings.length === 0 && <span className="kx-meta">No finding on this date.</span>}
        {[
          ['Limit comparisons', ['EXPOSURE_EXCEEDS_LIMIT', 'LIMIT_EXCEEDS_EXPOSURE']],
          ['Unmapped / uncertain', ['UNMAPPED_RISK', 'UNMAPPED_EXPOSURE', 'UNCERTAIN_MAPPING']],
          ['Exclusions', ['EXCLUSION_REVIEW']],
          ['Conditions', ['CONDITION_REVIEW']],
          ['Sublimits / deductibles', ['SUBLIMIT_REVIEW', 'DEDUCTIBLE_REVIEW']],
          ['Conflicts', ['COVERAGE_CONFLICT', 'POLICY_PERIOD_CONFLICT']],
          ['Missing evidence', ['MISSING_EVIDENCE']],
        ].map(([title, types]) => {
          const list = byType(types)
          if (!list.length) return null
          return (
            <div key={title} style={{ marginBottom: 8 }}>
              <div className="kx-plabel">{title}</div>
              {list.map((f) => (
                <div key={f.finding_id} className="kx-cov">
                  <span className={`pill ${f.review_status === 'OPEN' ? 'gold' : f.review_status === 'REJECTED' ? '' : 'ok'}`}>
                    {f.review_status}
                  </span>{' '}
                  <span className="kx-meta">{f.type}</span> {f.statement}
                  {f.review && (
                    <div className="kx-meta">
                      {f.review.decision} by {f.review.reviewer} at {f.review.at}
                    </div>
                  )}
                  {f.review_stale && <div className="kx-meta">An earlier review applied to different evidence.</div>}
                  <Ev items={f.evidence} />
                  {canReview && <div className="kx-actions">
                    {DECISIONS.map((d) => (
                      <button key={d} className="btn-ghost kx-small" disabled={busy === f.finding_id} onClick={() => decide(f, d)}>
                        {d.replace(/_/g, ' ').toLowerCase()}
                      </button>
                    ))}
                  </div>}
                </div>
              ))}
            </div>
          )
        })}
      </Row>

      <Row label="Changes">
        {!a.changes && <span className="kx-meta">Shown for the current date only.</span>}
        {a.changes && a.changes.status !== 'compared' && <span className="kx-meta">{a.changes.reason}</span>}
        {a.changes && a.changes.status === 'compared' && (
          <>
            <div className="kx-meta">
              {a.changes.from} → {a.changes.to}
            </div>
            {a.changes.changes.map((c, i) => (
              <div key={i} className="kx-cov">
                <span className={`pill ${c.kind === 'status_changed' ? 'gold' : ''}`}>{c.kind.replace(/_/g, ' ')}</span> {c.unit} →{' '}
                {c.coverage || '—'}
                {c.kind === 'status_changed' && (
                  <span className="kx-meta">
                    {' '}
                    {c.old_limit_state} → {c.new_limit_state}
                  </span>
                )}
              </div>
            ))}
            {a.changes.policy_changes.map((c, i) => (
              <div key={`p${i}`} className="kx-meta">
                {c.change_type ? `${c.change_type}${c.coverage ? ` (${c.coverage})` : ''}: ${c.old_value ?? ''} → ${c.new_value ?? ''}` : c.reason}
                {c.impact && ` — ${c.impact} [${c.status}]`}
              </div>
            ))}
          </>
        )}
      </Row>
    </div>
  )
}
