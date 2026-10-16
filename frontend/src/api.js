const BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Phase 2A (v0.14.0): the organization and the user come from the signed-in
// session cookie (HttpOnly, set by the backend). The browser never chooses
// its organization by header any more. Every state-changing request carries
// the session's CSRF token, which is kept in memory only.
let csrfToken = null

export function setCsrfToken(token) {
  csrfToken = token || null
}

// Kept for older views: the organization is no longer chosen client-side.
export function setActiveOrganization() {}
export function getActiveOrganization() {
  return null
}

function withAuth(options = {}) {
  const headers = new Headers(options.headers || {})
  const method = (options.method || 'GET').toUpperCase()
  if (csrfToken && method !== 'GET' && method !== 'HEAD') headers.set('X-CSRF-Token', csrfToken)
  return { ...options, headers, credentials: 'include' }
}

function signalSignedOut() {
  window.dispatchEvent(new Event('aurevia:signed-out'))
}

/**
 * One place that turns a non-2xx response into a thrown Error carrying the
 * backend's own `detail` message. A 401 also tells the app shell to show the
 * sign-in screen.
 */
async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, withAuth(options))
  } catch {
    throw new Error(
      'Could not reach the backend. Is it running? (docker-compose up --build)',
    )
  }

  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/auth/login')) signalSignedOut()
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : detail
    } catch {
      // Non-JSON error body — keep the status-code message.
    }
    const err = new Error(detail)
    err.status = response.status
    throw err
  }

  return response.json()
}

/** Download any authorized file (binary) and save it under the server's name. */
export async function downloadFile(path, fallbackName = 'document') {
  const response = await fetch(`${BASE}${path}`, withAuth())
  if (!response.ok) {
    if (response.status === 401) signalSignedOut()
    let detail = `Download failed (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // binary or empty body
    }
    throw new Error(detail)
  }
  const disposition = response.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename="([^"]+)"/)
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = match ? match[1] : fallbackName
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

// ---- Sign-in, organization, team (Phase 2A) ---------------------------------

const json = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export async function login(email, password) {
  const me = await request('/auth/login', json('POST', { email, password }))
  setCsrfToken(me.csrf_token)
  return me
}

export async function getMe() {
  const me = await request('/auth/me')
  setCsrfToken(me.csrf_token)
  return me
}

export async function logout() {
  try {
    await request('/auth/logout', { method: 'POST' })
  } finally {
    setCsrfToken(null)
  }
}

export async function switchOrganization(organizationId) {
  const me = await request('/auth/switch-organization', json('POST', { organization_id: organizationId }))
  setCsrfToken(me.csrf_token)
  return me
}

export function changePassword(currentPassword, newPassword) {
  return request('/auth/change-password', json('POST', { current_password: currentPassword, new_password: newPassword }))
}

export function acceptInvitation(token, displayName, password) {
  return request('/auth/invitations/accept', json('POST', { token, display_name: displayName, password }))
}

export function completePasswordReset(token, password) {
  return request('/auth/password-reset/complete', json('POST', { token, password }))
}

export function listMembers() {
  return request('/organization/members')
}

export function listAssignableUsers() {
  return request('/organization/users')
}

export function listInvitations() {
  return request('/organization/invitations')
}

export function inviteMember(email, role) {
  return request('/organization/invitations', json('POST', { email, role }))
}

export function revokeInvitation(invitationId) {
  return request(`/organization/invitations/${invitationId}`, { method: 'DELETE' })
}

export function updateMember(userId, changes) {
  return request(`/organization/members/${userId}`, json('PUT', changes))
}

export function issuePasswordReset(userId) {
  return request(`/organization/members/${userId}/password-reset`, { method: 'POST' })
}

export function listAuditEvents(params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ''))
  return request(`/audit-events${q.toString() ? `?${q}` : ''}`)
}

// ---- Client workspace (Phase 2A) ----------------------------------------------

export function searchClients(params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ''))
  return request(`/clients${q.toString() ? `?${q}` : ''}`)
}

export function archiveClient(clientId) {
  return request(`/clients/${clientId}/archive`, { method: 'POST' })
}

export function restoreClient(clientId) {
  return request(`/clients/${clientId}/restore`, { method: 'POST' })
}

export function getClientWorkspace(clientId) {
  return request(`/clients/${clientId}/workspace`)
}

export function linkClientBusiness(clientId, businessId) {
  return request(`/clients/${clientId}/link-business`, json('POST', { business_id: businessId }))
}

export function uploadIntelligenceDocument(clientId, file) {
  const form = new FormData()
  form.append('file', file)
  return request(`/clients/${clientId}/intelligence-documents`, { method: 'POST', body: form })
}

export function analyzeSubmission(submissionId) {
  return request(`/knowledge/submissions/${submissionId}/analyze-property-policy`, { method: 'POST' })
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
  const response = await fetch(`${BASE}/drafts/${draftId}/export`, withAuth())
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
  return request('/organizations', json('POST', { name }))
}

// ---- Knowledge Core (Phase 1) ----------------------------------------------
//
// Entities/facts/evidence/relationships built on top of the Document
// Intelligence slice's evidence+confidence+review shapes — see
// backend/knowledge/ and backend/property_policy_intelligence.py. Every call
// below goes through request(), so it automatically carries whichever
// organization the Doc Intelligence screen's switcher has selected — the
// same tenant boundary, not a second one.

export function getEntityFacts(entityId) {
  return request(`/knowledge/entities/${entityId}/facts`)
}

export function getKnowledgeFact(factId) {
  return request(`/knowledge/facts/${factId}`)
}

export function getKnowledgeFactHistory(factId) {
  return request(`/knowledge/facts/${factId}/history`)
}

export function correctKnowledgeFact(factId, newValue, reviewedBy, normalizedValue) {
  return request(`/knowledge/facts/${factId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      new_value: newValue,
      normalized_value: normalizedValue ?? null,
      reviewed_by: reviewedBy,
    }),
  })
}

