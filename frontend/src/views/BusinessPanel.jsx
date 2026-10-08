import { useEffect, useState } from 'react'

import { getBusinessChanges, getBusinessProfile } from '../api'

/**
 * Business Risk panel (Phase 1D, v0.12.0) — shown in the Knowledge explorer
 * when a business entity is open. Read-only: GET /businesses/{id}/profile and
 * /changes. Only what the documents state, each value with its document and
 * page. Nothing is inferred and no risk score is computed.
 */

function Ev({ facts }) {
  const ev = (facts || []).flatMap((f) => f.evidence || [])
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

function Val({ view }) {
  if (!view) return null
  const bad = view.review_status === 'contradiction'
  const a = view.answer
  const shown = a == null ? null : Array.isArray(a) ? a.join(' · ') : a
  return (
    <>
      <span className={bad ? 'kx-bad' : 'kx-value'}>
        {shown ?? (bad ? `Contradiction — needs review (${(view.values || []).join(' vs ')})` : 'Not stated')}
      </span>
      {view.observed_on && <span className="kx-meta"> · observed {view.observed_on}</span>}
      <Ev facts={view.facts_used} />
    </>
  )
}

function Section({ label, items, render, empty }) {
  return (
    <div className="kx-prow">
      <div className="kx-plabel">{label}</div>
      <div>
        {items.length === 0 && <span className="kx-meta">{empty || 'Not documented.'}</span>}
        {items.map((it) => (
          <div key={it.entity_id} className="kx-cov">{render(it)}</div>
        ))}
      </div>
    </div>
  )
}

const KIND_CLASS = { conflicting: 'bad', added: 'ok', changed: '', unchanged: '' }

export default function BusinessPanel({ businessId, asOf }) {
  const [p, setP] = useState(null)
  const [ch, setCh] = useState(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    setP(null)
    setCh(null)
    setErr(null)
    getBusinessProfile(businessId, asOf || undefined).then(setP).catch((e) => setErr(e.message))
    getBusinessChanges(businessId).then(setCh).catch(() => setCh(null))
  }, [businessId, asOf])

  if (err) return <div className="banner bad">{err}</div>
  if (!p) return <p className="subtitle">Loading business…</p>
  // Locations, operations, risks… : the entity name IS the stated value.
  const named = (it) => (
    <>
      <b>{it.name}</b>
      {it.value?.observed_on && <span className="kx-meta"> · observed {it.value.observed_on}</span>}
      <Ev facts={it.value?.facts_used} />
    </>
  )
  return (
    <div className="kx-block kx-policy">
      <h3>Business risk profile</h3>
      <div className="kx-meta">as of {p.as_of} · {p.note}</div>

      <div className="kx-prow"><div className="kx-plabel">Legal name</div><div><Val view={p.legal_name} /></div></div>
      <div className="kx-prow"><div className="kx-plabel">Workforce</div><div><Val view={p.workforce} /></div></div>
      <div className="kx-prow"><div className="kx-plabel">Surveyor risk rating</div><div><Val view={p.survey_risk_rating} /></div></div>

      <Section label="Locations" items={p.locations} render={named} />
      <Section label="Operations" items={p.operations} render={named} />
      <Section label="Assets (declared value)" items={p.assets} render={(it) => <><b>{it.name}</b>: <Val view={it.value} /></>} />
      <Section label="Exposures" items={p.exposures} render={named} empty="No exposure stated in the documents." />
      <Section label="Risk factors" items={p.risk_factors} render={named} empty="No risk factor stated." />
      <Section label="Risks" items={p.risks} render={named} empty="No risk stated in the documents (none inferred)." />
      <Section label="Controls" items={p.controls} render={(it) => <><b>{it.name}</b>: <Val view={it.value} /></>} />
      <Section
        label="Claims / losses"
        items={p.claims}
        render={(it) => (
          <>
            <div>Claims: <Val view={it.claims_count} /></div>
            <div>Total incurred: <Val view={it.total_incurred} /></div>
          </>
        )}
        empty="No loss run recorded."
      />
      <div className="kx-prow">
        <div className="kx-plabel">Policies</div>
        <div className="kx-meta">{p.policies.map((x) => x.name).join(' · ') || 'None linked.'}</div>
      </div>

      {p.conflicts.length > 0 && (
        <div className="banner bad">
          Unresolved conflicts: {p.conflicts.map((c) => `${c.key}: ${c.values.join(' vs ')}`).join('; ')}
        </div>
      )}

      <div className="kx-prow">
        <div className="kx-plabel">Changes</div>
        <div>
          {!ch && <span className="kx-meta">Loading…</span>}
          {ch && ch.status !== 'compared' && <span className="kx-meta">{ch.reason}</span>}
          {ch && ch.status === 'compared' && (
            <>
              <div className="kx-meta">
                {ch.from} → {ch.to} · {ch.summary.material} material change(s)
              </div>
              {ch.changes.map((c, i) => (
                <div key={i} className="kx-cov">
                  <span className={`pill ${KIND_CLASS[c.kind]}`}>{c.kind}</span> {c.key}
                  {c.kind === 'conflicting' && `: ${(c.values || []).join(' vs ')}`}
                  {(c.kind === 'changed') && `: ${c.old_value} → ${c.new_value}`}
                  {(c.kind === 'added' || c.kind === 'unchanged') && !c.key.endsWith(c.new_value) && `: ${c.new_value}`}
                  {c.percentage_change != null && ` (${c.percentage_change > 0 ? '+' : ''}${c.percentage_change}%)`}
                  <Ev facts={[{ evidence: [...(c.old_evidence || []), ...(c.new_evidence || [])] }]} />
                </div>
              ))}
              {ch.not_reobserved.length > 0 && (
                <div className="kx-meta">
                  Not re-observed (not treated as removed): {ch.not_reobserved.map((n) => n.key || n.fact_type).join(' · ')}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
