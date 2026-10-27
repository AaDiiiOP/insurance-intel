# Phase 2C — Human Review & Audit Workflows: status (v0.16.0, 2026-10-27)

Status: **COMPLETE — DEVELOPMENT GATE PASSED (2026-10-27).** Verified in the
sandbox and on the developer's Docker deployment (see "Docker verification"). Not production-ready; no
compliance claim; tenant isolation is application-level (no PostgreSQL RLS).
Phase 2D is not started.

Starting point: v0.15.0 (Phase 2A frozen, Phase 2B complete).

## Baseline (sandbox, before any change)
- Backend tests: **244 passed, 0 failed, 0 skipped.**
- Dry run (model off) AUR-SYN-001–004: Phase 1B 0 wrong + 28/28 probes; 1C
  28/28 + 24/24; 1D 53/53 + 24/24; 1E 36/36 + 51/51.

## Gap analysis (real code)
Already existed and reused unchanged:
- Knowledge Core `review_fact` (verify only with evidence and not in a
  conflict; reject keeps the row as superseded) and `correct_fact`
  (versioned: the original row is closed, never overwritten; a conflict is
  settled only by the corrected value a person gives — never "newest wins").
- Phase 1E coverage review (`kc_coverage_reviews`): decisions OPEN / REVIEWED
  / ACCEPTED / REJECTED / NEEDS_MORE_EVIDENCE stored with an evidence
  fingerprint; a decision on different evidence shows as stale; the server
  checks the finding belongs to the organization and business.
- Roles and permissions (`facts.correct`, `findings.review`), reviewer
  identity from the session, append-only `audit_events`.

Missing (built in 2C):
1. No review queue.
2. No assignment or lifecycle (in review, needs more information, reopen).
3. No reason required for corrections, rejections or dismissals.
4. Fact verify / reject / correct, coverage decisions and Document
   Intelligence fact reviews were **not written to the audit history**.
5. Nothing acted on staleness (the coverage "stale" flag was display-only)
   and a verified fact that later became conflicting was not re-reviewed.

## Delivered
1. **Workflow tables** (`repositories/connection.py` MIGRATIONS; a workflow
   layer only — no facts are stored here):
   - `review_items`: one row per actionable item the engines report —
     `fact_conflict` (unresolved conflict), `unverified_fact` (current
     needs_review fact with evidence), `missing_evidence` (current
     needs_review fact with no evidence), `coverage_finding` (open or stale
     Phase 1E finding), `failed_document`. Linked to client, business,
     subject, title/summary, engine status, evidence fingerprint, state,
     assignee. Unique per (organization, kind, subject).
   - `review_events`: append-only history (database trigger refuses UPDATE
     and DELETE): actor, by_system, action, from → to state, decision,
     reason, evidence references, metadata.
2. **`review_workflow.py`** — queue refresh, lifecycle, decisions:
   - Lifecycle (server-side; anything else 409): assign / reassign
     (Owner, Admin); unassign; start (claims an unassigned item); needs more
     information (reason); decide (only while IN_REVIEW); reopen (reason;
     coverage and document items).
     States: OPEN, ASSIGNED, IN_REVIEW, NEEDS_INFO, RESOLVED, DISMISSED,
     REOPENED.
   - Decisions go only through the existing services:
     conflict → `correct` (a person chooses one competing fact and may type
     the value; reason required; uses `correct_fact`, which closes the other
     side and keeps every original row); unverified fact → `verify`,
     `correct` (reason), `reject` (reason); fact without evidence → `reject`
     (reason; it cannot be verified); coverage finding → ACCEPTED,
     REVIEWED, REJECTED (reason → DISMISSED), NEEDS_MORE_EVIDENCE (reason →
     NEEDS_INFO), stored by the Phase 1E review with the reason as comment;
     failed document → resolved / dismissed (reason; the document itself is
     unchanged).
   - Queue refresh (on every queue read, serialized per organization):
     creates items for new engine reports (system event "created");
     **reopens** a closed coverage item when its evidence changed (stale) and
     a verified-fact item when that fact later became part of a conflict;
     **closes** an open item whose subject was settled outside the queue
     (e.g. in the Knowledge explorer or Coverage panel). System events are
     recorded as `by_system`, actor "system" — never as a person. A coverage
     finding already decided before the queue saw it is not queued.
   - Permissions: Owner/Admin assign and act on any item; Reviewer works
     fact and coverage items and cannot act on an item assigned to someone
     else; Broker sees the queue and can work failed-document items only.
     Assignees must be active members whose role can work that kind.