/** Trigger Property Policy Intelligence over one submission's documents —
 * extracts property/policy facts, builds Business + Asset(property) +
 * Policy entities and their relationships, and reconciles facts across
 * documents (flagging contradictions rather than picking a winner). */
export function analyzePropertyPolicy(submissionId) {
  return request(`/knowledge/submissions/${submissionId}/analyze-property-policy`, {
    method: 'POST',
  })
}

// ---- Knowledge explorer (v0.9.0) -------------------------------------------

export function listKnowledgeEntities(entityType) {
  const q = entityType ? `?entity_type=${encodeURIComponent(entityType)}` : ''
  return request(`/knowledge/entities${q}`)
}

/** Everything known about one entity; asOf (YYYY-MM-DD) adds "in force on". */
export function getEntityKnowledge(entityId, asOf) {
  const q = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  return request(`/knowledge/entities/${entityId}/knowledge${q}`)
}

export function reviewKnowledgeFact(factId, action, reviewedBy) {
  return request(`/knowledge/facts/${factId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, reviewed_by: reviewedBy }),
  })
}

export function listPossibleDuplicates(entityType) {
  const q = entityType ? `?entity_type=${encodeURIComponent(entityType)}` : ''
  return request(`/knowledge/possible-duplicates${q}`)
}

/** v0.10.0: answer a question from the Knowledge Core (deterministic first). */
export function queryKnowledge(question, { entityId, asOf, useLlm = true } = {}) {
  return request('/knowledge/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, entity_id: entityId || null, as_of: asOf || null, use_llm: useLlm }),
  })
}

// ---- Policy Intelligence (Phase 1C, v0.11.0) --------------------------------

export function getPolicy(policyId, asOf) {
  const q = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  return request(`/policies/${policyId}${q}`)
}

export function comparePolicy(leftPolicyId, rightPolicyId, asOf) {
  return request('/policies/compare', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ left_policy_id: leftPolicyId, right_policy_id: rightPolicyId || null, as_of: asOf || null }),
  })
}

// ---- Business Risk Intelligence (Phase 1D, v0.12.0) -------------------------

export function getBusinessProfile(businessId, asOf) {
  const q = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  return request(`/businesses/${businessId}/profile${q}`)
}

export function getBusinessChanges(businessId, fromDate, toDate) {
  const p = new URLSearchParams()
  if (fromDate) p.set('from_date', fromDate)
  if (toDate) p.set('to_date', toDate)
  const q = p.toString() ? `?${p}` : ''
  return request(`/businesses/${businessId}/changes${q}`)
}

// ---- Risk → Coverage Intelligence (Phase 1E, v0.13.0) -----------------------

export function getCoverageAudit(businessId, asOf) {
  const q = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
  return request(`/businesses/${businessId}/coverage-audit${q}`)
}

export function reviewCoverageFinding(findingId, businessId, decision, reviewer, comment) {
  return request(`/coverage-findings/${findingId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ business_id: businessId, decision, reviewer, comment: comment || null }),
  })
}
