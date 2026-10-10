# Phase 1E expected outcomes (development benchmark)

Hand-written from reading AUR-SYN-001–004's own documents and the Phase 1D
supplement — not produced by the system. Used by the "coverage check
(Phase 1E)" gate in `eval_property_policy.py --query-check` and by
`recheck_query.py`. No new documents: Phase 1E adds no synthetic data.

- `limit` / `exposure`: Property Damage sum insured on the policy, and the sum
  of the current declared values (schedule, with AUR-SYN-001's follow-up
  survey raising Plant and Machinery to 210,000,000).
- AUR-SYN-004: the schedule prints a total (370,000,000) that its items do not
  add up to (350,000,000). If the model reads that total as a second sum
  insured, the limit comparison must be NEEDS_REVIEW (conflict), never a
  confident state.
