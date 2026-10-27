# Phase 2B — Unified Client Intelligence Workspace: status (v0.15.0, 2026-10-27)

Status: **COMPLETE — DEVELOPMENT GATE PASSED (2026-10-27).** Verified in the
sandbox and on the developer's Docker deployment (see "Docker verification"). Not production-ready; no
compliance claim; tenant isolation is application-level (no PostgreSQL RLS).
Phases 2C and 2D are not started.

Starting point: v0.14.1 (Phase 2A frozen, commit b0abf91).

## Baseline (before any change, sandbox)
- Backend tests: **225 passed, 0 failed, 0 skipped.**
- Dry run (model off) AUR-SYN-001–004: Phase 1B 0 wrong + 28/28 probes; 1C
  28/28 + 24/24; 1D 53/53 + 24/24; 1E 36/36 + 51/51, evidence and page 100%.

## What was inspected (real code, not documentation)
- All intelligence already existed **per business**: `/businesses/{id}/profile`,
  `/changes`, `/conflicts` (Phase 1D), `/policies/{id}` `/history` `/conflicts`
  (Phase 1C), `/businesses/{id}/coverage-audit` (Phase 1E: mapping statuses,
  limit comparisons, findings, review state, coverage changes), evidence page
  text `/documents/{id}/text`, original-file download (Phase 2A).
- The client page (Phase 2A) showed paperwork and the document library; the
  intelligence was only reachable in the Knowledge explorer by picking a
  business from a list. The link existed in the data: `clients.business_entity_id`
  (Phase 2A) → business → `has_policy` → policies.
- Gap: no client-centric view. No engine was missing.

