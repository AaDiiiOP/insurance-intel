# Evaluation results — one authoritative location

Every benchmark/eval result this project produces lives under here, laid
out by phase:

```
evaluations/
  phase0/
    eval_results.json       Phase 0 coverage-limit extraction benchmark
                             (backend/eval_harness.py)
    hard_ocr_results.json   Phase 0 hard-OCR stress test
                             (backend/run_hard_ocr_test.py)
  phase1a/
    result-<timestamp>.json  every eval_property_policy.py run, kept —
                              never overwritten in place
    latest.json              a copy of the most recent run where the model
                              was ACTUALLY reachable (model_reachable=true
                              in its own "run" block) — this is the file
                              to read for "what does Phase 1A currently
                              show," not whichever result-*.json sorts
                              last by filename (a smoke-test run where the
                              model was unreachable still gets its own
                              result-*.json for the record, but never
                              becomes latest.json)
  historical/
    ...                       results.json / finalresult.json / older
                              eval_results.json-style files that predate
                              this layout, kept for their historical value
                              (see each file's own name and the note
                              below) but superseded by whatever is
                              currently in phase1a/latest.json
  phase1b/
    result-<timestamp>.json  every eval_policy_comparison.py run, kept —
                              never overwritten in place
    latest.json              the most recent run. Unlike phase1a/latest.json
                              this has no model_reachable gate: eval_policy_
                              comparison.py never calls a model (it writes
                              facts directly via reconcile_fact() and diffs
                              them with policy_comparison.py), so every run
                              is equally "real" — there is no unreachable-
                              model case to guard against here, only
                              pass/fail on 13 deterministic test cases.
```

## Why this exists

Before this layout, result files accumulated loose in `backend/`
(`results.json`, `finalresult.json`, `eval_results.json`,
`eval_property_policy_results.json`, ...), each one silently overwritten
by the next run with no record of which model produced it, whether that
model was actually reachable, or when it ran. That made it possible for
`Results.md` to describe one run while the file actually sitting in the
repo was a different, later, broken one — exactly what happened once
(see `Results.md`'s stale-file warning at the top, which predates this
cleanup and is kept as an honest record of the mistake, not deleted).

Every `phase1a/result-*.json` now carries its own `"run"` block —
`model`, `model_reachable`, `ollama_host`, `timestamp_utc`, `git_commit`
— so a result file can never again be mistaken for a different kind of
run than it actually was. `eval_property_policy.py` also refuses to
exit 0 when the model was not reachable, so a broken/no-model run can
never be mistaken for a passing benchmark by an automated caller either.

## Historical files in `historical/`

- `2026-09-27_run2_silent_wrong_6.json` — a real, model-reachable run,
  predating the scoring-bug fixes described in `Results.md`'s Phase 1A
  section (the benchmark's own grader was wrongly flagging correct
  repeated singleton extractions as silently wrong). Kept to show the
  before/after of that fix, not as a current result.
- `2026-09-28_run3_silent_wrong_0.json` — a real, model-reachable run
  after both scoring-bug fixes: `zero_silent_wrong_facts: true`,
  `fact_recall_pct: 67.5`. The last real-model result on record at the
  time this evaluations/ layout was introduced. Superseded by
  `phase1a/latest.json` once a fresh run is made under the new layout —
  until then, this is the most recent real evidence available.

Neither historical file carries the newer `"run"` metadata block (they
predate it) — their provenance is documented here instead, by hand.
