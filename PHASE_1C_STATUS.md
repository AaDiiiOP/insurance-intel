# Phase 1C — Policy Intelligence: status (v0.11.0, 2026-10-07)

Status: **READY FOR PHASE 1C ACCEPTANCE** (2026-10-08).
Phase 1C was validated using the 3–4 company development benchmark
(AUR-SYN-001–004, real qwen2.5:7b): 0 silent wrong, 0 wrong answers, 28/28
Phase 1C policy checks correct, 24/24 Phase 1C probes, 28/28 Phase 1B probes.
Large-scale 20-company acceptance is deferred.

Previous version v0.10.3 (Phase 1B ready) → new version **v0.11.0** (minor
bump: new capability, Phase 1B behaviour preserved).

## A. Baseline
v0.10.3: 146 tests, 146 passed. Policy comparison 13/13. Phase 1B real-Qwen
development validation: 0 silent wrong, 0 wrong answers, 28/28 probes.

## B. Audit — what existed vs. what Phase 1C needed

| Capability | Before (v0.10.3) | Now (v0.11.0) |
|---|---|---|
| Policy entity, period, versions, corrections, contradictions | IMPLEMENTED (Phase 1B) | reused unchanged |
| Coverages, policy-wide limit, deductible, exclusions, endorsement text | IMPLEMENTED (model extraction) | reused unchanged |
| Policy number, insurer | MISSING | deterministic, labelled lines |
| Conditions (separate from exclusions/endorsements) | MISSING (only a rule *rejecting* conditions as endorsements) | deterministic, "Conditions" section |
| Definitions | MISSING | deterministic, `"Term" means …` |
| Sub-limits, and never confusing them with the coverage limit | MISSING | deterministic + sub-limit lines can never be a limit/sum insured |
| Per-coverage limits | MISSING (one policy-wide limit) | `coverage` entities (policy -provides-> coverage) with `coverage.limit` |
| Percentage deductibles | UNSAFE ("5% of loss" would normalise to 5) | kept as text, no amount |
| Endorsement id / effective date / policy reference | MISSING | deterministic; attached only if it references this policy |
| Document hierarchy (policy / schedule / endorsement / survey / loss run) | MISSING (all documents flat) | `document` entities with role, linked only when the policy number matches |
| Policy status current / expired / future | MISSING | from stated periods |
| Policy view, history, conflicts, comparison with change types | PARTIAL (`policy_comparison.py`, current facts only) | `policy_intelligence.py` |
| Policy questions (insurer, highest limit, which has a contradiction, endorsements effective, what changed) | MISSING | query layer, deterministic |

**Design choice:** policy *structure* is read by fixed rules from labelled
lines, never by the model. Every value is a verbatim line with its page, the
model's error surface is unchanged, and anything not written in labelled form
is reported as "not stated / needs review". Phase 1A fact scoring is untouched
(structure facts are kept out of the scored list).

## C. Models (all in the existing Knowledge Core)
- **Policy** — `policy` entity: `policy.number`, `policy.insurer`, period (effective_from/to on its facts), status (computed).
- **Coverage** — `coverage` entity per coverage named on the policy, `policy -provides-> coverage`, `coverage.limit` (money); `policy.coverage` (names, model-extracted) unchanged.
- **Limits / deductible / sub-limits** — `policy.coverage_limit`, `policy.deductible` (model, now with the % guard), `policy.sublimit` (deterministic, multi-valued, amount only when stated, never a percentage).
- **Endorsements** — `policy.endorsement` (what it does, model) + `policy.endorsement_ref` (id, effective from its own date to the policy end, deterministic).
- **Exclusions** — `policy.exclusion` (model, unchanged rules). **Conditions** — `policy.condition`. **Definitions** — `policy.definition`.
- **Documents** — `document` entities `{document_id, role}` -supports-> policy (or asset for surveys, business for loss runs).
- Period-bound: number, insurer, coverages, limits, deductible, exclusions, endorsement text, conditions, definitions, sub-limits carry the policy document's stated period; schedules/endorsements/surveys/loss runs never receive a period they do not state.

