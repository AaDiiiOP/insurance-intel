"""
Phase 1B acceptance benchmark — policy_comparison.py against deterministic
ground truth.

Unlike eval_property_policy.py (Phase 1A), this benchmark never calls a
model: policy_comparison.py's job starts AFTER extraction — it diffs facts
that are already Knowledge Core rows. So this harness writes those rows
directly (via knowledge.service.reconcile_fact(), the same write path
property_policy_intelligence.py itself uses — never a shortcut that bypasses
it) rather than going through document parsing or LLM extraction, and then
checks that compare_submissions()/compare_entities() report exactly the
expected change for each fact type. This is deterministic and 100%
reproducible — there is no model variance to account for, so every test
case either passes or fails, with no statistical threshold involved.

Each test case is a (baseline facts, renewal facts, expected comparison)
triple covering the change types a broker actually needs correctly
reported (see TEST_CASES below): increased/decreased singleton values,
added/removed singleton values, unchanged values, added/removed/kept
multi-valued values, an unresolved contradiction on one side, and
cross-organization isolation (comparing across two different
organizations' entities must be rejected, never silently returned).

Usage:
    cd backend
    python3 eval_policy_comparison.py
"""
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import db
import knowledge.service as kc
import policy_comparison
from knowledge.fact_types import FACT_DEFINITIONS


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _write_fact(conn, organization_id, entity_id, fact_type, value, normalized_value=None):
    """Write one fact through the real reconcile_fact() write path — the
    same one property_policy_intelligence.py uses. document_id is None
    here (no real document backs this synthetic fact), which is fine:
    reconcile_fact only links evidence when a document_id is actually
    given, and this benchmark tests comparison logic, not evidence
    linking (that's Phase 1A's job, already covered by
    test_evidence_anchoring.py)."""
    return kc.reconcile_fact(
        conn, organization_id=organization_id, entity_id=entity_id,
        fact_type=fact_type, value=value, normalized_value=normalized_value,
        confidence={"value": 1.0, "type": "deterministic",
                    "method": "eval_policy_comparison_test_fixture", "note": None},
        produced_by="eval_policy_comparison.py", document_id=None,
        evidence_page=None, evidence_section=None, evidence_snippet=None,
        evidence_source=None,
    )


def _make_submission_and_policy(conn, organization_id, label):
    """Creates a submission and BOTH its Policy and Asset entities the
    exact same way property_policy_intelligence.analyze_submission does
    (get_or_create_entity keyed off the submission's own label, with the
    same "— policy" / "— insured property" name suffixes that compare_
    submissions() looks for). Both are created even for test cases that
    only write policy.* or only property.* facts, because compare_
    submissions() only finds the asset side at all when an entity with
    that exact name already exists (see policy_comparison.compare_
    submissions' left_asset/right_asset lookup) — a benchmark that only
    created the Policy entity would silently never exercise the asset half
    of compare_submissions, which is exactly the kind of gap this
    benchmark exists to catch."""
    submission = db.create_submission(conn, organization_id=organization_id, label=label)
    policy = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="policy",
        name=f"{label} — policy",
    )
    asset = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="asset",
        name=f"{label} — insured property",
    )
    return str(submission["id"]), str(policy["id"]), str(asset["id"])


def _numeric(value_str: str) -> float:
    """Parse a plain 'n<digits>' test-fixture value into a float — same
    'n' prefix convention the synthetic dataset's PDFs use for currency
    (see scripts/gen_phase1_supplement.py), kept here only so test case
    definitions below can write 'n500000000' instead of a bare float and
    stay visually consistent with the rest of the synthetic dataset."""
    return float(value_str.lstrip("n").replace(",", ""))


