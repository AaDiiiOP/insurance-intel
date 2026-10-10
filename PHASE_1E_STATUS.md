# Phase 1E — Risk → Coverage Intelligence: status (v0.13.0, 2026-10-09)

Status: **PHASE 1E DEVELOPMENT COMPLETE — READY FOR ACCEPTANCE** (2026-10-10).
Validated on the 4-company development benchmark (AUR-SYN-001–004, real
qwen2.5:7b): all five gates passed — 0 silent wrong, 0 wrong answers in every
gate, coverage check 33 correct + 3 correct abstentions, 51/51 probes,
evidence 100%, page accuracy 100%. 20-company validation is not claimed.

Previous version v0.12.1 (Phase 1D accepted, frozen) → **v0.13.0** (new capability).

## A. Inspection and baseline
Read: Knowledge Core (`knowledge/service.py` reconcile, versions, conflicts),
`policy_intelligence.py`, `business_intelligence.py`, `knowledge/query.py`,
`api/policies.py`, `api/businesses.py`, migrations, tests, eval scripts, and
every document of AUR-SYN-001–004.
Baseline: **177 tests, 177 passed, 0 failed, 0 skipped.**

What the 4 companies contain for this phase: each policy has Property Damage +
Business Interruption with their own stated limits, one deductible, two
exclusions, two conditions, one endorsement; the schedule's declared values
(Buildings / Plant and Machinery / Stock) are the property exposure; Phase 1D
adds controls, one explicit risk (AUR-SYN-004 ammonia leak) and AUR-SYN-001's
follow-up survey raising machinery to 210,000,000. No new synthetic documents
were needed for Phase 1E.

## B. Architecture (reused, nothing rebuilt)
`coverage_intelligence.py` reads ONLY through `business_intelligence`
(business state as of a date) and `policy_intelligence` (policy in force on a
date: coverages, limits, exclusions, conditions, sublimits, deductible,
endorsements, conflicts). It extracts nothing and stores no mapping or
finding — both are computed on read, so they cannot drift from the facts.
Four layers stay separate in every output: observed fact (document + page) →
derived relationship (mapping, `layer: derived_relationship`) → potential gap
(finding, `layer: potential_gap`) → human judgment (review).
No graph database.

## C. Coverage mapping model
Each mapping: source unit (property exposure / risk / explicit exposure),
target policy and coverage, mapping type, status, rationale, coverages
considered, evidence, limit comparison, relevant exclusions / sublimits,
deterministic `mapping_id`, review status. Tenant = the organization it is
computed in.

- Property exposure = sum of current documented declared values. Maps to the
  in-force coverage whose name is a property coverage. **EXPLICIT_POLICY_LINK**
  when a declared-value document names that policy number (the schedule),
  else **DETERMINISTIC_DOMAIN_RULE**.
