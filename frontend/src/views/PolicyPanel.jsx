import { useEffect, useState } from 'react'

import { comparePolicy, getPolicy } from '../api'

/**
 * Policy Intelligence panel (Phase 1C, v0.11.0) — shown in the Knowledge
 * explorer when a policy entity is open. Read-only: every value comes from
 * GET /policies/{id}, i.e. the same rules as the query layer. A value is
 * shown only with its evidence; a section that has none says why.
 */

function Ev({ items }) {
  const ev = (items || []).flatMap((f) => f.evidence || [])
  if (!ev.length) return null
  return (
    <div className="kx-meta">
      {ev.slice(0, 3).map((e, i) => (
        <span key={i} title={e.snippet || ''}>
          {i ? ' · ' : ''}
          {e.document_filename} p{e.page ?? '?'}
        </span>
      ))}
    </div>
  )
}

function Row({ label, view }) {
  if (!view) return null
  const a = view.answer
  const shown = a == null ? null : Array.isArray(a) ? (a.length ? a.join(' · ') : '—') : a
  return (
    <div className="kx-prow">
      <div className="kx-plabel">{label}</div>
      <div>
        <span className={view.review_status === 'contradiction' ? 'kx-bad' : 'kx-value'}>
          {shown ?? (view.review_status === 'contradiction' ? 'Contradiction — needs review' : 'Not stated / needs review')}
        </span>
        {shown == null && view.reason && <div className="kx-meta">{view.reason}</div>}
        <Ev items={view.facts_used} />
      </div>
    </div>
  )
}

export default function PolicyPanel({ policyId, asOf }) {
  const [p, setP] = useState(null)
  const [cmp, setCmp] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    setP(null)
    setCmp(null)
    setErr(null)
    getPolicy(policyId, asOf || undefined).then(setP).catch((e) => setErr(e.message))
  }, [policyId, asOf])

  async function compare() {
    try {
      setCmp(await comparePolicy(policyId, null, asOf))
    } catch (e) {
      setErr(e.message)
    }
  }

  if (err) return <div className="banner bad">{err}</div>
  if (!p) return <p className="subtitle">Loading policy…</p>
  return (
    <div className="kx-block kx-policy">
      <h3>
        Policy {p.policy_number.answer || ''}{' '}
        <span className={`pill ${p.period.status === 'current' ? 'ok' : p.period.status === 'expired' ? 'bad' : ''}`}>
          {p.period.status}
        </span>
      </h3>
      <div className="kx-meta">
        as of {p.as_of}
        {p.period.known.length ? ` · periods: ${p.period.known.map((k) => `${k.from} → ${k.to}`).join(', ')}` : ''}
      </div>
      <Row label="Insurer" view={p.insurer} />
      <Row label="Policy number" view={p.policy_number} />
      <Row label="Policy limit" view={p.policy_limit} />
      <Row label="Deductible" view={p.deductible} />
      {p.sum_insured && <Row label="Sum insured" view={p.sum_insured} />}
      <div className="kx-prow">
        <div className="kx-plabel">Coverages</div>
        <div>
          {p.coverages.length === 0 && <span className="kx-meta">No coverage with its own stated limit.</span>}
          {p.coverages.map((c) => (
            <div key={c.coverage_id} className="kx-cov">
              <b>{c.name}</b>:{' '}
              <span className={c.limit.review_status === 'contradiction' ? 'kx-bad' : 'kx-value'}>
                {c.limit.answer ?? (c.limit.review_status === 'contradiction' ? 'Contradiction — needs review' : 'Not stated')}
              </span>
              <Ev items={c.limit.facts_used} />
            </div>
          ))}
        </div>
      </div>
      <Row label="Coverages listed" view={p.coverages_listed} />
      <Row label="Exclusions" view={p.exclusions} />
      <Row label="Conditions" view={p.conditions} />
      <Row label="Definitions" view={p.definitions} />
      <Row label="Sub-limits" view={p.sublimits} />
      <Row label="Endorsements" view={p.endorsements} />
      <div className="kx-prow">
        <div className="kx-plabel">Endorsement ids</div>
        <div>
          {p.endorsement_refs.length === 0 && <span className="kx-meta">None recorded.</span>}
          {p.endorsement_refs.map((r) => (
            <div key={r.fact_id}>
              {r.value} — effective {r.effective_from || 'date not readable'}{' '}
              <span className={`pill ${r.in_effect_on_date ? 'ok' : ''}`}>{r.in_effect_on_date ? 'in effect' : 'not in effect'}</span>
              <Ev items={[r]} />
            </div>
          ))}
        </div>
      </div>
      <div className="kx-prow">
        <div className="kx-plabel">Documents</div>
        <div className="kx-meta">{p.documents.map((d) => `${d.role}: ${d.name.split(' — ').pop()}`).join(' · ') || 'None linked.'}</div>
      </div>
      {p.conflicts.length > 0 && (
        <div className="banner bad">
          Unresolved conflicts: {p.conflicts.map((c) => `${c.coverage || c.fact_type}: ${c.values.join(' vs ')}`).join('; ')}
        </div>
      )}
      <button className="btn-ghost kx-small" onClick={compare}>Compare with previous period</button>
      {cmp && (
        <div className="kx-meta" style={{ marginTop: 6 }}>
          {cmp.status !== 'compared'
            ? cmp.reason
            : cmp.changes.filter((c) => !c.change_type.endsWith('_unchanged')).map((c, i) => (
                <div key={i}>
                  {c.change_type}{c.coverage ? ` (${c.coverage})` : ''}: {c.old_value ?? ''}{c.old_value && c.new_value ? ' → ' : ''}{c.new_value ?? ''}
                  {c.percentage_change != null ? ` (${c.percentage_change > 0 ? '+' : ''}${c.percentage_change}%)` : ''}
                </div>
              ))}
        </div>
      )}
    </div>
  )
}