# ---------------------------------------------------------------------------
# Test cases
#
# Each case: baseline facts written to submission A's Policy entity, renewal
# facts written to submission B's, then compare_submissions(A, B) is run and
# checked against `expect` — a dict of {fact_type: expected "change" value,
# or a custom assertion function for cases needing more than a bare string
# compare}.
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id": "increased_sum_insured",
        "description": "property.sum_insured goes up between baseline and renewal.",
        "baseline": {"property.sum_insured": ("n500,000,000", _numeric("n500000000"))},
        "renewal": {"property.sum_insured": ("n600,000,000", _numeric("n600000000"))},
        "expect": {"property.sum_insured": "increased"},
        "entity": "asset",
    },
    {
        "id": "decreased_sum_insured",
        "description": "property.sum_insured goes down between baseline and renewal.",
        "baseline": {"property.sum_insured": ("n600,000,000", _numeric("n600000000"))},
        "renewal": {"property.sum_insured": ("n500,000,000", _numeric("n500000000"))},
        "expect": {"property.sum_insured": "decreased"},
        "entity": "asset",
    },
    {
        "id": "increased_deductible",
        "description": "policy.deductible goes up.",
        "baseline": {"policy.deductible": ("n1,000,000", _numeric("n1000000"))},
        "renewal": {"policy.deductible": ("n1,500,000", _numeric("n1500000"))},
        "expect": {"policy.deductible": "increased"},
        "entity": "policy",
    },
    {
        "id": "decreased_deductible",
        "description": "policy.deductible goes down.",
        "baseline": {"policy.deductible": ("n1,500,000", _numeric("n1500000"))},
        "renewal": {"policy.deductible": ("n1,000,000", _numeric("n1000000"))},
        "expect": {"policy.deductible": "decreased"},
        "entity": "policy",
    },
    {
        "id": "unchanged_coverage_limit",
        "description": "policy.coverage_limit stays the same (within tolerance) — must NOT be reported as changed.",
        "baseline": {"policy.coverage_limit": ("n300,000,000", _numeric("n300000000"))},
        "renewal": {"policy.coverage_limit": ("n300,000,000", _numeric("n300000000"))},
        "expect": {"policy.coverage_limit": "unchanged"},
        "entity": "policy",
    },
    {
        "id": "unchanged_within_tolerance",
        "description": "A trivial rounding difference (<1%) must still read unchanged, matching reconcile_fact()'s own tolerance.",
        "baseline": {"policy.coverage_limit": ("n300,000,000", _numeric("n300000000"))},
        "renewal": {"policy.coverage_limit": ("n300,500,000", _numeric("n300500000"))},
        "expect": {"policy.coverage_limit": "unchanged"},
        "entity": "policy",
    },
    {
        "id": "added_sum_insured",
        "description": "No baseline value, renewal states one — must read 'added', not 'increased' (nothing to compare against).",
        "baseline": {},
        "renewal": {"property.sum_insured": ("n400,000,000", _numeric("n400000000"))},
        "expect": {"property.sum_insured": "added"},
        "entity": "asset",
    },
    {
        "id": "removed_sum_insured",
        "description": "Baseline states a value, renewal states none — must read 'removed'.",
        "baseline": {"property.sum_insured": ("n400,000,000", _numeric("n400000000"))},
        "renewal": {},
        "expect": {"property.sum_insured": "removed"},
        "entity": "asset",
    },
    {
        "id": "exclusion_added_removed_kept",
        "description": "Multi-valued fact type: one exclusion kept, one removed, one newly added.",
        "baseline": {"policy.exclusion": [("War, terrorism and nuclear risks", None),
                                           ("Flood in basement storage areas", None)]},
        "renewal": {"policy.exclusion": [("War, terrorism and nuclear risks", None),
                                          ("Consequential loss from spontaneous combustion", None)]},
        "expect_multi": {
            "policy.exclusion": {
                "kept": {"War, terrorism and nuclear risks"},
                "added": {"Consequential loss from spontaneous combustion"},
                "removed": {"Flood in basement storage areas"},
            },
        },
        "entity": "policy",
    },
    {
        "id": "endorsement_added_removed",
        "description": "Multi-valued: an endorsement present in both (A, C) is kept, B is removed, D is added.",
        "baseline": {"policy.endorsement": [("Endorsement A", None), ("Endorsement B", None), ("Endorsement C", None)]},
        "renewal": {"policy.endorsement": [("Endorsement A", None), ("Endorsement C", None), ("Endorsement D", None)]},
        "expect_multi": {
            "policy.endorsement": {
                "kept": {"Endorsement A", "Endorsement C"},
                "added": {"Endorsement D"},
                "removed": {"Endorsement B"},
            },
        },
        "entity": "policy",
    },
    {
        "id": "fully_unchanged_policy",
        "description": "Every fact type identical between baseline and renewal — nothing should be reported as changed.",
        "baseline": {
            "policy.deductible": ("n1,000,000", _numeric("n1000000")),
            "policy.coverage_limit": ("n300,000,000", _numeric("n300000000")),
            "policy.exclusion": [("War, terrorism and nuclear risks", None)],
        },
        "renewal": {
            "policy.deductible": ("n1,000,000", _numeric("n1000000")),
            "policy.coverage_limit": ("n300,000,000", _numeric("n300000000")),
            "policy.exclusion": [("War, terrorism and nuclear risks", None)],
        },
        "expect": {"policy.deductible": "unchanged", "policy.coverage_limit": "unchanged"},
        "expect_multi": {"policy.exclusion": {"kept": {"War, terrorism and nuclear risks"}, "added": set(), "removed": set()}},
        "entity": "policy",
    },
    {
        "id": "missing_fact_both_sides",
        "description": "A fact type stated on neither side must read absent_both, not added/removed/changed.",
        "baseline": {},
        "renewal": {},
        "expect": {"property.risk_factors": "absent_both"},
        "entity": "asset",
        "multi_fact_type": "property.risk_factors",
    },
]


