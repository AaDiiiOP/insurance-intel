# Phase 2A — Broker Workspace: status (v0.14.1, 2026-10-26)

Status: **FROZEN — DEVELOPMENT GATE PASSED (2026-10-26).** The developer's
Docker check on v0.14.1 passed every gate item (see "Docker verification"
below). The first Docker check (v0.14.0) had found one blocker — paperwork
upload failed on his existing database — fixed in v0.14.1.
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

## Docker verification (freeze gate)

### Run 1 — v0.14.0, developer's machine (Docker Compose, existing database)
Commands:
```
docker-compose up -d --build
docker-compose exec backend python3 phase2a_live_check.py http://localhost:8000
docker-compose logs --tail 80 backend
```
Result: **FAIL.** 22 checks passed (sign-in, cookies, CSRF, organization
switch and header refusal, roles, client search / pagination / assignment /
archive / restore), then `POST /clients/{id}/documents/upload` returned 500,
so the duplicate check failed and the run stopped. Log:
`psycopg.errors.UndefinedColumn: column "score" of relation "client_documents" does not exist`.

Root cause: the developer's database created `client_documents` before the
columns `score`, `score_parts`, `improvements` (and others) were added to
SCHEMA. `CREATE TABLE IF NOT EXISTS` skips an existing table and no `ALTER`
was ever written for these columns. **Pre-existing schema drift, not
introduced by Phase 2A**; the sandbox missed it because every sandbox
database was created fresh. Every past release (v0.4.0–v0.13.0) already had
these columns in SCHEMA, so the developer's table is older than v0.4.0.

### Fix — v0.14.1 (smallest safe fix, nothing dropped)
- `repositories/connection.py` MIGRATIONS: `ALTER TABLE client_documents ADD
  COLUMN IF NOT EXISTS ...` for all 14 non-key columns, same types and
  defaults as SCHEMA. Runs on every startup; a no-op once applied; existing
  rows keep their data and get the column default.
- `find_schema_drift()` + `backend/check_schema.py`: **read-only** check that
  builds the expected tables in a scratch schema inside a rolled-back
  transaction and lists any missing table or column in the live database.
- `backend/phase2a_live_check.py` added to the repo (41 checks; new check 0:
  "database has every table and column the code expects").
- Regression tests `tests/test_schema_upgrade.py` (3): a client_documents
  table made old (14 columns dropped, one existing row) is repaired by
  MIGRATIONS, the row survives with defaults, the failing insert now works,
  migrations are idempotent, the drift check leaves nothing behind.
- Test flake fixed (test only): the duplicate-upload test built the same
  .docx twice; a .docx stores a timestamp, so builds ~2 s apart differ and
  the 409 was occasionally not triggered (1 failure in 3 full runs). The
  product's duplicate check was correct; the test now builds the file once.

### Sandbox verification of v0.14.1 (2026-10-21; Docker Hub is blocked here)
- Old-database reproduction: database built with v0.14.0 code, then
  `score, score_parts, improvements` dropped. `check_schema.py` → FAIL (3
  missing columns); v0.14.0 startup migrations → still FAIL; v0.14.1 backend
  started on it → `check_schema.py` PASS; `phase2a_live_check.py` over HTTP
  → **41/41 PASS** (upload, duplicate 409, download, intelligence upload,
  cross-tenant, rate limit, expiry, audit, no secrets).
- Backend tests: **225 passed, 0 failed, 0 skipped** (222 + 3), run 3 times.
- Dry run (model off) AUR-SYN-001–004: Phase 1B 0 wrong + 28/28 probes; 1C
  28/28 + 24/24; 1D 53/53 + 24/24; 1E 36/36 + 51/51 — unchanged.
- Phase 1D acceptance audit (sandbox dry data): 198/198, evidence 100%, page 100%.

### Run 2 — v0.14.1, developer's machine (2026-10-21 and 2026-10-26): **PASS**
Docker Compose, Windows, the developer's existing (older) database;
services backend :8100, frontend :5183, postgres :5442, chromadb :8101.
Synthetic data only; each live check creates its own throwaway organization.

2026-10-21 (Ollama running):
```
docker-compose up -d --build backend
docker-compose exec backend python3 check_schema.py
docker-compose exec backend python3 phase2a_live_check.py http://localhost:8000
docker-compose exec backend python3 -m pytest -q
docker-compose exec backend python3 check_schema.py
```
- First `check_schema.py`: FAIL, 3 missing columns (`score`, `score_parts`,
  `improvements`) — run before the backend had finished starting up. The
  live check a moment later and the re-run both: **PASS** (0 missing tables,
  0 missing columns, 0 type differences) — the v0.14.1 migration repaired
  the developer's table.
- Live check: 23 checks PASS, then the paperwork upload exceeded the
  script's 60-second client timeout while the local Qwen model verified the
  file (a limit of the check script, not an app error; no 500 this time).
