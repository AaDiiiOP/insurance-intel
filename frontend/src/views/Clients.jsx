import { useEffect, useRef, useState } from 'react'

import { createClient, extractClient, listClients } from '../api'

const CATEGORIES = [
  'fire',
  'marine',
  'liability',
  'burglary',
  'personal accident',
  'motor',
  'health',
]

// Which fields are redacted before any model sees them. Shown in the form so
// the team knows what leaves the database and what does not, rather than
// having to trust an invisible rule.
const SENSITIVE = new Set([
  'business_name',
  'pan',
  'gstin',
  'contact_name',
  'contact_phone',
  'contact_email',
])

const FIELDS = [
  { key: 'business_name', label: 'Business name', required: true },
  { key: 'entity_type', label: 'Entity type', placeholder: 'Private Limited, LLP, Partnership…' },
  { key: 'industry', label: 'Industry / trade', placeholder: 'Hardware retail, small fabrication…' },
  { key: 'city', label: 'City' },
  { key: 'state', label: 'State' },
  { key: 'turnover', label: 'Annual turnover', placeholder: '4.2 crore' },
  { key: 'years_trading', label: 'Years trading', placeholder: '7' },
  { key: 'employee_count', label: 'Employees', placeholder: '18' },
  { key: 'sum_insured', label: 'Sum insured sought', placeholder: '85 lakh' },
  { key: 'pan', label: 'PAN', placeholder: 'AAFCS1234K' },
  { key: 'gstin', label: 'GSTIN' },
  { key: 'contact_name', label: 'Contact name' },
  { key: 'contact_phone', label: 'Contact phone' },
  { key: 'contact_email', label: 'Contact email' },
]

const EMPTY = Object.fromEntries(FIELDS.map((f) => [f.key, '']))

function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    })
  } catch {
    return ''
  }
}