# ---------------------------------------------------------------------------
# Cross-organization isolation
#
# Not a TEST_CASES entry — it exercises a different code path
# (compare_entities() called with an organization_id that does NOT own one
# of the two entities) rather than a baseline/renewal fact diff, so it gets
# its own dedicated check function instead of being forced into the
# baseline/renewal shape above. Reviewer's explicit ask: "verify that
# comparisons are organization-scoped and do not leak facts between
# organizations."
# ---------------------------------------------------------------------------

def _check_cross_org_isolation(conn) -> dict:
    """
    Org A has a Policy entity with real facts. Org B tries to compare its
    own (factless) entity against Org A's entity, passing Org B's
    organization_id. This must be rejected outright (ValueError from
    compare_entities — org_id doesn't own that entity), never silently
    return Org A's facts to an Org B caller, and never silently return
    "absent" data as if the entity just had no facts (that would look like
    a harmless comparison result while actually being a tenant-isolation
    leak of the entity's mere existence).
    """
    org_a_id = str(db.create_organization(conn, "Eval Org A — cross-org isolation")["id"])
    org_b_id = str(db.create_organization(conn, "Eval Org B — cross-org isolation")["id"])

    _, policy_a_id, _ = _make_submission_and_policy(conn, org_a_id, "Cross-org baseline (Org A)")
    _write_fact(conn, org_a_id, policy_a_id, "policy.deductible", "n1,000,000", _numeric("n1000000"))

    _, policy_b_id, _ = _make_submission_and_policy(conn, org_b_id, "Cross-org renewal (Org B)")
    _write_fact(conn, org_b_id, policy_b_id, "policy.deductible", "n1,000,000", _numeric("n1000000"))

    checks = []

    # 1. Org B asking to compare Org A's entity against its own, scoped to
    #    Org B, must be rejected — Org A's entity does not exist "for" Org B.
    try:
        policy_comparison.compare_entities(
            conn, organization_id=org_b_id,
            left_entity_id=policy_a_id, right_entity_id=policy_b_id,
        )
        checks.append({"check": "org_b_cannot_read_org_a_entity", "passed": False,
                        "detail": "compare_entities did not raise — Org B was able to read Org A's entity."})
    except ValueError:
        checks.append({"check": "org_b_cannot_read_org_a_entity", "passed": True, "detail": None})

    # 2. Same, with sides reversed, to make sure the leak isn't one-sided.
    try:
        policy_comparison.compare_entities(
            conn, organization_id=org_b_id,
            left_entity_id=policy_b_id, right_entity_id=policy_a_id,
        )
        checks.append({"check": "org_b_cannot_read_org_a_entity_reversed", "passed": False,
                        "detail": "compare_entities did not raise with sides reversed."})
    except ValueError:
        checks.append({"check": "org_b_cannot_read_org_a_entity_reversed", "passed": True, "detail": None})

    # 3. Sanity: Org A comparing its own entity against itself, scoped to
    #    Org A, must succeed and report unchanged — confirms the two
    #    failures above are actually about scoping, not a broken fixture.
    try:
        result = policy_comparison.compare_entities(
            conn, organization_id=org_a_id,
            left_entity_id=policy_a_id, right_entity_id=policy_a_id,
        )
        deductible = next(c for c in result["fact_comparisons"] if c["fact_type"] == "policy.deductible")
        ok = deductible["change"] == "unchanged"
        checks.append({"check": "org_a_can_compare_its_own_entities", "passed": ok,
                        "detail": None if ok else f"expected unchanged, got {deductible['change']!r}"})
    except ValueError as exc:
        checks.append({"check": "org_a_can_compare_its_own_entities", "passed": False, "detail": str(exc)})

    passed = all(c["passed"] for c in checks)
    return {"id": "cross_org_isolation", "description": (
        "Org B must not be able to read or compare Org A's entities by "
        "passing Org A's entity_id with Org B's organization_id."
    ), "passed": passed, "checks": checks}


