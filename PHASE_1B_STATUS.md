# Phase 1B — Knowledge Core + Knowledge Query: status (v0.10.3, 2026-10-07)

Status: **READY FOR PHASE 1B ACCEPTANCE** (2026-10-07).
Phase 1B implementation validated on the current 3–4 company development
benchmark (AUR-SYN-001–004, real qwen2.5:7b): 0 silent-wrong facts, 0 wrong
answers, 0 failed safety probes. The larger 20-company acceptance benchmark is
deliberately deferred; Phase 1B is not claimed as accepted on 20 companies.

## A. Baseline (before this loop)
- Version v0.10.1. Test suite: 128 tests, 128 passed, 0 failed, 0 skipped, 0 errors.
- Policy comparison benchmark: 13/13.
- Last real-Qwen run (v0.10.1, AUR-SYN-001–004, `--query-check`): 0 silent wrong, 0 wrong answers (reported by the developer).

## B. Audit — capability map (verified against code and tests, not docs)

| Capability | State | Evidence |
|---|---|---|
| Entities, types, tenant scoping | IMPLEMENTED | `knowledge/entities.py`, `repositories/knowledge.py`; isolation tests |
| Entity normalization / aliases / ambiguity | IMPLEMENTED | exact → single normalized match + alias; ≥2 matches → new entity; look-alikes only flagged (`test_entity_normalization.py`) |
| Facts: typed, singleton vs multi-valued, confidence, review | IMPLEMENTED | `fact_types.py`, `service.reconcile_fact`, `review_fact` |
| Evidence: document, page, section, snippet, verification | IMPLEMENTED | `kc_evidence`; snippet verified against full document text in extraction |
| Temporal: valid_from/to, effective period, as-of, history | IMPLEMENTED — **2 bugs found and fixed (below)** | `versions.py`, `query.py` |
| Corrections / supersession / history | IMPLEMENTED — **bugs fixed (below)** | `facts.supersede_fact` |
| Contradictions surfaced, never silently resolved | IMPLEMENTED | conflict rows; current value null; query abstains with both sides' evidence |
| Knowledge query, deterministic first | IMPLEMENTED — **routing bugs fixed (below)** | `knowledge/query.py`, `POST /knowledge/query` |
| LLM path cannot invent facts | IMPLEMENTED | quote must be in passage, every number in passage; else abstain; always unverified |
| Policy comparison with evidence, %, assessment | IMPLEMENTED | `policy_comparison.py`; 13/13 |
| Tenant isolation | IMPLEMENTED, **application-level** (no PostgreSQL RLS) | new full-endpoint audit test |
| Knowledge Explorer: entity → facts → relationships → evidence → history → conflicts → review | IMPLEMENTED | `KnowledgeExplorer.jsx` |
| Duplicated / unsafe code | none found in Phase 1B scope | SQL f-strings interpolate only fixed column names; no text/snippet logging |

## C. Problems found and fixed in this loop

1. **Correcting last year's value made it "current" again (UNSAFE).** A
   correction of a closed (historical) row created an OPEN row, so the
   current-value API, policy comparison and the Explorer showed the 2025 limit
   as current beside the 2026 one. Fix: correcting a historical row creates a
   historical (closed) correction.
2. **A human correction was ignored by the query layer (SILENT WRONG).**
   Corrections carried no evidence, the query layer (correctly) uses only
   evidenced facts, so it fell back to the corrected-AWAY value. Fix: a
   correction carries the corrected fact's evidence, marked
   `source = reviewer_correction` (shown as such in the Explorer).
3. **An already-replaced value could be corrected again**, letting an old
   correction override a newer one. Fix: refused ("correct the latest version").
4. **Query routing gave stored facts to questions that did not ask for them
   (WRONG ANSWERS):** "time limit for notifying a claim" / "age limit of the
   building" → coverage limit; "when was the building built" → construction;
   "insurer / broker address" → property location; "what endorsements were
   added" → renewal diff. Fix: no bare "limit"/"built"; location not used when
   the question is about the insurer/broker/agent/office; bare "added/removed"
   no longer means "compare periods".
5. **Peril-specific questions got the policy-wide value (WRONG ANSWER):**
   "deductible for flood", "limit of indemnity for business interruption".
   Fix: words left after removing the fact phrase, entity name, dates and
   generic words are a *qualifier*: singleton → abstain, policy-wide value
   offered as a candidate; multi-valued → yes-with-evidence if listed, else
   abstain ("not among the recorded values; absence is not proof").