- Risks / explicit exposures are put in categories by a fixed keyword list
  (machinery, stock/goods, buildings, fire, flood, earthquake, storm, theft,
  business interruption). **DIRECT** when an in-force coverage name names the
  category; **UNCERTAIN** when only a broad property coverage could relate to a
  physical peril; **UNMAPPED** otherwise ("No relevant coverage was identified
  in the available policy documents" + coverages considered). Never inferred
  from industry; risk factors are context only.
- Status: MAPPED / PARTIALLY_MAPPED (exceeds limit, or a relevant exclusion or
  sublimit) / CONFLICTING (limit or sum insured contradicted) / UNCERTAIN /
  UNMAPPED / NEEDS_REVIEW (limit or exposure unknown) / NOT_APPLICABLE (taxonomy).
- Limit comparison: WITHIN_DOCUMENTED_LIMIT / EXCEEDS_DOCUMENTED_LIMIT /
  LIMIT_UNKNOWN / EXPOSURE_UNKNOWN / NEEDS_REVIEW. Wording: "The documented
  exposure amount (X) is greater than the stated Property Damage limit (Y)" —
  never "underinsured".
- Coverages only *named* in the policy (no stated limit) are included with
  LIMIT_UNKNOWN.

## D. Finding model
UNMAPPED_RISK, UNMAPPED_EXPOSURE, EXPOSURE_EXCEEDS_LIMIT, LIMIT_EXCEEDS_EXPOSURE,
SUBLIMIT_REVIEW ("Relevant sublimit identified", or applicability not
established), EXCLUSION_REVIEW ("Exclusion identified in the available policy
documents that may affect …"), CONDITION_REVIEW (a related control is
documented / not documented; "not a finding that the condition is breached",
"missing evidence is not a breach"; also operation changes vs a
notify-changes condition), DEDUCTIBLE_REVIEW (contradictory or percentage
deductible), COVERAGE_CONFLICT and POLICY_PERIOD_CONFLICT (from the existing
contradiction framework, never resolved), MISSING_EVIDENCE, UNCERTAIN_MAPPING.
Every finding: deterministic id, statement, subject, policy, coverage,
evidence (document, page, snippet, fact id, role), rationale, fingerprint,
`needs_review: true`, "For broker review — not a coverage or underwriting
decision". A finding without evidence is marked `certainty: UNCERTAIN`.

## E. Temporal behaviour and changes
- Policies are used only when a stated period covers the date; the business
  state is as of the same date (a future-dated observation is never "current").
- Before a policy period: "No policy period in the available documents
  includes this date."
- Coverage changes: coverage map as of A vs as of B (default: the two latest
  dated states — business observations, policy starts, endorsement dates):
  status_changed / newly_mapped / unchanged / not_in_effect_on_later_date
  (never "removed"). Plus Phase 1C policy-period comparison with the related
  documented exposure attached to every limit change, status NEEDS_REVIEW.
- Endorsements: id, effective date, in effect on the date, affected coverage /
  exposure category.

## F. Human review
One new table `kc_coverage_reviews` (append-only; migration in
`repositories/connection.py`): organization, business, subject (mapping or
finding id), decision OPEN / REVIEWED / ACCEPTED / REJECTED /
NEEDS_MORE_EVIDENCE, reviewer, comment, evidence snapshot, timestamp,
fingerprint. A review applies only to the evidence it saw: if the finding's
values or evidence change, it returns to OPEN with `review_stale: true` and the
history is kept. (Fact-level review stays in the existing fact review flow;
mappings and findings are not facts, so they needed their own decisions table.)

## G. APIs, questions, graph, UI
APIs (organization-scoped; another organization's id → 404; bad date → 400):
`GET /businesses/{id}/coverage-map?as_of=`, `/coverage-findings`,
`/unmapped-risks`, `/coverage-conflicts`, `/coverage-changes?from_date=&to_date=`,
`/coverage-audit`, `/coverage-graph`; `POST /coverage-findings/{id}/review`,
`POST /coverage-mappings/{id}/review` (body: business_id, decision, reviewer, comment).

Questions (`POST /knowledge/query`): relevant coverage, unmapped risks,
exposures exceeding limits, exclusions affecting risks, conditions needing
review, coverage status changes, which policies protect the exposure, mapping
evidence, findings for broker review, coverage conflicts, coverage state as of
a date. "Is the company adequately insured / underinsured / should it buy more
insurance?" → **declined** (answer none, `declined_judgment`), with the
findings listed for review. Existing Phase 1B/1C/1D questions keep their
intents and answers (verified: identical answers to v0.12.1 on the same data).

Graph: `/coverage-graph` = the Phase 1D graph unchanged + edges marked
`derived: true` (mapped_to_coverage, potentially_affected_by_exclusion,
has_limit, condition_refers_to_control), each with rule/status/evidence.

UI: Knowledge Explorer → Coverage audit panel under the Business panel:
policies in force, risks/exposures, mappings with status and limit
comparison, deductibles, endorsements, findings grouped by type with review
buttons (reviewer name required), changes. Checked at 1200 px and 390 px, no
console errors.

## H. Fixes to existing code (found by Phase 1E, minimal)
1. **Knowledge Core silent-wrong (genuine defect):** a value for an EARLIER
   policy period read after a renewal was compared with the current (later)
   period, stored as history, and then answered for the earlier period even if
   it disagreed with that period's value. Now it is compared with the same
   period's value: agreement merges evidence; disagreement flags both and opens
   a conflict. `knowledge/service.py` (`_same_period_fact`). Regression test
   added; both new tests fail on the old code.
2. **Deterministic document order:** documents uploaded in the same
   transaction had no defined order (`ORDER BY uploaded_at` only), which made
   one existing test fail once in a full-suite run. Tie-break by filename, id.
No Phase 1D behaviour changed (Phase 1D acceptance audit: 198/198 on the new code).

## I. Validation
**Deterministic (done):**
- Tests: **199 passed** (177 → 199; 22 new in `tests/test_coverage_intelligence.py`
  covering all 24 required areas: mappings, direct / partial / unmapped /
  uncertain, limits, exposure vs limit, sublimits, deductibles, exclusions,
  conditions, endorsements, temporal coverage, policy period changes,
  conflicts, change detection, evidence, missing evidence, human review,
  tenant isolation, query safety, graph, regression of earlier intents).
- Same data, v0.12.1 vs v0.13.0 code: Phase 1B 68/68, 1C 52/52, 1D 49/49 answers
  and probes identical. Phase 1D acceptance audit 198/198.
- Full dry run, model off, AUR-SYN-001–004: Phase 1B 0 wrong + 28/28 probes;
  1C 28/28 + 24/24; 1D 53/53 + 24/24; **1E coverage check 36/36 correct, 0 wrong,
  51/51 probes, evidence verified 100%, page accuracy 100%.**
- Simulated real-model conflicts (AUR-SYN-004 schedule total 370M vs 350M;
  AUR-SYN-001 policy-wide limit 600M vs 120M): limit comparison abstains
  (NEEDS_REVIEW), conflicts surfaced, 0 wrong.

Development results on the 4 companies (current date):

| Company | Property exposure vs Property Damage limit | Findings |
|---|---|---|
| AUR-SYN-001 | 630,000,000 > 600,000,000 → EXCEEDS (was WITHIN until the 15 Sep 2026 survey) | exposure exceeds limit; machinery wear-and-tear exclusion; fire-protection condition |
| AUR-SYN-002 | 300,000,000 = 300,000,000 → WITHIN | fire-protection condition |
| AUR-SYN-003 | 500,000,000 = 500,000,000 → WITHIN | goods-in-transit exclusion (Stock); fire-protection condition |
| AUR-SYN-004 | 350,000,000 vs 350,000,000 — NEEDS_REVIEW if the schedule's 370,000,000 total is read as a second sum insured | ammonia-leak risk UNMAPPED; stock-spoilage exclusion; fire-protection condition |

**Real Qwen (done, 2026-10-10 04:56 UTC, `result-20261010T045655`):**

    docker-compose exec backend python3 eval_property_policy.py /app/benchmark_data --only AUR-SYN-001,AUR-SYN-002,AUR-SYN-003,AUR-SYN-004 --query-check

Prints five gates; the new one is **coverage check (Phase 1E)**. Expected
outcomes: `backend/benchmark_phase1e/` (hand-written from the documents).

| Field | Value |
|---|---|
| Version | v0.13.0 |
| Companies | AUR-SYN-001–004 |
| Model | qwen2.5:7b |
| Date | 2026-10-10 |
| Phase 1A facts | recall 100%, false extraction 2.6% (2, both flagged contradictions), silent wrong **0**, evidence verified 100%, page accuracy 100% |
| Phase 1B query check | 40 questions: 35 correct, 5 abstained (all source contradictions), **0 wrong**; probes **28/28** |
| Phase 1C policy check | **28/28** correct, 0 wrong; probes **24/24** |
| Phase 1D business check | **53/53** correct, 0 wrong; probes **24/24** |
| Phase 1E coverage check | 36 checks: **33 correct, 3 abstained, 0 wrong**; probes **51/51**; finding evidence verified **100%**, page accuracy **100%** (0 without a page) |
| Abstentions | all AUR-SYN-004: its schedule prints a 370,000,000 total while its items add up to 350,000,000, so the documents disagree on the sum insured; limit comparison, as-of comparison and change window correctly say NEEDS_REVIEW instead of choosing a value |
| Fabricated insurance conclusions | 0 (probe: no "underinsured / not covered / breach" wording; advice questions declined) |
| Tenant isolation | cross-tenant probe passed for all 4 companies |

## J. Security and tenant isolation
Application-level isolation (organization_id in every query), not PostgreSQL
RLS. Tested: every coverage endpoint and review endpoint returns 404 for
another organization; questions from another organization return nothing;
review requires a current mapping/finding of that business in the caller's
organization. **Findings:** no authentication — the reviewer name is
self-declared and X-Organization-Id is trusted (documented pilot boundary;
needs auth/RBAC before production). Review comments are stored as text and
rendered as text (React escapes them).

## K. Limitations
- Category vocabulary is small and fixed; risks described in other words are
  UNMAPPED (safe) rather than guessed. Risk factors are not mapped.
- Exposure = declared values only; no business-interruption exposure is
  documented, so BI coverage is listed as "no documented related exposure".
- The deductible is stated policy-wide; applicability per coverage is reported
  as not stated.
- Sublimits, percentage deductibles, renewals and machinery-breakdown coverage
  are not in the benchmark — tested with fixtures only.
- Exclusion text is model-extracted (Phase 1A); a wording the category list
  does not recognise is not matched.
- 20-company acceptance not claimed. No Phase 2 work.