## D. Service, API, queries, UI
- `policy_intelligence.py`: `list_policies`, `get_policy(as_of)`, `get_policy_history`, `compare_policies` (previous vs current period of one policy, or two policies), `find_policy_conflicts`. It reuses the query layer's selection rules, so the policy view and the query API cannot disagree.
- Comparison change types: `policy_period_changed`, `insurer_changed`, `policy_number_changed`, `limit_increase/decrease`, `deductible_increase/decrease`, `coverage_limit_increase/decrease` (per coverage), `coverage/exclusion/endorsement/condition/definition/sublimit _added/_removed`, `unresolved_contradiction`, `unknown` (stated on one side only — never reported as "removed"). Each carries old and new evidence; percentage only when both sides are numbers.
- API (tenant-scoped, 404 for another organization's id): `GET /policies`, `GET /policies/{id}?as_of=`, `GET /policies/{id}/history`, `GET /policies/{id}/conflicts`, `POST /policies/compare`. Questions use the existing `POST /knowledge/query` (entity_id = policy or business).
- New question types: insurer, policy number, conditions, "what does X mean", sub-limits, the limit of a named coverage, which coverage has the highest/lowest limit (abstains if any coverage is contradictory or tied), which coverage has a contradiction, which endorsements became effective in the policy period, is the policy in force/expired, what changed from the previous policy / at renewal.
- Knowledge Explorer: a Policy panel on policy entities (header, status, coverages with limits, exclusions, conditions, definitions, sub-limits, endorsement ids, documents by role, conflicts, compare with previous period), each value with document and page.

## E. Validation

**Deterministic (done):**
- Tests: **166 passed** (146 → 166; 20 new: reader, wiring, %-deductible, sub-limit guard, policy view, two-policy and period comparison, conflicts, history labels, API + tenant isolation, 7 policy-question tests). One earlier test changed — `test_misleading_questions_do_not_get_a_stored_fact`: "insurer address" now routes to the new insurer type (and abstains because of the qualifier "address"); the test now asserts the protected outcome directly (no stored fact returned).
- Policy comparison benchmark 13/13. Phase 1B ground-truth simulation 40/40, 28/28 probes (unchanged).
- Structure reader on all 20 benchmark companies (app parser): roles 100/100 documents, policy number 20/20, insurer 20/20, conditions 20/20 (40 sentences), per-coverage limits 40/40 equal to ground truth, endorsement id/reference/date 20/20, nothing read from surveys or loss runs, 0 definitions / 0 sub-limits (none exist).
- Dry run of the Phase 1C checks on AUR-SYN-001–004 real documents (model off): 28/28 checks correct, 24/24 probes.

**Real Qwen (done, 2026-10-08 07:34 UTC, `result-20261008T073454`):**

    docker-compose exec backend python3 eval_property_policy.py /app/benchmark_data --only AUR-SYN-001,AUR-SYN-002,AUR-SYN-003,AUR-SYN-004 --query-check

Prints three gates: Phase 1A facts (silent wrong), Phase 1B query check
(+ 7 probes/company) and the new **policy check (Phase 1C)**: policy number,
insurer, conditions, each coverage's limit (vs ground truth), highest-limit
coverage (vs ground truth), endorsement effective dates; probes: definition
and sub-limit not stated → abstain, single period → no renewal diff, status
current in period, policy view equals query answers, cross-tenant → nothing.

| Field | Value |
|---|---|
| Version | v0.11.0 |
| Companies | AUR-SYN-001, 002, 003, 004 |
| Model | qwen2.5:7b (Ollama) |
| Date | 2026-10-08 |
| Phase 1A facts | recall 100%, false extraction 2.7% (2, both flagged contradictions), silent wrong **0**, evidence verified 100%, page accuracy 100% |
| Phase 1B query check | 40 questions: 34 correct, 6 abstained (5 contradictions + 1 occupancy restatement), **0 wrong**; probes **28/28** |
| Phase 1C policy check | 28 checks: **28 correct**, 0 abstained, **0 wrong**, answered with evidence 100%; probes **24/24** |
| Structure facts written | policy.number 8, insurer 4, conditions 8, coverage limits 8, endorsement ids 4 |

## F. Security
Tenant isolation is application-level (organization_id in every query), not
PostgreSQL RLS. Tested: every `/policies` endpoint and every policy question
from another organization returns 404 / nothing; documents naming a different
policy number are never attached to a policy. No authentication/RBAC yet (out
of scope).

## G. Limitations
- Structure is read from labelled lines; a policy that states its number, insurer, conditions or definitions in another layout is reported as not stated (safe, less complete). Real-world wordings will need more label forms.
- The benchmark has no definitions or sub-limits — those paths are tested only with fixtures.
- A coverage limit is per coverage only when written as "<Coverage> Sum Insured / Limit: amount".
- "What endorsements affect this coverage?" is a text match of the coverage name in the endorsement wording.
- 20-company acceptance deferred.
