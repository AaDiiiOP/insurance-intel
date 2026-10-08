# Phase 1D — Business Risk Intelligence & Business Risk Graph: status (v0.12.0, 2026-10-08)

Status: **DEVELOPMENT COMPLETE · ACCEPTANCE PASSED · FROZEN** (2026-10-09).
**Phase 1D accepted using 4-company validation** (AUR-SYN-001–004, real qwen2.5:7b). 20-company acceptance is NOT claimed (deferred).
**PHASE 1D ACCEPTANCE PASSED — READY FOR PHASE 1E.** No further Phase 1D work.
Phase 1D validated on the 3–4 company development benchmark (AUR-SYN-001–004,
real qwen2.5:7b): 0 silent wrong, 0 wrong answers in every gate, business
check 53/53 correct, all probes passed (Phase 1B 28/28, Phase 1C 24/24,
Phase 1D 24/24). 20-company acceptance deferred.

Previous version v0.11.0 (Phase 1C ready) → new version **v0.12.0** (minor
bump: new capability; Phase 1A/1B/1C behaviour preserved).

## A. Baseline
v0.11.0: 166 tests, 166 passed. Policy comparison 13/13. Phase 1C real-Qwen
development validation (2026-10-08): 0 silent wrong, query 0 wrong + 28/28
probes, policy check 28/28 + 24/24 probes.

## B. Inspection — what the data and code had