def _run_case(conn, organization_id: str, case: dict) -> dict:
    """Runs one TEST_CASES entry: writes baseline facts to submission A's
    entities, renewal facts to submission B's, runs compare_submissions,
    and checks the result against `expect` / `expect_multi`.

    A fact is written to whichever entity (Policy or Asset) FACT_DEFINITIONS
    actually says that fact_type belongs to — never to whatever `case["entity"]`
    says on its own — so a test case that got its "entity" label wrong would
    fail loudly (fact written to the wrong entity, comparison sees it as
    "added" on one side) rather than silently comparing against the wrong
    entity kind."""
    suffix = uuid.uuid4().hex[:8]
    submission_a_id, policy_a_id, asset_a_id = _make_submission_and_policy(
        conn, organization_id, f"{case['id']} baseline {suffix}")
    submission_b_id, policy_b_id, asset_b_id = _make_submission_and_policy(
        conn, organization_id, f"{case['id']} renewal {suffix}")

    def _entity_for(fact_type, policy_id, asset_id):
        definition = FACT_DEFINITIONS.get(fact_type)
        entity_kind = definition.entity if definition is not None else case.get("entity", "policy")
        return asset_id if entity_kind == "asset" else policy_id

    def _write_side(policy_id, asset_id, facts):
        for fact_type, spec in facts.items():
            entries = spec if isinstance(spec, list) else [spec]
            entity_id = _entity_for(fact_type, policy_id, asset_id)
            for value, normalized_value in entries:
                _write_fact(conn, organization_id, entity_id, fact_type, value, normalized_value)

    _write_side(policy_a_id, asset_a_id, case["baseline"])
    _write_side(policy_b_id, asset_b_id, case["renewal"])

    try:
        result = policy_comparison.compare_submissions(
            conn, organization_id=organization_id,
            left_submission_id=submission_a_id, right_submission_id=submission_b_id,
        )
    except Exception as exc:  # noqa: BLE001 — a raised exception here is itself a failed case, not a crash
        return {"id": case["id"], "description": case["description"], "passed": False,
                "checks": [{"check": "compare_submissions_raised", "passed": False, "detail": repr(exc)}]}

    # compare_submissions() returns separate policy/asset sub-comparisons
    # (see its own docstring) — merge both into one fact_type -> comparison
    # map so a single TEST_CASES entry can freely mix policy.* and
    # property.* fact types in its `expect`/`expect_multi` without the test
    # harness needing to know which sub-comparison holds which.
    by_type = {}
    for sub in (result.get("policy"), result.get("asset")):
        if sub:
            by_type.update({c["fact_type"]: c for c in sub["fact_comparisons"]})
    checks = []

    for fact_type, expected_change in case.get("expect", {}).items():
        actual = by_type.get(fact_type)
        if actual is None:
            checks.append({"check": f"{fact_type}.change", "passed": False,
                            "detail": f"fact_type not present in comparison result at all"})
            continue
        ok = actual["change"] == expected_change
        checks.append({"check": f"{fact_type}.change", "passed": ok,
                        "detail": None if ok else f"expected {expected_change!r}, got {actual['change']!r}"})

    for fact_type, expected_sets in case.get("expect_multi", {}).items():
        actual = by_type.get(fact_type)
        if actual is None:
            checks.append({"check": f"{fact_type}.multi", "passed": False,
                            "detail": "fact_type not present in comparison result at all"})
            continue
        for bucket in ("kept", "added", "removed"):
            actual_values = {f["value"] for f in actual.get(bucket, [])}
            expected_values = expected_sets.get(bucket, set())
            ok = actual_values == expected_values
            checks.append({"check": f"{fact_type}.{bucket}", "passed": ok,
                            "detail": None if ok else f"expected {expected_values!r}, got {actual_values!r}"})

    passed = all(c["passed"] for c in checks)
    return {"id": case["id"], "description": case["description"], "passed": passed, "checks": checks}