- pytest: **225 passed, 0 failed, 0 skipped** (1 library DeprecationWarning
  from starlette/anyio), 104.9 s.

2026-10-26 (final gate run; Ollama stopped so uploads skip the model's
document verification and finish quickly):
```
cd C:\Project\Perosnal_Projects\insurance-intel
docker-compose up -d --build
docker-compose exec backend python3 check_schema.py
docker-compose logs --tail 200 backend | Select-String "documents/upload|Error|Traceback"
docker-compose exec backend python3 phase2a_live_check.py http://localhost:8000
docker-compose exec backend python3 -m pytest -q
docker-compose ps
```
- `check_schema.py`: **PASS** (0 missing tables, 0 missing columns, 0 type
  differences).
- Log search: no errors (the rebuild had cleared the earlier log).
- `phase2a_live_check.py`: **41 checks, 41 passed, 0 failed → PASS** —
  schema; unauthenticated 401s; identical 401 for wrong password / unknown
  e-mail; sign-in; HttpOnly + SameSite=Lax cookie; organization create /
  switch / non-member refused; header naming another organization 403; CSRF
  403; ADMIN cannot grant OWNER; REVIEWER cannot create clients; BROKER
  creates but cannot delete clients, cannot read audit or manage members;
  REVIEWER can view; search; pagination; assign; archive; restore; client
  document upload; duplicate 409; authorized download returns the original;
  intelligence document upload; bad file type 400; workspace lists both;
  intelligence endpoints work when signed in; another organization's user
  gets 404 on every probe and A's client is unchanged; 5 failed sign-ins →
  429; logout; signed-out session not reusable; expired and idle sessions
  refused; audit events recorded; no password or token in the audit API; no
  password, session token or invitation code in clear in the database;
  passwords are argon2id.
- pytest: **225 passed, 0 failed, 0 skipped** (same 1 library warning),
  172.4 s.
- `docker-compose ps`: backend, frontend, postgres, chromadb all Up.
- Frontend through Docker (http://localhost:5183), checked by hand: sign
  in as Owner, client list, client → "Documents & intelligence", "Team &
  security", sign out — all worked, no errors.

### Freeze gate
| # | Criterion | Result |
|---|---|---|
| 1 | Docker startup and migrations | PASS — schema check 0 missing |
| 2 | Authentication and role checks | PASS — live check + pytest |
| 3 | Cross-tenant access denied | PASS — every live probe 404, pytest over every route |
| 4 | Client and document workflows | PASS |
| 5 | Intelligence regression | PASS — endpoints work signed in (Docker); dry run 1B–1E unchanged and Phase 1D audit 198/198 (sandbox) |
| 6 | Frontend builds and works through Docker | PASS |
| 7 | Complete test suite | PASS — 225/225, 0 skipped, no suppressed failures |
| 8 | Limitations documented | Done — below |

Not verified on Docker: an upload with the real Qwen model running to the
end (it takes longer than the check script's 60 s on the developer's
laptop); the upload itself is covered by the run without the model and by
pytest. `/docs` returning 404 in production mode was verified in the
sandbox only (the developer ran the development compose file).

## Security review — classification
| Finding | Class | Note |
|---|---|---|
| `client_documents` columns missing on older databases (upload 500) | **Blocker — fixed in v0.14.1** | verified on the developer's Docker database 2026-10-26 |
| Live check gives up after 60 s while the local model verifies an upload | Future task (test tooling) | not an app defect; raise the script's timeout for model steps when needed |
| No PostgreSQL row-level security | Accepted limitation | application-level isolation, tested over every route; RLS is production-hardening work |
| Cookie `Secure` off by default; no HTTPS in this repo | Accepted for local use; **blocker for any real deployment** | set `AUTH_COOKIE_SECURE=true` behind HTTPS |
| Per-IP sign-in limit counts the direct address | Accepted for single-developer use; **must fix before multi-user use** | behind a proxy / Docker port mapping everyone shares one address, so 30 failures in 15 min block all sign-ins. Seen in the sandbox: three test runs left 36 failures from the shared test address and the Phase 1D audit's sign-in was refused (429) until the window passed. Fix proposed, not applied (needs approval). |
| No multi-factor authentication | Future task | |
| `/docs`, `/openapi.json` public outside production | Accepted | 404 when `AUREVIA_ENV=production` (verified in the sandbox) |
| Records before v0.14.0 carry self-declared names | Accepted limitation | |
| No e-mail delivery of invitation / reset codes | Accepted (by design) | codes handed over by an Owner/Admin |

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
v0.14.0 Docker check: FAIL (upload on an older database) → fixed in v0.14.1.
v0.14.1 Docker check (2026-10-26): schema PASS, live check 41/41, pytest
225/225, frontend works through Docker.
**Phase 2A: FROZEN — DEVELOPMENT GATE PASSED (2026-10-26).** Not
production-ready; no compliance claim; no PostgreSQL RLS.
Phases 2B (client intelligence workspace), 2C and 2D — not started.