| Item | Before (v0.11.0) | Benchmark data (AUR-SYN-001–020) | Now (v0.12.0) |
|---|---|---|---|
| Business entity, policy link | IMPLEMENTED | yes | reused |
| Location | address fact only on the property asset | "Location:" in policy/survey | `location` entities, `has_location` |
| Operations | occupancy fact (model) | "The insured operates X." in survey | `operation` entities, deterministic |
| Workforce | MISSING | "Estimated workforce: N" | `business.workforce` |
| Surveyor risk rating | MISSING | "Overall risk rating: X" | `business.survey_risk_rating` (the surveyor's, not ours) |
| Controls | MISSING | one "Fire protection:" line per company | `control` entities |
| Asset values | sum insured (model) | "Declared Values" block, 3 assets | `asset.declared_value` per asset |
| Losses | MISSING | loss count + total incurred only | `loss.claims_count`, `loss.total_incurred` on a `claim` (loss-run) entity |
| Risk factors | model fact on the policy | "Risk Factors:" line | `risk_factor` entities |
| Explicit risks / exposures | MISSING | **none stated** | readers ready; tested on the supplement |
| More than one observation date | — | **one period only** | tested on the supplement |

Because the benchmark has no explicit risks, no exposures and only one period,
a small **synthetic, clearly labelled supplement** was added (allowed by the
brief): `backend/benchmark_phase1d/` (generator: `backend/gen_phase1d_supplement.py`).

| Company | Supplement | What it tests |
|---|---|---|
| AUR-SYN-001 | follow-up survey 15 Sep 2026: workforce 812, Additional Location (Chakan), sprinklers added to fire protection, machinery 210,000,000 | added / changed / % change |
| AUR-SYN-002 | survey 20 Sep 2026: rating Elevated, Security control; loss run 20 Sep 2026: 2 claims, 2,750,000 | added control, changed rating, changed losses |
| AUR-SYN-003 | survey 30 Sep 2026 restating the same values | no false changes |
| AUR-SYN-004 | addendum dated the same day as the original survey: workforce 290 (vs 282), "Identified risk: Ammonia refrigeration leak…" | contradiction + explicit risk |

## C. Ontology (in the existing Knowledge Core — no graph database)
Entities: business, business_unit, location, asset, operation, exposure, risk,
risk_factor, control, claim, policy, document (new: business_unit, operation,
exposure, risk_factor, control).
Relationships (validated by type): has_unit, has_location, performs_operation,
supports_operation, contains_asset, owns, has_exposure, has_risk,
has_risk_factor, has_control, mitigated_by (risk→control only), has_claim,
related_to_risk, has_policy, insures (policy→business/asset/location/exposure),
supports.
Facts: business.legal_name, business.workforce, business.survey_risk_rating,
location.address, operation.description, control.description,
asset.declared_value, loss.claims_count, loss.total_incurred, risk.description,
risk_factor.description, exposure.description — registry source "business";
kept out of Phase 1A scoring.

**Why no Neo4j:** the graph is two hops deep and small per business;
`kc_entities` + `kc_relationships` answer every required query with indexed
lookups, keep evidence/versions/conflicts in one place, and keep tenant
isolation in one code path. A graph database is not justified by this phase.

**Database migration:** one — the `kc_entities.entity_type` CHECK is dropped
and re-added with the five new types (idempotent, in `MIGRATIONS`).

## D. Temporal and observation model
- Each value is an observation dated by its own document: "Survey date",
  "Report date", "Inspection date", "Effective" — or, for a policy, schedule
  or loss run without one, the policy period start. Stored as
  effective_from = effective_to = that date.
- Current = the latest observation; as of a day = the latest observation on or
  before that day; history keeps every earlier observation with evidence.
- Two different values on the same date = contradiction (flagged, never picked).
- A later document stating a different value is history, not a contradiction.
- Undated observations are used only for current questions, and only when no
  dated observation exists.

## E. Change detection rules
Compares the business state as of date A vs as of date B (default: the two
latest observation dates; `?from_date=&to_date=` to choose).
- Re-observed by B: **added** (not known at A), **changed** (with old/new
  value, both evidences, absolute and % change for numbers), **unchanged**.
- Unresolved contradiction: **conflicting** (both values, both evidences).
- Not re-observed by B: listed as `not_reobserved` — **never reported as
  removed** (absence of a statement is not removal).
- Fewer than two observation dates → "insufficient", nothing invented.

## F. APIs (tenant-scoped; another organisation's id → 404; bad date → 400)
`GET /businesses`, `/businesses/{id}`, `/profile?as_of=`, `/locations`,
`/assets`, `/operations`, `/exposures`, `/risks`, `/risk_factors`, `/controls`,
`/claims`, `/changes?from_date=&to_date=`, `/conflicts`, `/graph`.

## G. Query behaviour (`POST /knowledge/query`, business or its policy)
New intents: business profile, locations, operations, assets, controls,
losses, exposures, risks, business changes ("what changed / what is new"),
workforce, surveyor rating; "contradictions" now includes business conflicts.
- "Is there flood risk?" with nothing stated → **abstains**: "No documented
  evidence of flood risk was found in the available documents." Risks are
  never inferred from industry or operations.
- "What changed during renewal?" with one policy period → still abstains
  (policy words never fall back to business changes).

## H. Frontend
Knowledge Explorer → Business panel when a business entity is open: legal
name, workforce, surveyor rating, locations, operations, assets, exposures,
risk factors, risks, controls, claims/losses, policies, unresolved conflicts,
changes (added / changed / unchanged / conflicting + not re-observed), every
value with document and page. Respects the explorer's "as of" date. Checked
with Playwright at 1200 px and 390 px; no console errors. (The explorer page as
a whole already scrolls sideways at 390 px — pre-existing, not from this panel.)

## I. Validation

**Deterministic (done):**
- Tests: **177 passed** (166 → 177; 11 new in `tests/test_business_intelligence.py`:
  reader, profile, relationships/graph (mitigated_by only risk→control),
  changes, as-of + history, same-date contradiction, correction resolving a
  conflict, business questions, unsupported-risk abstention, API + tenant
  isolation, renewal-question regression).
- Policy comparison 13/13; Phase 1B simulation 40/40 + 28/28 probes (unchanged).
- Business reader on all 20 companies vs independent ground truth: location,
  operation, workforce, rating, losses, legal name 20/20; 1 control and 3 assets
  each; 0 risks / 0 exposures invented.
- Dry run (model off) on AUR-SYN-001–004 real documents + supplement:
  Phase 1B probes 28/28; Phase 1C 28/28 correct, 24/24 probes;
  **Phase 1D 53/53 correct, 0 wrong, 24/24 probes** (supplement used for 4 companies).
- `recheck_query.py` (no model) on the dry-run organisation: query 0 wrong;
  policy 28 correct / 0 failed probes; business 25 correct / 0 wrong /
  0 failed probes; exit 0.

**Found and fixed during validation:**
- Regression: the new business-change fallback answered "What changed during
  renewal?" (one policy period) — 4 Phase 1C probes failed. Fixed: policy
  words never fall back; regression test added.
- Recheck graded stage A against a state the supplement had already updated
  (6 false "wrong"). Fixed: when the supplement is present, the recheck grades
  stage B only.

**Real Qwen (done, 2026-10-08 16:50 UTC, `result-20261008T165020`):**

    docker-compose exec backend python3 eval_property_policy.py /app/benchmark_data --only AUR-SYN-001,AUR-SYN-002,AUR-SYN-003,AUR-SYN-004 --query-check

Must print four gates with 0 silent wrong, 0 wrong answers and all probes
passed: Phase 1A facts, Phase 1B query check, Phase 1C policy check,
**business check (Phase 1D)**.

| Field | Value |
|---|---|
| Version | v0.12.0 |
| Companies | AUR-SYN-001–004 |
| Model | qwen2.5:7b (Ollama) |
| Date | 2026-10-08 |
| Phase 1A facts | recall 100%, false extraction 4.1% (3, all flagged contradictions — none silent), silent wrong **0**, evidence verified 100%, page accuracy 100% |
| Phase 1B query check | 40 questions: 34 correct, 6 abstained (all source contradictions), **0 wrong**; probes **28/28** |
| Phase 1C policy check | 28 checks: **28 correct**, **0 wrong**; probes **24/24** |
| Phase 1D business check | 53 checks: **53 correct**, 0 abstained, **0 wrong**; probes **24/24** (supplement used for 4 companies) |
| Business facts written | declared values 17, locations 13, legal name 9, workforce 8, controls 8, operations 8, rating 7, loss count 5, incurred 5, risk factors 4, risks 1 |

## J. Security and tenancy
Tenant isolation is application-level (organization_id on every query), not
PostgreSQL RLS. Tested: every `/businesses` endpoint and every business
question from another organisation returns 404 / nothing. No
authentication/RBAC (out of scope).

## K. Limitations
- The benchmark states no explicit risks or exposures; those paths are tested
  only on the synthetic supplement and fixtures.
- Losses are summary-only (count, total incurred) — no claim dates, perils or
  per-claim records exist in the data.
- The supplement is synthetic; real-world documents will need more label forms.
- `mitigated_by` (risk → control) is only recorded when a person links them;
  it is never inferred.
- Values are read from labelled lines; anything written another way is
  reported as "not documented" (safe, less complete).
- 20-company acceptance deferred. Phase 1E not started.

## L. Final 4-company acceptance gate (v0.12.1, 2026-10-09)

Scope: AUR-SYN-001–004 only; real qwen2.5:7b; no 20-company run; no new
Phase 1D features.

**Pre-test inspection**
- Implementation files unchanged since the real-Qwen run (checksums of
  `business_structure.py`, `business_intelligence.py`,
  `property_policy_intelligence.py`, `knowledge/query.py`, `api/businesses.py`,
  `eval_property_policy.py` identical before and after this gate).
- Phase 1D tests: `tests/test_business_intelligence.py` (11). Phase 1D eval:
  `eval_property_policy.py --query-check` (4th gate) + `recheck_query.py`.
- Data: 4 companies × 5 documents + `benchmark_phase1d/` supplement (5 PDFs,
  4 ground-truth files). Qwen: available on the developer's machine only
  (not in the build sandbox).

**Baseline (before anything else)**
- Full suite: 177 tests — 177 passed, 0 failed, 0 skipped.
- Phase 1D tests: 11 passed. Policy comparison 13/13.

**Real-Qwen run (2026-10-08 16:50 UTC, `result-20261008T165020`, organization
`cabeb69a-9bb6-4fc6-bde1-e6ba66545efc`)** — all four gates passed: Phase 1A
silent wrong 0 (recall 100%, evidence 100%, page 100%); Phase 1B 0 wrong,
probes 28/28; Phase 1C 28/28, probes 24/24; Phase 1D 53/53, 0 wrong,
probes 24/24.

**Gap found in the evaluation (not the implementation):** the existing gates
answer questions but did not audit every *persisted* business fact. Items the
gate requires that were not measured: evidence verification and page
accuracy of business facts; "nothing invented" across all controls / claims /
assets / locations / exposures; relationship validity; Graph / Change / Risk
APIs over HTTP for both tenants; no risk score. Added
**`phase1d_acceptance_audit.py`** — read-only, no model, audits the stored
state of the real-Qwen run against independent expectations (company profile,
ground truth, Phase 1 ground truth, supplement ground truth). Sections A–H,
198 checks over 4 companies.

**Evaluator issue found and fixed (classified: evaluator):** a first audit
version compared AUR-SYN-004's schedule asset values with the schedule's
printed total. The document's own items add to 350,000,000 but it prints
370,000,000 (a deliberate benchmark inconsistency already handled as a
contradiction in Phase 1B). The stored values equal the document's lines, so
the check now compares with the schedule's item lines. No implementation
change.

**Audit self-test:** injecting an invented "Flood risk" and "Flood zone"
exposure (rolled back) makes the audit fail exactly those two checks.

**Dry audit (sandbox, deterministic model stub — NOT the acceptance result):**
198 checks, 198 passed; business facts evidence-verified 100%, page accuracy
100%.

**Acceptance result on the real-Qwen data — PASSED (2026-10-09 00:42 IST / 2026-10-08 19:12 UTC, `phase1d-acceptance-20261008T191206`):**

    docker-compose exec backend python3 phase1d_acceptance_audit.py /app/benchmark_data cabeb69a-9bb6-4fc6-bde1-e6ba66545efc

| Field | Value |
|---|---|
| Acceptance date | 2026-10-09 (IST) |
| Companies | AUR-SYN-001, 002, 003, 004 |
| Model | qwen2.5:7b (Ollama) |
| Total checks / passed / failed | audit 198 / 198 / 0 (A 12, B 53, C 20, D 10, E 6, F 29, G 28, H 40); plus real-Qwen gates: Phase 1D 53/53 + 24/24 probes, Phase 1C 28/28 + 24/24, Phase 1B 0 wrong + 28/28 |
| Silent wrong | **0** |
| Wrong business answers | **0** |
| Unsupported risks / controls / claims / assets / locations / exposures | **0** (only the one explicitly stated risk, AUR-SYN-004; 0 exposures; no risk score anywhere) |
| Evidence verification (80 business facts) | **100%** |
| Page accuracy | **100%** |
| Contradiction results | 100%: exactly the expected set (AUR-SYN-004 workforce 282 vs 290), both values kept, never answered |
| Change detection | 100%: every expected added / changed / unchanged / conflicting correct; 0 unexpected; never "removed"; not-re-observed never reported as change |
| Tenant isolation | 100%: every /businesses endpoint 404 with no data for another tenant; list excludes business; query API and query layer return nothing |
| Regression tests | 177/177 (0 failed, 0 skipped) |
| Final status | **PASS — Phase 1D accepted using 4-company validation; frozen** |

