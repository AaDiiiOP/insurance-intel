import { useEffect, useState } from 'react'

import { getClientIntelligence } from '../api'
import BusinessPanel from './BusinessPanel'
import CoveragePanel from './CoveragePanel'
import { EvidenceRow } from './KnowledgeExplorer'
import PolicyPanel from './PolicyPanel'
import RenewalPanel from './RenewalPanel'

/**
 * Phase 2B (v0.15.0) — unified client intelligence on the client page.
 *
 * Overview comes from GET /clients/{id}/intelligence (a read-only summary the
 * backend builds from the existing Phase 1C–1E engines for the business linked
 * to this client). The other tabs reuse the existing Business, Policy and
 * Coverage panels with the same ids — nothing is recomputed in the browser.
 * The server decides what this user may see; buttons are hidden only for
 * convenience.
 */

const TABS = [
  ['overview', 'Overview'],
  ['business', 'Business & risks'],
  ['policies', 'Policies'],
  ['coverage', 'Coverage findings'],
  ['conflicts', 'Conflicts & changes'],
  ['documents', 'Documents'],
  ['renewal', 'Renewal & submission'],
]

const STATE = {
  verified: ['ok', 'Verified by a person'],
  read_by_rule: ['ok', 'Read by rule'],
  unverified: ['gold', 'Not yet verified'],
  conflicting: ['bad', 'Conflicting'],
  missing: ['', 'Not found in the documents'],
}

const SECTION_LABEL = {
  available: null,
  not_linked: 'Not linked to an analyzed business yet',
  not_analyzed: 'Not analyzed yet',
  none_found: 'None found',
  insufficient: 'Not enough dated documents to compare',
  unavailable: 'Could not be loaded',
}

const FIELD_LABEL = {
  legal_name: 'Legal name', workforce: 'Workforce', survey_risk_rating: 'Survey risk rating',
  policy_number: 'Policy number', insurer: 'Insurer', policy_limit: 'Policy limit', sum_insured: 'Sum insured',
  deductible: 'Deductible', sublimits: 'Sublimits', exclusions: 'Exclusions', conditions: 'Conditions',
  endorsements: 'Endorsements',
}

function StatePill({ state }) {
  const [cls, label] = STATE[state] || ['', state]
  return <span className={`pill ${cls}`}>{label}</span>
}

function SectionStatus({ s }) {
  if (!s) return null
  const label = SECTION_LABEL[s.status]
  if (!label) return null
  return (
    <div className={`banner ${s.status === 'unavailable' ? 'bad' : 'info'}`}>
      {label}
      {s.reason ? ` — ${s.reason}` : ''}
      {s.error ? ` — ${s.error}` : ''}
    </div>
  )
}

function shown(answer) {
  if (answer == null) return null
  return Array.isArray(answer) ? answer.join(' · ') : String(answer)
}

/** One value with its state, every conflicting value, and its evidence. */
function Value({ label, v, onError }) {
  const [open, setOpen] = useState(false)
  if (!v) return null
  const text = v.state === 'conflicting'
    ? (v.values || []).join(' vs ')
    : shown(v.answer) ?? 'Not found in the documents'
  return (
    <div className="ci-row">
      <div className="ci-label">{label}</div>
      <div className="ci-value">
        <span className={v.state === 'conflicting' ? 'kx-bad' : v.answer == null ? 'kx-meta' : 'kx-value'}>{text}</span>{' '}
        <StatePill state={v.state} />
        {v.observed_on && <span className="kx-meta"> · observed {v.observed_on}</span>}
        {v.evidence?.length > 0 ? (
          <button className="link ci-evlink" onClick={() => setOpen(!open)}>
            {open ? 'hide evidence' : `evidence (${v.evidence.length})`}
          </button>
        ) : (
          v.answer != null && <span className="kx-meta"> · no evidence recorded</span>
        )}
        {open && v.evidence.map((e, i) => <EvidenceRow key={i} ev={e} onError={onError} />)}
      </div>
    </div>
  )
}

