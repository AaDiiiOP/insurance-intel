const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Which organization the app acts as, for the doc-intelligence tenant
// boundary. null means "no header sent" — the backend then falls back to
// its own default organization, exactly matching every request made before
// this existed. Set via setActiveOrganization(), called from the org
// switcher in the Doc Intelligence screen.
let activeOrganizationId = null

export function setActiveOrganization(organizationId) {
  activeOrganizationId = organizationId || null
}

export function getActiveOrganization() {
  return activeOrganizationId
}

/**
 * One place that turns a non-2xx response into a thrown Error carrying the
 * backend's own `detail` message. Every view can then just catch and show
 * err.message, rather than each one re-deriving what went wrong.
 */
async function request(path, options = {}) {
  let response
  const headers = new Headers(options.headers || {})
  // Sent on every request once an organization is selected — this is the
  // one place that needs to know about it, so nothing else in api.js (or
  // any view) has to pass it through by hand.
  if (activeOrganizationId) {
    headers.set('X-Organization-Id', activeOrganizationId)
  }

  try {
    response = await fetch(`${BASE}${path}`, { ...options, headers })
  } catch {
    throw new Error(
      'Could not reach the backend. Is it running? (docker-compose up --build)',
    )
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Non-JSON error body — keep the status-code message.
    }
    throw new Error(detail)
  }

  return response.json()
}

export function getHealth() {
  return request('/health')
}

export function listCarriers() {
  return request('/carriers')
}

export function getCarrier(carrierId) {
  return request(`/carriers/${carrierId}`)
}

export function deleteCarrier(carrierId) {
  return request(`/carriers/${carrierId}`, { method: 'DELETE' })
}

/**
 * `staged` is the array of { file } objects held by the Upload view.
 * Sent as multipart/form-data — the browser sets the boundary header itself,
 * so no Content-Type is passed here.
 */
export function createCarrier(staged, name) {
  const form = new FormData()
  for (const item of staged) form.append('files', item.file)
  if (name) form.append('name', name)
  return request('/carriers', { method: 'POST', body: form })
}

export function createSummary(carrierId) {
  return request(`/carriers/${carrierId}/summary`, { method: 'POST' })
}

export function getSummary(carrierId) {
  return request(`/carriers/${carrierId}/summary`)
}

export function getSummaryProgress(carrierId) {
  return request(`/carriers/${carrierId}/summary/progress`)
}

export function editSummary(carrierId, summary) {
  return request(`/carriers/${carrierId}/summary`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ summary }),
  })
}

export function getDocumentText(documentId) {
  return request(`/documents/${documentId}/text`)
}

// ---- Phase 2 --------------------------------------------------------------

export function listClients() {
  return request('/clients')
}

export function getClient(clientId) {
  return request(`/clients/${clientId}`)
}

export function createClient(body) {
  return request('/clients', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function updateClient(clientId, body) {
  return request(`/clients/${clientId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function deleteClient(clientId) {
  return request(`/clients/${clientId}`, { method: 'DELETE' })
}

/** What the model is actually shown for this client — never the token map. */
export function getRedactedProfile(clientId) {
  return request(`/clients/${clientId}/profile`)
}

export function runMatch(clientId) {
  return request(`/clients/${clientId}/match`, { method: 'POST' })
}

export function getMatch(clientId) {
  return request(`/clients/${clientId}/match`)
}

export function askQuestion(question, carrierId) {
  return request('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, carrier_id: carrierId || null }),
  })
}

export function reindex() {
  return request('/reindex', { method: 'POST' })
}

/**
 * Read a client's own document into proposed intake fields.
 * Returns a draft only — nothing is saved until the form is submitted.
 */
export function extractClient(file) {
  const form = new FormData()
  form.append('file', file)
  return request('/clients/extract', { method: 'POST', body: form })
}

// ---- Phase 3 --------------------------------------------------------------

export function runGapAnalysis(clientId, carrierId) {
  const query = carrierId ? `?carrier_id=${carrierId}` : ''
  return request(`/clients/${clientId}/gaps${query}`, { method: 'POST' })
}

export function listCarriersForPicker() {
  return request('/carriers')
}

export function getGapAnalysis(clientId) {
  return request(`/clients/${clientId}/gaps`)
}

export function setClientDocuments(clientId, held, missing) {
  return request(`/clients/${clientId}/documents`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ held, missing }),
  })
}

// ---- Phase 3.5: documents the client actually sent -------------------------

export function uploadClientDocument(clientId, requirement, file) {
  const form = new FormData()
  form.append('requirement', requirement)
  form.append('file', file)
  return request(`/clients/${clientId}/documents/upload`, {
    method: 'POST',
    body: form,
  })
}

export function listClientDocuments(clientId) {
  return request(`/clients/${clientId}/documents`)
}

export function deleteClientDocument(clientId, documentId) {
  return request(`/clients/${clientId}/documents/${documentId}`, {
    method: 'DELETE',
  })
}

// ---- Phase 4: drafted paperwork -------------------------------------------

export function createDraft(clientId, carrierId, kind) {
  const params = new URLSearchParams()
  if (carrierId) params.set('carrier_id', carrierId)
  if (kind) params.set('kind', kind)
  const query = params.toString() ? `?${params}` : ''
  return request(`/clients/${clientId}/drafts${query}`, { method: 'POST' })
}

export function listDraftKinds() {
  return request('/draft-kinds')
}

export function listDrafts(clientId) {
  return request(`/clients/${clientId}/drafts`)
}

export function editDraft(draftId, title, body) {
  return request(`/drafts/${draftId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, body }),
  })
}

