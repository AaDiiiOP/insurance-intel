# Phase 2A — Broker Workspace: status (v0.14.0, 2026-10-11)

Status: **PHASE 2A DEVELOPMENT GATE PASSED (sandbox) — awaiting the developer's Docker check.**
Not production-ready: see Security findings and Known limitations.
Phases 2B, 2C and 2D are not started.

Starting point: v0.13.0 (Phase 1E accepted and frozen).

## Baseline (before any change)
- Backend tests: **199 total — 199 passed, 0 failed, 0 skipped.**
- Frontend: `vite build` succeeds. API: running; no authentication at all —
  the organization was chosen by an `X-Organization-Id` header (or a default
  organization when absent); approver/reviewer names were typed by the user.

## What already existed and was reused
Organizations (tenant boundary on every table, filtered in SQL), clients
(create / edit / delete / search-less list), client paperwork uploads with
verification, Document Intelligence uploads + submissions, the Knowledge Core,
Policy / Business / Coverage intelligence, drafts with approve / revoke /
DOCX export and an append-only draft history, corrections. None of this was
rebuilt; the ingestion pipeline (parsers, OCR, quality check) is unchanged.

## Scope delivered
1. **Authentication** — e-mail + password; argon2id hashing (`argon2-cffi`);
   unknown e-mails still run a hash check (no timing difference); one generic
   error message. Sessions: random 256-bit token in an HttpOnly, SameSite=Lax
   cookie (Secure when `AUTH_COOKIE_SECURE=true`); only its SHA-256 is stored;
   12 h absolute expiry, 120 min idle timeout; a new token on every sign-in,
   organization switch and password change (no session fixation); logout
   revokes; a password change or reset signs out all other sessions; a role
   change or suspension signs the member out.
2. **CSRF** — every state-changing request must carry `X-CSRF-Token` equal to
   the session's own random token (returned by `/auth/login` and `/auth/me`,
   kept in memory by the frontend, never in a cookie).
3. **Rate limiting** — failed sign-ins counted per e-mail (5) and per IP (30)
   over 15 minutes (configurable); invitation / reset completion also limited.
4. **Organizations, memberships, roles** — users can belong to several
   organizations; roles OWNER / ADMIN / BROKER / REVIEWER with an explicit
   permission list (`auth.ROLE_PERMISSIONS`). Every route is mapped to a
   permission (`auth.ROUTE_PERMISSIONS`); a state-changing route that is not
   mapped is **denied by default** (a test fails if one is ever added unmapped).
   Only an Owner can grant / change the Owner role; the last active Owner
   cannot be demoted or suspended.
5. **Tenant isolation** — the organization of every request comes only from
   the session's verified, active membership (`api/deps._resolve_organization`
   now reads it; without a session it fails closed with 401). An
   `X-Organization-Id` header naming another organization is refused (403).
   Still application-level isolation, **not PostgreSQL RLS** (see below).
6. **Invitations and password resets** — created by Owner/Admin, random,
   stored only as hashes, single-use, time-limited (72 h / 60 min). No e-mail
   is sent: the code is shown once to the creator, who hands it over.
7. **Reviewer identity from the session** — draft approval / revocation /
   edits / export, coverage-finding reviews, Knowledge Core fact review and
   correction, Document Intelligence fact review now record the signed-in
   user ("Name <email>"); typed names are ignored (fields kept optional so old
   clients do not break).
8. **Client management** — search (name, trading name, industry, city;
   wildcards escaped), status filter (active / archived / all), "assigned to
   me", SQL pagination; archive / restore (delete stays Owner/Admin only);
   trading name, notes, assigned broker (must be an active member); which
   fields were pre-filled from a document and not yet confirmed
   (`field_provenance`); link the client to the Knowledge Core business built
   from its own documents (one business per client, no duplicate identity).
9. **Document workspace** — one library per client (paperwork + intelligence
   documents, processing status, errors); intelligence uploads go through the
   existing parser into the client's own submission; analysis stays an
   explicit step (existing endpoint); original files of new uploads are
   stored (`stored_files`) and downloadable only by members with
   `evidence.view` (attachment, nosniff, no-store); duplicate files for the
   same client are refused (SHA-256); file type and 50 MB limit as before.
10. **Audit history** — `audit_events` table, append-only enforced by a
    database trigger (UPDATE/DELETE raise). Recorded: sign-in, failed sign-in
    (e-mail only), sign-out, organization switch, password change / reset,
    invitations, membership changes, client create / edit / archive / restore
    / delete / link, document upload / download / delete, draft approve /
    revoke / export. Readable only with `audit.view` (Owner/Admin). No
    passwords, tokens or document text are logged.
11. **First Owner** — `python3 manage_users.py create-owner ...` (password
    asked interactively, never on the command line).
12. **Frontend** — sign-in screen (sign in / invitation code / reset code),
    session check on load, automatic return to sign-in on 401, organization
    box with switcher, user, role and sign-out, "Team & security" page
    (members, roles, suspend, reset codes, invitations, own password, audit
    history), client list with search / filters / pagination / archive,
    client "Documents & intelligence" panel (library, download, upload, run
    analysis, link business), typed-name fields removed, small-screen layout
    (no horizontal scrolling at 390 px).

## Database changes (idempotent, in `repositories/connection.py` MIGRATIONS)
New tables: `users`, `memberships`, `auth_sessions`, `auth_tokens`,
`login_attempts`, `audit_events` (+ append-only trigger), `stored_files`.
New columns: `clients.status, archived_at, trading_name, notes,
assigned_user_id, business_entity_id, field_provenance`;
`submissions.client_id`. Document order is now deterministic
(`uploaded_at, filename, id`).