function Counts({ obj, cls = {} }) {
  const entries = Object.entries(obj || {})
  if (!entries.length) return <span className="kx-meta">none</span>
  return entries.map(([k, n]) => (
    <span key={k} className={`pill ${cls[k] || ''}`} style={{ marginRight: 6 }}>
      {k.replace(/_/g, ' ').toLowerCase()}: {n}
    </span>
  ))
}

const MAP_CLASS = { MAPPED: 'ok', PARTIALLY_MAPPED: 'gold', UNMAPPED: 'bad', CONFLICTING: 'bad', UNCERTAIN: 'gold', NEEDS_REVIEW: 'gold' }

function Overview({ d, go, onError }) {
  const s = d.sections
  const prof = s.profile || {}
  const pol = s.policies || {}
  const cov = s.coverage || {}
  const docs = s.documents || {}
  const prov = prof.provenance
  return (
    <div>
      {d.attention.length > 0 ? (
        <div className="ci-card">
          <h3>Needs attention</h3>
          <ul className="ci-attn">
            {d.attention.map((a, i) => (
              <li key={i}>
                <span className={`pill ${a.kind === 'conflict' || a.kind === 'processing_error' || a.kind === 'unavailable' ? 'bad' : a.kind === 'missing' ? '' : 'gold'}`}>
                  {a.kind.replace(/_/g, ' ')}
                </span>{' '}
                {a.text}{' '}
                {a.tab !== 'overview' && <button className="link" onClick={() => go(a.tab)}>open</button>}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="ci-card"><span className="kx-meta">Nothing flagged for review.</span></div>
      )}

      <div className="ci-grid">
        <div className="ci-card">
          <h3>Business</h3>
          <SectionStatus s={prof} />
          {prof.fields && Object.entries(prof.fields).map(([k, v]) => (
            <Value key={k} label={FIELD_LABEL[k] || k} v={v} onError={onError} />
          ))}
          {prof.counts && (
            <div className="ci-row">
              <div className="ci-label">Documented</div>
              <div className="ci-value">
                {Object.entries(prof.counts).map(([k, n]) => (
                  <span key={k} className="ci-count">{k.replace(/_/g, ' ')} <b>{n}</b></span>
                ))}
                {prof.missing_sections?.length > 0 && (
                  <div className="kx-meta">Not found in the documents: {prof.missing_sections.map((x) => x.replace(/_/g, ' ')).join(', ')}.</div>
                )}
              </div>
            </div>
          )}
          {prov && (
            <div className="kx-meta" style={{ marginTop: 6 }}>
              {prov.facts} fact(s) behind this view ·{' '}
              {Object.entries(prov.by_source).map(([k, n]) => `${n} ${k}`).join(' · ')}
              {prov.without_evidence > 0 && ` · ${prov.without_evidence} without evidence`}
            </div>
          )}
        </div>

        <div className="ci-card">
          <h3>Policies</h3>
          <SectionStatus s={pol} />
          {(pol.items || []).map((p) => (
            <div key={p.policy_id} className="ci-policy">
              <div>
                <b>{shown(p.fields.policy_number.answer) || p.name}</b>{' '}
                <span className={`pill ${p.period_status === 'current' ? 'ok' : ''}`}>{(p.period_status || 'period unknown').replace(/_/g, ' ')}</span>
                {p.conflicts > 0 && <span className="pill bad" style={{ marginLeft: 6 }}>{p.conflicts} conflict(s)</span>}
              </div>
              <div className="kx-meta">
                {p.periods.length ? p.periods.map((x) => `${x.from} → ${x.to}`).join(', ') : 'No policy period found in the documents'}
                {p.coverages.length > 0 && ` · ${p.coverages.join(', ')}`}
              </div>
              <Value label="Insurer" v={p.fields.insurer} onError={onError} />
            </div>
          ))}
          {pol.items?.length > 0 && <button className="link" onClick={() => go('policies')}>Open policies</button>}
        </div>

        <div className="ci-card">
          <h3>Coverage findings</h3>
          <SectionStatus s={cov} />
          {cov.status === 'available' && (
            <>
              <div className="ci-row"><div className="ci-label">Mapping</div><div className="ci-value"><Counts obj={cov.mapping_status_counts} cls={MAP_CLASS} /></div></div>
              <div className="ci-row"><div className="ci-label">Findings</div><div className="ci-value"><Counts obj={cov.finding_counts} /></div></div>
              <div className="ci-row"><div className="ci-label">Review</div><div className="ci-value"><Counts obj={cov.review_counts} cls={{ OPEN: 'gold' }} /></div></div>
              <div className="kx-meta">{cov.note}</div>
              <button className="link" onClick={() => go('coverage')}>Open coverage findings</button>
            </>
          )}
        </div>

        <div className="ci-card">
          <h3>Documents</h3>
          <SectionStatus s={docs} />
          {docs.status === 'available' && (
            <>
              <div className="ci-row"><div className="ci-label">For intelligence</div><div className="ci-value">{docs.intelligence_documents} <Counts obj={docs.by_status} /></div></div>
              <div className="ci-row"><div className="ci-label">Client paperwork</div><div className="ci-value">{docs.paperwork_documents} <Counts obj={docs.paperwork_verdicts} /></div></div>
              {docs.errors.length > 0 && <div className="banner bad">{docs.errors.length} document(s) failed processing.</div>}
            </>
          )}
        </div>
      </div>

      <p className="kx-meta" style={{ marginTop: 8 }}>
        <StatePill state="verified" /> checked by a person · <StatePill state="read_by_rule" /> read from a labelled line ·{' '}
        <StatePill state="unverified" /> extracted, not yet checked · <StatePill state="conflicting" /> documents disagree ·{' '}
        <StatePill state="missing" /> not stated in the documents. {d.note}
      </p>
    </div>
  )
}

function Policies({ d, asOf }) {
  const pol = d.sections.policies || {}
  const items = pol.items || []
  const [pick, setPick] = useState(items[0]?.policy_id || '')
  useEffect(() => { if (!items.find((p) => p.policy_id === pick)) setPick(items[0]?.policy_id || '') }, [items, pick])
  if (pol.status !== 'available') return <SectionStatus s={pol} />
  const cur = items.find((p) => p.policy_id === pick)
  return (
    <div>
      {items.length > 1 && (
        <div className="actions" style={{ marginTop: 0 }}>
          {items.map((p) => (
            <button key={p.policy_id} className={p.policy_id === pick ? 'btn-primary' : 'btn-ghost'} onClick={() => setPick(p.policy_id)}>
              {shown(p.fields.policy_number.answer) || p.name}
            </button>
          ))}
        </div>
      )}
      {cur && <PolicyPanel policyId={cur.policy_id} asOf={asOf} />}
    </div>
  )
}

function ConflictsChanges({ d, onError }) {
  const c = d.sections.conflicts || {}
  const ch = d.sections.changes || {}
  const all = [...(c.business || []).map((x) => ({ ...x, scope: 'Business' })),
    ...(c.policies || []).map((x) => ({ ...x, scope: 'Policy' }))]
  return (
    <div>
      <div className="ci-card">
        <h3>Conflicting values</h3>
        <SectionStatus s={c} />
        {c.status === 'available' && all.length === 0 && <span className="kx-meta">No unresolved conflict between documents.</span>}
        {all.map((x, i) => (
          <div key={i} className="ci-policy">
            <span className="pill bad">conflict</span> <span className="kx-meta">{x.scope}</span> <b>{x.key}</b>:{' '}
            {(x.values || []).join(' vs ')}
            <div className="kx-meta">All values are kept; a person decides (Knowledge explorer → fact review).</div>
            {(x.evidence || []).map((e, j) => <EvidenceRow key={j} ev={e} onError={onError} />)}
          </div>
        ))}
      </div>
      <div className="ci-card">
        <h3>Changes between document dates</h3>
        <SectionStatus s={ch} />
        {ch.status === 'available' && (
          <>
            <div className="kx-meta">{ch.from} → {ch.to}</div>
            <Counts obj={ch.summary} cls={{ conflicting: 'bad', added: 'ok' }} />
            {ch.not_reobserved > 0 && (
              <div className="kx-meta" style={{ marginTop: 6 }}>
                {ch.not_reobserved} value(s) were not restated in the later documents. That does not mean they were removed.
              </div>
            )}
            <div className="kx-meta">Each change with its evidence is listed under Business &amp; risks.</div>
          </>
        )}
      </div>
    </div>
  )
}

function Documents({ d }) {
  const docs = d.sections.documents || {}
  return (
    <div className="ci-card">
      <SectionStatus s={docs} />
      {docs.status === 'available' && (
        <>
          <div className="ci-row"><div className="ci-label">For intelligence</div><div className="ci-value">{docs.intelligence_documents} <Counts obj={docs.by_status} /></div></div>
          <div className="ci-row"><div className="ci-label">Client paperwork</div><div className="ci-value">{docs.paperwork_documents} <Counts obj={docs.paperwork_verdicts} /></div></div>
          {docs.errors.map((e) => (
            <div key={e.document_id} className="banner bad">{e.filename}: {e.error}</div>
          ))}
        </>
      )}
      <button className="link" onClick={() => document.getElementById('client-documents')?.scrollIntoView({ behavior: 'smooth' })}>
        Go to the document library (upload, download, run analysis, link business)
      </button>
    </div>
  )
}

export default function ClientIntelligence({ me, clientId, refreshKey, initialTab }) {
  const can = (p) => me?.permissions?.includes(p)
  const [tab, setTab] = useState(initialTab || 'overview')
  const [asOf, setAsOf] = useState('')
  const [d, setD] = useState(null)
  const [err, setErr] = useState(null)
  const [evErr, setEvErr] = useState(null)

  useEffect(() => {
    let live = true
    setErr(null)
    getClientIntelligence(clientId, asOf || undefined)
      .then((r) => live && setD(r))
      .catch((e) => live && setErr(e))
    return () => { live = false }
  }, [clientId, asOf, refreshKey])

  const bid = d?.business?.business_id
  return (
    <div className="section" id="client-intelligence">
      <div className="section-head ci-head">
        <h2>Client intelligence</h2>
        <label className="ci-asof">
          As of{' '}
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          {asOf && <button className="btn-ghost kx-small" onClick={() => setAsOf('')}>Today</button>}
        </label>
      </div>

      {err && (
        <div className="banner bad" role="alert">
          {err.status === 403 ? 'Your role cannot view this client’s intelligence.' : err.status === 404 ? 'Client not found in your organization.' : err.message}
        </div>
      )}
      {!d && !err && <p className="loading">Loading client intelligence…</p>}

      {d && (
        <>
          {d.historical && <div className="banner info">Showing what the documents said as of {d.as_of} (historical view).</div>}
          <div className="ci-tabs" role="tablist">
            {TABS.map(([k, label]) => (
              <button key={k} role="tab" aria-selected={tab === k} className={`ci-tab ${tab === k ? 'on' : ''}`} onClick={() => setTab(k)}>
                {label}
              </button>
            ))}
          </div>
          {evErr && <div className="banner bad">{evErr}</div>}
          <div role="tabpanel">
            {tab === 'overview' && <Overview d={d} go={setTab} onError={setEvErr} />}
            {tab === 'business' && (bid ? <BusinessPanel businessId={bid} asOf={asOf} /> : <SectionStatus s={d.sections.profile} />)}
            {tab === 'policies' && <Policies d={d} asOf={asOf} />}
            {tab === 'coverage' && (bid
              ? <CoveragePanel businessId={bid} asOf={asOf} canReview={can('findings.review')} />
              : <SectionStatus s={d.sections.coverage} />)}
            {tab === 'conflicts' && <ConflictsChanges d={d} onError={setEvErr} />}
            {tab === 'documents' && <Documents d={d} />}
            {tab === 'renewal' && <RenewalPanel me={me} clientId={clientId} />}
          </div>
        </>
      )}
    </div>
  )
}