## Delivered
1. **`GET /clients/{client_id}/intelligence[?as_of=YYYY-MM-DD]`** —
   `backend/client_intelligence.py`, route in `api/workspace.py`, permission
   `view` (all four roles). Read-only; stores nothing.
   - The business is taken only from the client row in the signed-in
     organization; every engine call is scoped to that organization. A client
     id from another organization → 404 (same as every client route).
   - Sections: `profile` (legal name, workforce, survey rating with value
     state and evidence; counts of locations, operations, assets, controls,
     claims, risk factors, risks, exposures; sections not found; fact
     provenance: review status and how each fact was produced — read by rule
     / AI-extracted / OCR — and how many have no evidence), `policies` (each
     linked policy: number, insurer, limit, sum insured, deductible,
     sublimits, exclusions, conditions, endorsements as evidenced values;
     period status current / historical; coverages; conflicts), `coverage`
     (mapping status counts — MAPPED / PARTIALLY_MAPPED / UNMAPPED / UNCERTAIN
     / CONFLICTING / NEEDS_REVIEW as the engine returns them, finding counts,
     review counts, open findings with evidence, the engine's own note),
     `conflicts` (business and policy conflicts, all values, evidence),
     `changes` (summary between observation dates; `not_reobserved` count,
     explicitly not a removal), `documents` (processing status, errors,
     paperwork verdicts).
   - Every section has a status: `available`, `not_linked`, `not_analyzed`,
     `none_found`, `insufficient` or `unavailable` (with a short error). Each
     section runs in its own database savepoint, so one engine failing never
     hides the others or breaks the response.
   - `attention`: conflicts, open coverage findings, unverified facts,
     sections not found, failed documents, unavailable sections — each
     pointing to the tab that shows it.
   - Values are labelled `verified` (checked by a person), `read_by_rule`,
     `unverified`, `conflicting` (all values listed) or `missing` ("not found
     in the documents", with the engine's reason). Nothing is filled in.
2. **Frontend — "Client intelligence" on the client page**
   (`views/ClientIntelligence.jsx`): tabs Overview · Business & risks ·
   Policies · Coverage findings · Conflicts & changes · Documents; one "As of"
   date for every tab with a historical-view banner; evidence links that open
   the document/page text (`EvidenceRow` reused from the Knowledge explorer);
   explicit loading, empty, not-linked, not-analyzed, error, unavailable and
   permission-denied states; small-screen layout (no horizontal scrolling at
   390 px; the tab bar scrolls). The Business, Policies and Coverage tabs
   reuse the existing `BusinessPanel`, `PolicyPanel` and `CoveragePanel` with
   the client's business / policy ids — nothing is recomputed in the browser.
   Coverage review buttons are shown only to roles with `findings.review`
   (Owner, Admin, Reviewer); the server enforces it regardless. The document
   library (upload, download, run analysis, link business) is unchanged and
   refreshes the intelligence section after any change.
3. **Knowledge Core fix (found by the Phase 2B tests, approved by the
   developer)** — `knowledge/service.py`. Two same-date documents disagreeing
   (e.g. workforce 300 vs 310) and a LATER period then recorded (320): only
   one of the two earlier facts was closed, so the earlier period showed a
   single value and the conflict lost a side. Now the whole earlier period is
   closed together; both values stay visible for that date. Regression tests
   at engine and API level.
4. Live check: `phase2a_live_check.py` adds 3 Phase 2B checks (summary loads
   with all sections, bad date 400, another organization 404) — 43 checks.
5. Clean-up: `backend/api/organizations.py` removed (an unregistered leftover
   from before Phase 2A; the v0.14.0 docs said it was deleted, but extracting
   a zip over the repository had kept it).

## Tests (sandbox, actually run)
- **244 passed, 0 failed, 0 skipped** (225 + 19 new in
  `tests/test_phase2b_client_intelligence.py`). One library warning
  (starlette/anyio deprecation).
- New tests cover: summary equals the engines' own output (counts, policies,
  mapping/finding counts, open findings, conflicts, policy number and period
  status); document processing; evidence document/page/snippet; missing
  values say so with a reason; conflicting values all kept and flagged;
  point-in-time history (risks absent before their document date, present
  after; renewal date); "removed" never appears; no under-/over-insured or
  advice wording; a new client (all not_linked); a partly processed client
  (failed document shown); one engine failing leaves the rest working and
  leaks no internals; bad input (400 / 404); no session 401; all four roles
  read the same summary; another organization's real client id 404 and the
  header trick 403; a tampered client row pointing at another
  organization's business exposes nothing; the Knowledge Core fix.
- Phase 2A's route-wide tests include the new route automatically (needs a
  session; another organization's real ids return no data).
- Dry run (model off) AUR-SYN-001–004 on the final code: Phase 1B 0 wrong +
  28/28 probes; 1C 28/28 + 24/24; 1D 53/53 + 24/24; 1E 36/36 + 51/51,
  evidence verified 100%, page accuracy 100% — **unchanged**.
- Phase 1D acceptance audit (sandbox dry data): **198/198**.
- Live check over HTTP (sandbox backend, model off): **43/43 PASS**.
- Frontend: `vite build` OK. Browser (Playwright, real backend, synthetic
  AUR-SYN-004 / AUR-SYN-001 clients): every tab opens, evidence opens,
  historical as-of view, empty client shows "not linked", Broker sees 0
  review buttons, 390 px with no horizontal scrolling, no page errors.
- Not run: real-Qwen evaluation (the Knowledge Core fix changes behaviour
  only when a period with conflicting values is followed by a later period;
  the model-off gates are unchanged). Docker: not available in the sandbox —
  verified on the developer's machine instead (below).

## Docker verification (developer's machine, 2026-10-27)
Docker Compose, Windows, existing database; synthetic data only.
```
cd C:\Project\Perosnal_Projects
Expand-Archive -Path "$env:USERPROFILE\Downloads\insurance-intel-v0.15.0.zip" -DestinationPath . -Force
cd C:\Project\Perosnal_Projects\insurance-intel
git rm -q backend/api/organizations.py
docker-compose up -d --build
docker-compose exec backend python3 check_schema.py
docker-compose exec backend python3 -m pytest -q
docker-compose exec backend python3 phase2a_live_check.py http://localhost:8000
```
- `check_schema.py`: **PASS** (0 missing tables, 0 missing columns, 0 type differences).
- pytest: **244 passed, 0 failed, 0 skipped** (1 library DeprecationWarning), 62.6 s.
- Live check: **43 checks, 43 passed, 0 failed** — including "client
  intelligence summary loads (Phase 2B)", "as-of date is checked (400)" and
  the cross-tenant probe of `/clients/{id}/intelligence` (404). Run with
  Ollama stopped, as for Phase 2A.
- Browser (http://localhost:5183), by hand: signed in as Owner; a client
  linked to an analyzed business — every Client intelligence tab, an evidence
  link with "View page", an earlier "As of" date (historical note) and back
  to today; a new client shows "not linked" — all fine.

## Known gaps / limitations
- The existing business "changes" view lists one old value when the earlier
  period itself was in conflict (it is marked `contradiction`, so not
  silent); the Conflicts tab shows both values. Not changed in 2B.
- Evidence opens the page TEXT, not an image of the page.
- Some evidence has no page number (e.g. a few exclusions: shown as "p?"),
  as reported by the existing engines.
- The client must be linked to its analyzed business (Documents panel →
  link business); until then every intelligence section says "not linked".
- Historical view uses document dates; facts without a stated date follow
  the existing engines' rules.
- Fact review and correction stay in the Knowledge explorer (the Conflicts
  tab says where); coverage review is available in the Coverage tab.
- All Phase 2A security limitations still apply (no RLS, HTTPS / Secure
  cookie for real deployments, per-IP sign-in limit, no MFA).

## Acceptance gate
| # | Criterion | Sandbox result |
|---|---|---|
| 1 | Unified workspace works end-to-end with existing client data | PASS (sandbox browser + API; developer's Docker browser check) |
| 2 | Policy, risk, coverage, evidence, temporal information linked | PASS |
| 3 | Material facts sourced or clearly marked unavailable | PASS |
| 4 | Conflicts and uncertainties visible | PASS (after the Knowledge Core fix) |
| 5 | Tenant isolation and role permissions enforced | PASS |
| 6 | Phase 1A–1E and 2A regression | PASS (model-off gates, 1D audit 198/198, 225 earlier tests) |
| 7 | Frontend builds, backend tests pass | PASS (244/244 sandbox and Docker; live check 43/43 Docker) |
| 8 | Documentation accurate | this file |

**Phase 2B: COMPLETE — DEVELOPMENT GATE PASSED (2026-10-27).** Not
production-ready; no compliance claim; no RLS. Phases 2C and 2D — not started.