## API (new)
`POST /auth/login`, `POST /auth/logout`, `GET /auth/me`,
`POST /auth/switch-organization`, `POST /auth/change-password`,
`POST /auth/invitations/accept`, `POST /auth/password-reset/complete`,
`GET|POST /organizations`, `GET /organization/members`,
`GET /organization/users`, `GET|POST /organization/invitations`,
`DELETE /organization/invitations/{id}`, `PUT /organization/members/{user_id}`,
`POST /organization/members/{user_id}/password-reset`, `GET /audit-events`,
`POST /clients/{id}/archive | restore | link-business | link-submission`,
`POST /clients/{id}/intelligence-documents`, `GET /clients/{id}/workspace`,
`GET /clients/{id}/documents/{doc}/download`,
`GET /doc-intelligence/documents/{doc}/download`.
Changed: `GET /clients` (q, status, assigned_to, page, page_size; returns
total). Removed: the unauthenticated `POST /organizations` form endpoint
(`api/organizations.py`).

## Files
Created: `backend/auth.py`, `backend/api/auth.py`, `backend/api/workspace.py`,
`backend/repositories/auth.py`, `backend/manage_users.py`,
`backend/tests/auth_helpers.py`, `backend/tests/test_phase2a_workspace.py`,
`frontend/src/views/Login.jsx`, `frontend/src/views/Team.jsx`,
`frontend/src/views/ClientWorkspacePanel.jsx`, this file, `PHASE_2_STATUS.md`.
Modified: `backend/main.py` (global guard, CORS with credentials, routers),
`backend/api/deps.py`, `api/clients.py`, `api/drafting.py`,
`api/doc_intelligence.py`, `api/knowledge.py`, `api/coverage.py`,
`repositories/connection.py`, `repositories/clients.py`,
`repositories/submissions.py`, `repositories/__init__.py`, `requirements.txt`,
`phase1d_acceptance_audit.py` (signs in with temporary read-only auditors),
`docker-compose.yml`, `docker-compose.prod.yml`, seven existing test files
(they now sign in; expected results unchanged; cross-tenant checks use a
second signed-in user), `frontend/src/api.js`, `App.jsx`, `Clients.jsx`,
`ClientDetail.jsx`, `DocIntelligence.jsx`, `Knowledge.jsx`,
`KnowledgeExplorer.jsx`, `CoveragePanel.jsx`, `index.css`, `package.json`.
Deleted: `backend/api/organizations.py`.

## Tests
- Before: 199 passed. **After: 222 passed, 0 failed, 0 skipped** (23 new).
- New tests cover: hashing, cookie flags, hash-only session storage, identical
  failure responses, **every protected route of the real app returns 401
  without a session (90+ routes)**, every state-changing route has a
  permission, CSRF, logout, no session fixation, idle / absolute expiry,
  suspended membership, password change sign-out, rate limiting, header
  organization refusal, role enforcement, organization switching, invitation
  single-use / expiry / role limits, password reset, last-Owner protection,
  **every route of the app called by another organization's user with real
  ids returns no data and accepts no write**, client search / pagination /
  archive / assignment / provenance, upload / download / duplicate / file
  type, document-client mismatch, client ↔ business link, approval identity,
  append-only audit, no secrets in audit.
- Intelligence gates unchanged (dry run, model off, AUR-SYN-001–004): Phase 1B
  0 wrong + 28/28 probes, 1C 28/28 + 24/24, 1D 53/53 + 24/24, 1E 36/36 + 51/51;
  Phase 1D acceptance audit 198/198.
- Frontend: `vite build` OK; browser run (Playwright, real backend): sign-in,
  wrong password message, client create, intelligence upload, original-file
  download, invitation → accept → sign in as Reviewer (no "New client"
  button), mobile 390 px with no horizontal scrolling on every page.
- AI: Phase 2A adds no model behaviour; no real-Qwen run needed for this gate.

## Security findings (honest list)
- **No PostgreSQL row-level security.** Isolation is enforced in the
  application (one place resolves the organization; every query filters on
  it; tested over every route). RLS was evaluated and deferred: the app uses
  one database role that owns all tables, so RLS would need FORCE ROW LEVEL
  SECURITY plus a per-request setting in every query path.
- Cookie `Secure` is off by default so local HTTP works; **set
  `AUTH_COOKIE_SECURE=true` when serving over HTTPS.** No HTTPS is provided by
  this repository.
- The per-IP limit uses the direct client address; behind a reverse proxy all
  users share one address (X-Forwarded-For is deliberately not trusted).
- No multi-factor authentication; no account lockout beyond the rate limit.
- `/docs` and `/openapi.json` are public when `AUREVIA_ENV` is not
  `production` (the production compose file sets it).
- Records made before v0.14.0 still carry self-declared names.
- Original files are stored in PostgreSQL (simple and access-controlled, but
  grows the database); files uploaded before v0.14.0 have no original.

## Known limitations
- Invitation / reset codes are passed on by hand (no e-mail by design).
- The client ↔ business link is suggested from the client's own submission
  and confirmed by a click; linking an existing older submission uses
  `POST /clients/{id}/link-submission` (no screen yet).
- Archived clients stay readable and editable by authorized users.
- Not production-ready; no compliance claim of any kind.

## Acceptance result
Phase 2A development gate (sandbox): authentication, membership,
server-side authorization, cross-tenant tests, client management, document
workspace, frontend/backend integration, intelligence features preserved,
regression passing, security findings documented — **PASS**.
Freeze after the developer's Docker check. **Next: Phase 2B (client
intelligence workspace) — not started.**