3. **API** (`api/reviews.py`): `GET /review-queue` (filters kind, state
   incl. open/closed, assigned me/unassigned, client; pagination; counts),
   `GET /review-items/{id}` (item, live engine view with evidence, competing
   values, history, the actions this user may take, assignable members),
   `POST /review-items/{id}/assign | start | needs-info | decide | reopen`.
   Permissions: `reviews.assign` (Owner, Admin), `reviews.work` (all four
   roles; each kind also needs its own permission), checked again in code.
   Organization and reviewer come from the session; another organization's
   item id → 404; the organization header cannot change it (403).
4. **Existing endpoints (approved decisions A and B):**
   `PUT /knowledge/facts/{id}` now requires `reason`; `POST
   /knowledge/facts/{id}/review` requires `reason` for `reject`; coverage
   `REJECTED` requires a comment; Document Intelligence `corrected` requires
   a reason. All of these, and fact verify, now write audit events
   (`fact.verified`, `fact.rejected`, `fact.corrected`,
   `coverage.review_recorded`, `doc_fact.*`). Every queue action writes
   `review.*` audit events.
5. **Frontend**: new "Review queue" page (`views/ReviewQueue.jsx`) — filters,
   list, detail with the system finding labelled "System finding … not a
   decision", competing values with evidence and page text, the server's
   allowed actions only, reason box, assignment, history (system vs person),
   link to the client's intelligence tab; loading, empty, error and
   permission states; works at 390 px. Reason fields added to the Knowledge
   explorer (reject), Knowledge analysis view (correct), Coverage panel
   (reject) and Document Intelligence (correct). The client intelligence
   section can open on a given tab.
6. Live check: one Phase 2C check added (queue loads; another organization
   sees none of its items and gets 404 on them) — 44 checks.

## Tests (sandbox, actually run)
- **265 passed, 0 failed, 0 skipped** (244 + 21 new in
  `tests/test_phase2c_review_workflow.py`); two existing tests updated for
  required reasons (`test_temporal_knowledge.py` now also asserts a
  rejection without a reason is refused; `test_tenant_isolation_audit.py`
  now gives a reason so only the tenant check can refuse — it asserts 404).
  One library warning (starlette/anyio).
- New tests: genuine items only, linked to client/business/evidence; second
  refresh creates nothing; conflict detail shows both values with evidence;
  empty queue; decided coverage findings not queued / closed by the system;
  valid and invalid transitions (409), reasons (400); fact items cannot be
  reopened by hand; assignment rules (reviewer cannot assign, broker cannot
  be assigned fact items, non-member refused, reassignment, another
  reviewer's item 403); broker limited to failed documents (and a resolved
  document item stays closed); verify through the Knowledge Core with the
  session identity and evidence; correction requires a reason and keeps the
  original; conflict resolved only by a person's choice, both originals kept,
  evidence of both sides recorded; missing evidence cannot be verified, only
  rejected with a reason; a verified fact reopens when a later document
  contradicts it; coverage REJECTED needs a reason, stale evidence reopens
  the item; coverage items bound to their own business; every mutation
  audited with the session identity; review history cannot be updated or
  deleted; direct endpoints require reasons and are audited; no session 401;
  another organization sees nothing and changes nothing (404, header 403,
  outsider cannot be assigned); queue filters.
- Dry run (model off) AUR-SYN-001–004: Phase 1B 0 wrong + 28/28; 1C 28/28 +
  24/24; 1D 53/53 + 24/24; 1E 36/36 + 51/51 — **unchanged**.