6. Input validation: question length capped at 1,000 characters.

## D. Tests
- 146 tests, 146 passed (18 new this loop: the undated-outside-period reproduction, plus 3 correction, 6 routing, 2
  peril-specific, membership, list-vs-diff, length, probes, 2 tenant-isolation
  audit tests covering every Knowledge Core / query / comparison / document
  endpoint for reads AND writes).
- Policy comparison 13/13. Ground-truth simulation (perfect facts for
  AUR-SYN-001–004 graded by the query check): 40/40 correct, 28/28 safety probes.

## E. Real-Qwen development validation (qwen2.5:7b, AUR-SYN-001–004)

**Run 1 — v0.10.2, 2026-10-07 07:07 UTC** (`result-20261007T070750`, org 021b22eb-…):

| Metric | Value |
|---|---|
| Silent wrong facts | **0** (hard gate passed) |
| Fact recall | 96.2% (AUR-SYN-001 construction/occupancy missed — see note) |
| False extraction | 2.9% (2, both flagged as contradictions) |
| Evidence snippet verified / traceable / page accuracy | 100% / 100% / 100% |
| Query questions | 40: 34 correct, 6 abstained, **0 wrong** |
| Safety probes | 28: **1 failed** — AUR-SYN-001 `date_before_any_policy_period` answered 600,000,000 |

Note: Ollama returned HTTP 500 on one AUR-SYN-001 call (`model_call_failed: 1`),
so that company's policy-document extraction was partial — the coverage limit
existed only UNDATED (from the schedule). The query layer then gave that
undated value for a date before the policy began. **Real logic gap, fixed in
v0.10.3:** an undated value of a period-bound fact type (limit, deductible,
sum insured, coverage, exclusion, endorsement) is never given for a day outside
every policy period known for the entity. Reproduced deterministically
(old code: probe fails with 600,000,000; new code: abstains) and covered by a
test.

**Run 2 — v0.10.2, 2026-10-07 07:44 UTC, AUR-SYN-001 only** (`result-20261007T074446`,
clean model run): recall 100%, false extraction 0%, silent wrong 0, evidence
100% / page 100%; query 10 questions: 9 correct, 1 abstained (coverage-limit
contradiction), 0 wrong; probes 7/7 passed.

**Abstentions (safe, not errors):** coverage-limit contradictions (AUR-SYN-001, 002, 003),
AUR-SYN-003 occupancy (risk-survey restatement), AUR-SYN-004 sum insured
contradiction, AUR-SYN-001 construction/occupancy missing in Run 1 (model failure).

**Final confirmation (v0.10.3, no model, seconds)** — re-grades the facts Run 1
already stored, including the failure case:

    docker-compose exec backend python3 recheck_query.py /app/benchmark_data 021b22eb-5b30-48e7-9bc8-7916b4649bf0

Result (2026-10-07): 0 wrong answers, 0 failed probes, hard gate passed.

| Field | Value |
|---|---|
| Version | v0.10.3 |
| Companies | AUR-SYN-001, 002, 003, 004 |
| Model | qwen2.5:7b (Ollama) |
| Silent wrong / wrong answers | 0 / 0 (Run 1 + Run 2) |
| Probes failed | 1 in Run 1 → fixed in v0.10.3; recheck 2026-10-07 12:11 UTC: **0 / 28** |
| Recheck (v0.10.3, no model, Run 1's stored facts) | 40 questions: 34 correct, 6 abstained, **0 wrong**; interpretation 100%; answered with evidence 100%; probes 28/28; file `query-recheck-20261007T121100.json` |

## F. Security
- Tenant isolation: every endpoint called by another organization with this
  organization's ids returns 400/404 or an empty result, never data; writes
  (correct, review, analyze, relationship) are refused and leave data unchanged.
- Isolation is enforced in application code (organization_id in every query),
  **not** PostgreSQL row-level security. There is no authentication/RBAC yet
  (out of scope until the lead says so); the organization is taken from the
  `X-Organization-Id` header.

## G. Remaining limitations
- Question interpretation is phrase-based English; unusual wording abstains or routes to "unknown".
- Qualifier detection is conservative: an unexpected extra word makes a singleton question abstain (safe, less helpful).
- Renewal comparison uses the last two stated periods.
- Fallback retrieval over client documents is keyword-only (carrier wordings keep the vector `/chat`).
- Risk-survey occupancy restatements still produce a contradiction (safe abstention).
- Full 20-company acceptance intentionally deferred.
