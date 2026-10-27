# Phase 2D — Renewal & Submission Intelligence: status (v0.17.0, 2026-10-27)

Status: **COMPLETE — DEVELOPMENT GATE PASSED (2026-10-27).** Developer's Docker check (2026-10-27): reported by the developer as all passing and as expected (schema check, pytest, live check, browser flow incl. DOCX export); the console output was not pasted, so exact Docker counts are not recorded here.
Not production-ready; no compliance claim; application-level tenant isolation (no
PostgreSQL RLS). No new phase started. Built without a separate plan approval at the
developer's request (2026-10-27), as the smallest complete end-to-end workflow.

## Gap analysis (real code)
Reused unchanged: Phase 1C `compare_policies` (period vs period, absent = unknown),
Phase 1D business profile / changes (`not_reobserved`) / conflicts, Phase 2B client
intelligence values (state + evidence), Phase 2C review queue, `export_docx.build`,
roles (`drafts.create`, `drafts.approve`, `export`), audit. Missing: a renewal view per
client, a submission package built from verified data, explicit readiness rules,
approval tied to content, stale approval, DOCX of the package.

## Delivered
- `GET /clients/{id}/renewal` (`renewal_submission.py`): per linked policy the expiring
  and renewal periods (last two stated periods), the Phase 1C comparison between them
  with evidence and dates, business changes with not-re-observed values ("does NOT mean
  removed"), and conflicts. Status `compared`, `pending` (no renewal period documented)
  or `not_linked`. The Phase 1C label `<x>_removed` (multi-valued item not stated in the
  newer version) is shown as `<x>_not_restated` with that note; nothing is invented
  (no quote, premium or term).
- Submission packages (`submission_packages`, append-only `submission_package_events`):
  content = client/business fields, locations, operations, assets, controls, claims,
  risks, exposures, risk factors, policies (number, insurer, limit, sum insured,
  deductible, sublimits, exclusions, conditions, endorsements), coverage review counts,
  conflicts with evidence, fact review state, open review items — each value with its
  state (verified / read by rule / unverified / conflicting / missing) and evidence.
  Content fingerprint = SHA-256 of the content.
- Readiness rules (explicit, in `RULES`): business linked; legal name stated; a policy
  found; a current or documented renewal period; no unresolved conflicts; no facts
  awaiting verification; no facts without evidence; no open review items; no stale
  coverage decision. Each failing rule says how to fix it. Waivable rules accept a
  **reviewed exception** (Owner/Admin/Reviewer, reason required) tied to the current
  fingerprint — it stops counting when the content changes. Identity, legal name and
  policy presence cannot be waived.
- Lifecycle (server-side, 409 otherwise): check DRAFT|NEEDS_INFORMATION →
  READY_FOR_REVIEW or NEEDS_INFORMATION; approve READY_FOR_REVIEW → APPROVED
  (`drafts.approve`, readiness rechecked, approver + time + fingerprint recorded);
  export APPROVED|EXPORTED → EXPORTED (`export`, content must equal what was approved);
  reopen → DRAFT (reason). Every read rebuilds the content: if it changed, the package
  returns to DRAFT and the approval is no longer current (reapproval needed).
- `GET /submission-packages/{id}/export`: DOCX DRAFT (existing builder) with every value,
  its state and source document/page, "NOT FOUND IN THE DOCUMENTS", conflicts as
  "CONFLICTING — a vs b", and readiness exceptions with who and why. Nothing is sent.
- Audit: `submission.created`, `checked`, `exception_recorded`, `approved`,
  `approval_refused`, `exported`, `reopened`, `content_changed`.
- Frontend: "Renewal & submission" tab in Client intelligence (`RenewalPanel.jsx`).
- Live check unchanged (44 checks); Phase 2D is covered by pytest.

## Tests (sandbox, actually run)
- **273 passed, 0 failed, 0 skipped** (265 + 8 in `tests/test_phase2d_renewal_submission.py`):
  renewal periods, limit 300m → 250m, coverage added, period change, no "removed",
  not-re-observed note, conflicts kept; not-linked client; fields sourced or "missing";
  readiness blockers with fixes, exception rules (reason, non-waivable, broker 403,
  audited); full lifecycle with invalid transitions, broker cannot approve, DOCX content
  and download headers; change after approval → DRAFT, export refused, old exceptions
  void; append-only history; sign-in 401 and cross-tenant 404 on renewal, packages,
  exceptions, transitions and export; header 403.
- Dry run (model off) AUR-SYN-001–004: 1B 28/28 probes, 1C 28/28 + 24/24, 1D 53/53 +
  24/24, 1E 36/36 + 51/51 — unchanged.
- Live check (sandbox HTTP): 44/44. Frontend build OK; browser: renewal comparison,
  prepare draft, readiness check (needs information with reasons), no page errors.
- Not run this phase: Phase 1D acceptance audit (no engine changed; last 198/198 on
  v0.16.0), real Qwen. Docker: developer's check pending.

## Known limitations
- Renewal = the last two stated periods of the same policy; a replacement policy with a
  different number is not paired automatically.
- On an analyzed client the "facts awaiting verification" and "open review items" rules
  block until the facts are reviewed or an exception with a reason is recorded (by design).
- Package content includes the review state, so any review decision makes an approval
  stale (strict on purpose).
- Export is a GET that also moves the state to EXPORTED (audited).
- DOCX is a plain draft (no insurer template). Business-changes detail in the UI is a summary.
- All Phase 2A security limitations still apply.

## Acceptance gate (sandbox)
1 renewal evidence/dates/uncertainty PASS · 2 no invented values PASS · 3 missing info and
reviews visible PASS · 4 readiness transparent and enforced PASS · 5 approval stale on change
PASS · 6 DOCX authorized, audited, tested PASS · 7 tenant and role tests PASS · 8 regression
PASS (273/273, model-off gates) · 9 documented (this file).
**Phase 2D: COMPLETE — DEVELOPMENT GATE PASSED (2026-10-27).** Not production-ready; no RLS. No new phase started.