- Phase 1D acceptance audit (sandbox dry data): **198/198**, evidence and
  page 100% (first attempt refused at sign-in by the per-IP limit after the
  test runs — the known Phase 2A limitation; rerun after the 15-minute
  window).
- Live check over HTTP (sandbox backend, model off): **44/44 PASS**.
- Frontend: `vite build` OK. Browser (Playwright, real backend, synthetic
  Riverbend organization): Reviewer starts a conflict, picks a value, gives
  a reason → resolved with history; Owner assigns a coverage finding to the
  Reviewer and opens the client's Coverage tab from the item; Broker sees
  "no action available" on a fact item; 390 px with no horizontal
  scrolling; no page errors.
- Not run: real-Qwen evaluation (no model behaviour changed).

## Docker verification (developer's machine, 2026-10-27)
Docker Compose, Windows, existing database; synthetic data only.
```
cd C:\Project\Perosnal_Projects
Expand-Archive -Path "$env:USERPROFILE\Downloads\insurance-intel-v0.16.0.zip" -DestinationPath . -Force
cd C:\Project\Perosnal_Projects\insurance-intel
docker-compose up -d --build
docker-compose exec backend python3 check_schema.py
docker-compose exec backend python3 -m pytest -q
docker-compose exec backend python3 phase2a_live_check.py http://localhost:8000
```
- `check_schema.py`: **PASS** (0 missing tables, 0 missing columns, 0 type
  differences) — the new review tables were created by the startup
  migrations on the existing database.
- pytest: **265 passed, 0 failed, 0 skipped** (1 library DeprecationWarning), 178.6 s.
- Live check: **44 checks, 44 passed, 0 failed** — including "review queue
  loads and stays inside the organization (Phase 2C)". Run with Ollama
  stopped, as before.
- Browser (http://localhost:5183), by hand: Owner opened Review queue; a
  coverage finding was started, rejected (dismissed) with a reason and the
  history showed the name and reason; the "Open … tab" link reached the
  client's Coverage findings tab; in Knowledge Core, rejecting a fact
  without a reason was refused — all fine.

## Known limitations
- The queue refreshes when it is read (a GET also brings the workflow table
  in line with the engines); with very many businesses this costs one
  coverage audit per business per read.
- Unverified-fact items are per fact; an analyzed organization has many
  (about a dozen per company in AUR-SYN data). Filters and pagination help;
  there is no bulk verify (deliberately).
- Only current facts are queued (historical rows awaiting review are not).
- Fact items reopen automatically, not by hand; to change a decided fact,
  correct it (a new version) — history is never rewritten.
- Coverage staleness in the test is simulated by changing the stored
  fingerprint (the engine's real fingerprint path is the Phase 1E code).
- Phase 0 Document Intelligence extracted facts are not in the queue (they
  are a separate pre-Knowledge-Core store); their reviews are now audited
  and corrections need a reason.
- Conflict titles use the business entity's name as the engines store it
  (the submission label when no legal name was set).
- The per-IP sign-in limit (Phase 2A limitation) was hit again in the
  sandbox by repeated test runs; still to be fixed before multi-user use.
- All Phase 2A security limitations still apply (no RLS, HTTPS / Secure
  cookie for real deployments, no MFA).

## Acceptance gate
| # | Criterion | Sandbox result |
|---|---|---|
| 1 | Queue shows genuine actionable items | PASS |
| 2 | Transitions and permissions enforced server-side | PASS |
| 3 | Corrections preserve provenance and history | PASS |
| 4 | Coverage reviews keep evidence and detect stale decisions | PASS |
| 5 | Audit captures meaningful mutations | PASS |
| 6 | Cross-tenant and role checks | PASS |
| 7 | Phase 1A–1E and 2A–2B regression | PASS (model-off gates, 1D audit 198/198, 244 earlier tests, live check) |
| 8 | Frontend builds, tests pass | PASS (265/265 sandbox and Docker; live check 44/44 Docker; browser check on Docker) |
| 9 | Limitations documented | this file |

**Phase 2C: COMPLETE — DEVELOPMENT GATE PASSED (2026-10-27).** Not
production-ready; no compliance claim; no RLS. Phase 2D — not started.