def main() -> int:
    run_metadata = {
        "benchmark": "phase1b_policy_comparison",
        "model_dependent": False,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "test_case_count": len(TEST_CASES) + 1,  # +1 for cross-org isolation
    }

    with db.get_connection() as conn:
        organization_id = db.get_or_create_default_organization(conn)
        case_results = [_run_case(conn, organization_id, case) for case in TEST_CASES]
        case_results.append(_check_cross_org_isolation(conn))
        conn.commit()

    passed_count = sum(1 for r in case_results if r["passed"])
    failed = [r for r in case_results if not r["passed"]]
    all_passed = len(failed) == 0

    summary = {
        "total_cases": len(case_results),
        "passed": passed_count,
        "failed": len(failed),
        "all_passed": all_passed,
        "failed_case_ids": [r["id"] for r in failed],
    }

    output = {"run": run_metadata, "summary": summary, "cases": case_results}

    evaluations_dir = Path(__file__).parent / "evaluations" / "phase1b"
    evaluations_dir.mkdir(parents=True, exist_ok=True)
    run_ts = run_metadata["timestamp_utc"].replace(":", "").replace("-", "")
    out_path = evaluations_dir / f"result-{run_ts}.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))
    latest_path = evaluations_dir / "latest.json"
    latest_path.write_text(json.dumps(output, indent=2, default=str))

    print(json.dumps({"run": run_metadata, "summary": summary}, indent=2))
    print(f"\nFull results written to {out_path}")
    print(f"Also updated {latest_path}")

    if not all_passed:
        print(f"\nFAILED cases: {summary['failed_case_ids']}")
        for r in failed:
            print(f"\n  {r['id']}: {r['description']}")
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"    - {c['check']}: {c['detail']}")
        return 1

    print(f"\nAll {summary['total_cases']} Phase 1B comparison test cases passed "
          f"(hard requirement — deterministic, no statistical threshold).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
