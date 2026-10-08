# Phase 1A results — what each file is

Nothing here is ever deleted or overwritten; failures stay on record.
`latest.json` = the most recent FULL (20-company) run with the model reachable.
It changes only when a full run happens, so between phase-acceptance runs it
deliberately shows an older version.

| File | Code version | Type | Companies | Recall | Silent wrong | Status |
|---|---|---|---|---|---|---|
| result-20260929T181141 | before v0.6.0 | full, real Qwen | 20 | 67.5% | 0 | historical baseline |
| result-20260930T155314 | v0.6.0 | quick, real Qwen | 3 | 100% | 4 (risk factors) | historical failure, fixed in v0.6.1 |
| result-20261001T090702 (= latest.json) | v0.7.0 | full, real Qwen | 20 | 100% | 2 (AUR-SYN-014 endorsements) | historical failure, fixed in v0.7.1 |
| result-20261002T124058 * | v0.8.1 | quick, real Qwen | 3 | 100% | 2 ("Plant and machinery" coverage) | historical failure, fixed in v0.8.3 |
| result-20261002T…(AUR-SYN-002,003) * | v0.8.3 | targeted, real Qwen | 2 | — | 0 (reported) | current-code re-check |
| result-20261006T162353 * | v0.10.0 | development + query check, real Qwen | 4 | 98.1% | 1 (endorsement label); query: 1 wrong, 6 abstained | historical failure, fixed in v0.10.1 |
| result-20261006T… (v0.10.1 re-run) * | v0.10.1 | development + query check, real Qwen | 4 | see file | 0; query: 0 wrong (reported) | historical (v0.10.1) |
| result-20261007T070750 * | v0.10.2 | development + query check + probes, real Qwen | 4 | 96.2% | 0; query 0 wrong; 1 probe failed | fixed in v0.10.3 |
| result-20261007T074446 * | v0.10.2 | AUR-SYN-001 re-run, real Qwen | 1 | 100% | 0; query 0 wrong; probes 7/7 | clean re-run |
| query-recheck-20261007T121100 * | v0.10.3 | recheck, no model, on run 07:07's stored facts | 4 | — | query 0 wrong; probes 28/28 | **current — Phase 1B development acceptance** |
| result-20261008T073454 * | v0.11.0 | development, real Qwen, Phase 1A+1B+1C gates | 4 | 100% | 0; query 0 wrong, probes 28/28; policy 28/28, probes 24/24 | Phase 1C development acceptance |
| result-20261008T165020 * | v0.12.0 | development, real Qwen, Phase 1A+1B+1C+1D gates | 4 | 100% | 0; query 0 wrong, probes 28/28; policy 28/28, probes 24/24; business 53/53, probes 24/24 | Phase 1D development acceptance (gates) |
| phase1d-acceptance-20261008T191206 * | v0.12.1 | read-only audit of result-20261008T165020's stored data | 4 | — | 198/198 checks; evidence 100%, page 100%; 0 unsupported | **current — Phase 1D ACCEPTED (4-company), frozen** |
| (phase end) full 20-company run | end of Phase 1B | acceptance, real Qwen | 20 | | | **pending — not run yet** |

\* produced on the developer's machine; commit them from `backend/evaluations/phase1a/`.

Categories:
- **historical** — an older code version; kept so failures and fixes stay traceable.
- **current** — run on the code version currently in the repo.
- **phase acceptance** — the full 20-company run that closes a phase. None exists yet for the current code.
- Deterministic results (no model): `../phase1b/` (policy comparison, 13/13) and the pytest suite.
