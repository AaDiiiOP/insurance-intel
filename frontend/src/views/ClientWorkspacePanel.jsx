import { useEffect, useRef, useState } from 'react'

import {
  analyzeSubmission,
  downloadFile,
  getClientWorkspace,
  linkClientBusiness,
  uploadIntelligenceDocument,
} from '../api'

const STATUS_LABEL = {
  parsed: 'read',
  failed: 'could not be read',
  unchecked: 'saved, not checked',
}

/**
 * Phase 2A — the client's document library and its link to the business that
 * the intelligence engine builds from these documents. Uploading here keeps
 * the original file (downloadable by authorized users); analysis is a
 * separate, explicit step that runs the existing Knowledge Core pipeline.
 */
export default function ClientWorkspacePanel({ me, clientId, onChanged }) {
  const can = (p) => me?.permissions?.includes(p)
  const [ws, setWs] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null)
  const fileRef = useRef(null)

  function load() {
    setError(null)
    getClientWorkspace(clientId).then(setWs).catch((e) => setError(e.message))
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [clientId])

  async function run(label, fn) {
    setBusy(label)
    setError(null)
    try {
      await fn()
      load()
      if (onChanged) onChanged()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  if (!ws && !error) return <p className="loading">Loading documents…</p>
  return (
    <div className="section" id="client-documents">
      <div className="section-head"><h2>Documents &amp; intelligence</h2></div>
      {error && <div className="banner bad">{error}</div>}
      {ws && (
        <>
          <div className="kx-meta" style={{ marginBottom: 8 }}>
            {ws.client.assigned_to ? `Assigned to ${ws.client.assigned_to.display_name}` : 'Not assigned'}
            {Object.keys(ws.client.field_provenance || {}).length > 0 &&
              ` · read from a document, not yet confirmed: ${Object.keys(ws.client.field_provenance).join(', ')}`}
          </div>

          <div className="kx-prow">
            <div className="kx-plabel">Business intelligence</div>
            <div>
              {ws.business ? (
                <span>Linked to <b>{ws.business.name}</b> — open it in the Knowledge Core for the full profile.</span>
              ) : ws.suggested_business ? (
                <span>
                  Found <b>{ws.suggested_business.name}</b> ({ws.suggested_business.basis}).{' '}
                  {can('clients.edit') && (
                    <button className="btn-ghost kx-small" disabled={!!busy}
                      onClick={() => run('link', () => linkClientBusiness(clientId, ws.suggested_business.business_id))}>
                      Link to this client
                    </button>
                  )}
                </span>
              ) : (
                <span className="kx-meta">No business built yet. Upload policy documents, then run analysis.</span>
              )}
            </div>
          </div>

          <div className="kx-prow">
            <div className="kx-plabel">Library</div>
            <div>
              {ws.documents.length === 0 && <span className="kx-meta">No documents yet.</span>}
              {ws.documents.map((d) => (
                <div className="ws-doc" key={`${d.kind}-${d.document_id}`}>
                  <div>
                    <b>{d.filename}</b>{' '}
                    <span className="pill">{d.kind === 'intelligence' ? 'for intelligence' : `for: ${d.requirement}`}</span>{' '}
                    <span className="kx-meta">{STATUS_LABEL[d.status] || d.status}{d.error ? ` — ${d.error}` : ''}</span>
                  </div>
                  <div>
                    {d.downloadable && can('evidence.view') ? (
                      <button className="btn-ghost kx-small" disabled={!!busy}
                        onClick={() => run('download', () => downloadFile(d.download_url, d.filename))}>
                        Download original
                      </button>
                    ) : (
                      <span className="kx-meta">original not stored</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {can('documents.upload') && (
            <div className="actions">
              <input ref={fileRef} type="file" accept=".pdf,.docx,.xlsx,.pptx" style={{ display: 'none' }}
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  e.target.value = ''
                  if (f) run('upload', () => uploadIntelligenceDocument(clientId, f))
                }} />
              <button className="btn-ghost" disabled={!!busy} onClick={() => fileRef.current?.click()}>
                {busy === 'upload' ? 'Uploading…' : '+ Upload policy / schedule / survey'}
              </button>
              {can('analysis.run') && ws.submissions.length > 0 && (
                <button className="btn-primary" disabled={!!busy}
                  onClick={() => run('analyze', () => analyzeSubmission(ws.submissions[0].submission_id))}>
                  {busy === 'analyze' ? 'Analysing (uses the local model)…' : 'Run analysis'}
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