export function deleteDraft(draftId) {
  return request(`/drafts/${draftId}`, { method: 'DELETE' })
}

export function getDraft(draftId) {
  return request(`/drafts/${draftId}`)
}

export function approveDraft(draftId, approvedBy, note) {
  return request(`/drafts/${draftId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved_by: approvedBy, note: note || null }),
  })
}

export function revokeDraft(draftId, reason) {
  return request(`/drafts/${draftId}/revoke`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason || null }),
  })
}

/**
 * Download a draft as a .docx.
 *
 * Not routed through request(), which parses JSON — this response is binary.
 * The filename comes from Content-Disposition so it matches what the backend
 * chose, rather than being rebuilt (and drifting) here.
 */
export async function downloadDraft(draftId) {
  const response = await fetch(`${BASE}/drafts/${draftId}/export`)
  if (!response.ok) {
    let detail = `Export failed (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Binary or empty error body — keep the status message.
    }
    throw new Error(detail)
  }

  const disposition = response.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename="([^"]+)"/)
  const filename = match ? match[1] : 'submission.docx'

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Revoked on the next tick so the click has taken the URL first.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

// ---- Phase 5: the feedback loop -------------------------------------------

export function getInsights() {
  return request('/insights')
}

export function getHouseStyle(kind) {
  return request(`/house-style/${kind}`)
}

export function learnHouseStyle(kind) {
  return request(`/house-style/${kind}/learn`, { method: 'POST' })
}

export function saveHouseStyle(kind, guidance) {
  return request(`/house-style/${kind}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guidance }),
  })
}

export function overrideGapItem(reportId, requirement, status, note) {
  return request(`/gap-reports/${reportId}/item`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requirement, status, note: note || null }),
  })
}

/** Fill a blank proposal form the carrier supplied, from the client's file. */
export async function fillProposalForm(clientId, file, carrierId) {
  const form = new FormData()
  form.append('file', file)
  if (carrierId) form.append('carrier_id', carrierId)
  return request(`/clients/${clientId}/drafts/fill-form`, {
    method: 'POST',
    body: form,
  })
}

// ---- Document Intelligence vertical slice ---------------------------------

/** Upload one policy document: quality check, OCR fallback, one extracted
 * fact with evidence and typed confidence, awaiting human review. */
export function uploadForExtraction(file, submissionId) {
  const form = new FormData()
  form.append('file', file)
  if (submissionId) form.append('submission_id', submissionId)
  return request('/doc-intelligence/upload', { method: 'POST', body: form })
}

export function createSubmission(label) {
  const form = new FormData()
  form.append('label', label)
  return request('/doc-intelligence/submissions', { method: 'POST', body: form })
}

export function listSubmissions() {
  return request('/doc-intelligence/submissions')
}

export function getSubmission(submissionId) {
  return request(`/doc-intelligence/submissions/${submissionId}`)
}

export function getExtractionFact(factId) {
  return request(`/doc-intelligence/facts/${factId}`)
}

export function reviewExtractionFact(factId, reviewer, action, newValue, reason) {
  return request(`/doc-intelligence/facts/${factId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reviewer,
      action,
      new_value: newValue || null,
      reason: reason || null,
    }),
  })
}

// ---- Organizations (pilot-stage tenant boundary) --------------------------
//
// Lets the Doc Intelligence screen actually exercise the isolation that is
// enforced on the backend — before this, the boundary was real but
// invisible: every request silently used the one default organization, and
// trying a second one meant hand-crafting an HTTP header outside the app.

export function listOrganizations() {
  return request('/organizations')
}

export function createOrganization(name) {
  const form = new FormData()
  form.append('name', name)
  return request('/organizations', { method: 'POST', body: form })
}