export default function Clients({ refreshKey, onOpen, onCreated }) {
  const [clients, setClients] = useState(null)
  const [error, setError] = useState(null)
  const [adding, setAdding] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState(EMPTY)
  const [categories, setCategories] = useState([])
  const [notes, setNotes] = useState('')
  const [reading, setReading] = useState(false)
  // Which fields arrived from a document rather than being typed. Marked in
  // the form so the broker knows what to check rather than what to trust.
  const [prefilled, setPrefilled] = useState(new Set())
  const [readSummary, setReadSummary] = useState(null)
  const fileRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    listClients()
      .then((d) => !cancelled && setClients(d.clients))
      .catch((e) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [refreshKey])

  function set(key, value) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  function toggleCategory(category) {
    setCategories((current) =>
      current.includes(category)
        ? current.filter((c) => c !== category)
        : [...current, category],
    )
  }

  function reset() {
    setForm(EMPTY)
    setCategories([])
    setNotes('')
    setAdding(false)
    setError(null)
    setPrefilled(new Set())
    setReadSummary(null)
  }

  async function readDocument(file) {
    if (!file) return
    setReading(true)
    setError(null)
    setReadSummary(null)
    try {
      const result = await extractClient(file)

      // Merge rather than replace: anything already typed wins, so uploading
      // a second document to fill the gaps never wipes a correction.
      const filled = new Set(prefilled)
      setForm((current) => {
        const next = { ...current }
        for (const [key, value] of Object.entries(result.fields || {})) {
          if (value && !next[key]) {
            next[key] = value
            filled.add(key)
          }
        }
        return next
      })
      setPrefilled(filled)

      if (result.categories?.length) {
        setCategories((current) => [
          ...current,
          ...result.categories.filter((c) => !current.includes(c)),
        ])
      }
      if (result.risk_notes) {
        setNotes((current) =>
          current ? `${current}\n\n${result.risk_notes}` : result.risk_notes,
        )
      }

      setReadSummary({
        filename: result.filename,
        count: Object.values(result.fields || {}).filter(Boolean).length,
        truncated: result.truncated,
        recognised: result.recognised !== false,
      })
    } catch (e) {
      setError(e.message)
    } finally {
      setReading(false)
    }
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const created = await createClient({
        ...form,
        categories,
        risk_notes: notes,
      })
      reset()
      onCreated?.()
      onOpen?.(created.client_id)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  if (adding) {
    return (
      <>
        <button className="back-link" onClick={reset}>
          ← All clients
        </button>

        <header className="page-head">
          <div>
            <p className="eyebrow">Intake</p>
            <h1>New client</h1>
            <p className="subtitle">
              Filled in by you on the client's behalf. Save what you have — a
              half-complete picture after a first call is still worth keeping.
            </p>
          </div>
        </header>

        {error && <div className="banner bad">{error}</div>}

        {readSummary && !readSummary.recognised && (
          <div className="banner bad">
            {readSummary.filename} does not look like a client submission or
            proposal form — very little could be read from it. Check you
            uploaded the right file. Anything it did find is below; the rest is
            yours to fill in.
          </div>
        )}

        {readSummary && readSummary.recognised && (
          <div className="banner good">
            Read {readSummary.count} field(s) from {readSummary.filename}. Check
            them before saving — anything the document did not state is left
            blank.
            {readSummary.truncated &&
              ' The document was long, so only the first part was read.'}
          </div>
        )}

        <div className="drop-slot">
          <button
            type="button"
            className="dropzone"
            onClick={() => fileRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              if (e.dataTransfer.files?.[0]) readDocument(e.dataTransfer.files[0])
            }}
            disabled={reading}
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
              {reading
                ? 'Reading the document…'
                : 'Have their submission or proposal form? Drop it here to fill this in'}
            </p>
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.docx,.xlsx,.pptx"
            style={{ display: 'none' }}
            onChange={(e) => {
              readDocument(e.target.files?.[0])
              e.target.value = ''
            }}
          />
        </div>

        <div className="field-grid">
          {FIELDS.map((field) => (
            <div className="name-field" key={field.key}>
              <label htmlFor={field.key}>
                {field.label}
                {field.required && ' *'}
                {prefilled.has(field.key) && (
                  <span className="field-tag from-doc">from doc</span>
                )}
                {SENSITIVE.has(field.key) && (
                  <span className="field-tag">redacted</span>
                )}
              </label>
              <input
                id={field.key}
                type="text"
                value={form[field.key]}
                placeholder={field.placeholder || ''}
                onChange={(e) => set(field.key, e.target.value)}
              />
            </div>
          ))}
        </div>

        <div className="section">
          <div className="section-head">
            <h2>Cover requested</h2>
          </div>
          <div className="chip-row">
            {CATEGORIES.map((category) => (
              <button
                key={category}
                type="button"
                className={`chip ${categories.includes(category) ? 'is-on' : ''}`}
                onClick={() => toggleCategory(category)}
              >
                {category}
              </button>
            ))}
          </div>
        </div>

        <div className="section">
          <div className="section-head">
            <h2>Broker notes</h2>
          </div>
          <p className="subtitle" style={{ marginBottom: 10 }}>
            What you learned on the call — how they operate, what they are
            worried about, anything unusual about the premises or the stock.
            This is what the matching leans on most.
          </p>
          <textarea
            className="edit-area"
            value={notes}
            placeholder="Stock is kept in a basement. Two units, one with a commercial kitchen…"
            onChange={(e) => setNotes(e.target.value)}
          />
        </div>

        <div className="actions">
          <span className="hint">
            Names, PAN, GSTIN and contact details are replaced with
            placeholders before anything reaches the model.
          </span>
          <button className="btn-ghost" onClick={reset} disabled={saving}>
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={save}
            disabled={saving || !form.business_name.trim()}
          >
            {saving ? 'Saving…' : 'Save client'}
          </button>
        </div>
      </>
    )
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Clients</p>
          <h1>Clients</h1>
          <p className="subtitle">
            Businesses you are placing cover for. Open one to match it against
            every carrier you have briefed.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setAdding(true)}>
          + New client
        </button>
      </header>

      {error && <div className="banner bad">{error}</div>}
      {clients === null && !error && <p className="loading">Loading…</p>}

      {clients?.length === 0 && (
        <div className="empty">
          <p>No clients yet.</p>
          <button className="btn-primary" onClick={() => setAdding(true)}>
            Add the first one
          </button>
        </div>
      )}

      {clients?.length > 0 && (
        <div className="card-grid">
          {clients.map((client) => (
            <button
              key={client.client_id}
              className="card"
              onClick={() => onOpen(client.client_id)}
            >
              <h3 className="card-title">{client.business_name}</h3>
              <div className="card-meta">
                {client.industry && <span>{client.industry}</span>}
                {client.city && <span>· {client.city}</span>}
              </div>
              <div className="card-meta" style={{ marginTop: 9 }}>
                {(client.categories || []).slice(0, 3).map((c) => (
                  <span className="pill" key={c}>
                    {c}
                  </span>
                ))}
                {client.last_matched_at ? (
                  <span className="pill gold">
                    Matched {formatDate(client.last_matched_at)}
                  </span>
                ) : (
                  <span className="pill">Not matched</span>
                )}
              </div>
            </button>
          ))}
        </div>
      )}
    </>
  )
}
