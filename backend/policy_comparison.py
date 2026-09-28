"""
Phase 1B — Policy Intelligence, first slice: coverage comparison.

Compares the CURRENT Knowledge Core facts of two submissions that have
already been through Property Policy Intelligence (property_policy_
intelligence.analyze_submission) — typically last year's policy vs this
year's renewal, or a new submission vs the client's expiring policy — and
reports, per fact type, whether it improved, worsened, stayed the same, or
appeared/disappeared entirely.

Deliberately reuses rather than re-derives:
  - knowledge/fact_types.py's FACT_DEFINITIONS for cardinality/value_type
    (a singleton fact type like policy.deductible compares its one current
    value directly; a multi-valued type like policy.exclusion compares the
    two SETS of current values instead — a fact type's shape decides how it
    is compared, exactly like it already decides how it is written and
    contradiction-checked).
  - doc_intelligence._numeric_value / CONTRADICTION_TOLERANCE for numeric
    parsing and the "close enough to call unchanged" threshold — the same
    1% tolerance reconcile_fact() already uses, so a figure this module
    calls "unchanged" is the same figure the write path would also treat as
    non-contradictory, not a second, independently-tuned notion of "close".
  - knowledge.service.find_facts(current_only=True) — the exact same read
    every other Knowledge Core surface (GET /knowledge/entities/{id}/facts)
    uses, so a comparison is never seeing a different "current" than what a
    human reviewer would see by opening either policy directly.

This module does not write anything — it is a pure read-side comparison.
Nothing here creates facts, entities, or touches reconcile_fact(); running
a comparison is safe to call repeatedly and has no effect on either
submission's own Knowledge Core state.

Two entities of the SAME entity_type (both "policy", or both "asset") are
compared — never asked to guess which two entities in a submission
correspond to each other. Callers pass the specific entity_id on each side
(see compare_submissions() for the submission-level convenience wrapper,
which locates each submission's Policy and Asset entities the same way
property_policy_intelligence.analyze_submission does: kc.get_or_create_
entity keyed off the submission's own label).
"""
from doc_intelligence import CONTRADICTION_TOLERANCE, _numeric_value

import db
import knowledge.service as kc
from knowledge.fact_types import FACT_DEFINITIONS, ALL_FACT_TYPES


def _text_key(value: str) -> str:
    """Case/whitespace-insensitive comparison key for a text fact value —
    the same normalisation knowledge.service._values_agree already applies,
    reused here so 'War, Terrorism' and 'war, terrorism ' are recognised as
    the same exclusion rather than reported as one dropped and one added."""
    return " ".join(str(value).split()).lower()


def _numeric_of(fact: dict) -> float | None:
    if fact.get("normalized_value") is not None:
        return float(fact["normalized_value"])
    return _numeric_value(fact["value"])


def _fact_summary(fact: dict) -> dict:
    """The small, comparison-relevant slice of a fact row — not the full
    _fact_response() shape from api/knowledge.py, since a comparison result
    is about VALUES changing, not full evidence/history detail (a caller
    that wants that already has fact_id here and can fetch it)."""
    return {
        "fact_id": str(fact["id"]),
        "value": fact["value"],
        "normalized_value": (
            float(fact["normalized_value"]) if fact.get("normalized_value") is not None else None
        ),
        "status": fact["status"],
    }


def _compare_singleton(fact_type: str, left_facts: list[dict], right_facts: list[dict]) -> dict:
    """
    One current value expected on each side (facts.py's contradiction-
    checked types: sum_insured, location, occupancy, construction,
    coverage_limit, deductible).

    left_facts/right_facts are already filtered to this fact_type and
    current_only=True — ordinarily exactly one row each, but a side with an
    unresolved contradiction_flagged conflict can have more than one
    "current" row (see knowledge/versions.py's get_current_value_or_
    conflict — the same situation that function exists to surface). Rather
    than silently pick one, an unresolved side is reported as such: the
    comparison itself is not the place to resolve a conflict that GET
    /knowledge/entities/{id}/facts/{fact_type}/current already surfaces.
    """
    left_unresolved = len(left_facts) > 1
    right_unresolved = len(right_facts) > 1
    left = left_facts[0] if left_facts else None
    right = right_facts[0] if right_facts else None

    result = {
        "fact_type": fact_type,
        "cardinality": "singleton",
        "left": _fact_summary(left) if left else None,
        "right": _fact_summary(right) if right else None,
        "left_unresolved_conflict": left_unresolved,
        "right_unresolved_conflict": right_unresolved,
    }

    if left is None and right is None:
        result["change"] = "absent_both"
        return result
    if left is None:
        result["change"] = "added"
        return result
    if right is None:
        result["change"] = "removed"
        return result

    left_number = _numeric_of(left)
    right_number = _numeric_of(right)

    if left_number is not None and right_number is not None:
        if left_number == 0:
            # Same degenerate-value guard as knowledge.service._values_agree.
            result["change"] = "unchanged" if right_number == 0 else "changed"
        else:
            relative_difference = abs(right_number - left_number) / abs(left_number)
            if relative_difference <= CONTRADICTION_TOLERANCE:
                result["change"] = "unchanged"
            else:
                result["change"] = "increased" if right_number > left_number else "decreased"
            result["relative_difference"] = round(relative_difference, 4)
    else:
        result["change"] = "unchanged" if _text_key(left["value"]) == _text_key(right["value"]) else "changed"

    return result


def _compare_multi(fact_type: str, left_facts: list[dict], right_facts: list[dict]) -> dict:
    """
    A set of independently-true current values on each side (policy.
    coverage, policy.exclusion, policy.endorsement, property.risk_factors).
    Compared as sets, keyed by normalised text — matches facts.py's own
    "each occurrence is a separate true fact" treatment of these types:
    there is no single "the" exclusion to diff, only which exclusions are
    present on each side.
    """
    left_by_key = {_text_key(f["value"]): f for f in left_facts}
    right_by_key = {_text_key(f["value"]): f for f in right_facts}

    kept_keys = set(left_by_key) & set(right_by_key)
    added_keys = set(right_by_key) - set(left_by_key)
    removed_keys = set(left_by_key) - set(right_by_key)

    return {
        "fact_type": fact_type,
        "cardinality": "multi",
        "kept": [_fact_summary(right_by_key[k]) for k in sorted(kept_keys)],
        "added": [_fact_summary(right_by_key[k]) for k in sorted(added_keys)],
        "removed": [_fact_summary(left_by_key[k]) for k in sorted(removed_keys)],
        "change": "changed" if (added_keys or removed_keys) else (
            "unchanged" if kept_keys else "absent_both"
        ),
    }


def compare_entities(conn, *, organization_id: str, left_entity_id: str,
                      right_entity_id: str, fact_types: list[str] | None = None) -> dict:
    """
    Compare the CURRENT facts of two entities of the same kind (e.g. two
    Policy entities, or two Asset entities), fact type by fact type.

    `fact_types` restricts the comparison to a subset of ALL_FACT_TYPES
    (e.g. only policy.* types when comparing two Policy entities, though
    nothing here enforces that — a caller comparing an Asset against a
    Policy by mistake simply gets "absent_both" for every fact type that
    doesn't apply to one side, which is a harmless, honest answer, not a
    silent misread). Defaults to every registered fact type.

    Left is the BASELINE (e.g. last year's policy, or the client's
    expiring policy); right is the NEW one being compared against it —
    "increased"/"decreased"/"added"/"removed" are all stated relative to
    left, matching how a broker reads "what changed this renewal".

    Raises ValueError if either entity does not exist for this
    organization — same 404-worthy convention as every other org-scoped
    lookup in this codebase (caller turns this into a 404).
    """
    left_entity = kc.get_entity(conn, left_entity_id, organization_id)
    if not left_entity:
        raise ValueError("Left entity not found.")
    right_entity = kc.get_entity(conn, right_entity_id, organization_id)
    if not right_entity:
        raise ValueError("Right entity not found.")

    types_to_compare = list(fact_types) if fact_types else sorted(ALL_FACT_TYPES)

    comparisons = []
    for fact_type in types_to_compare:
        left_facts = kc.find_facts(conn, left_entity_id, organization_id,
                                    fact_type=fact_type, current_only=True)
        right_facts = kc.find_facts(conn, right_entity_id, organization_id,
                                     fact_type=fact_type, current_only=True)

        definition = FACT_DEFINITIONS.get(fact_type)
        is_singleton = definition is None or definition.is_singleton
        if is_singleton:
            comparisons.append(_compare_singleton(fact_type, left_facts, right_facts))
        else:
            comparisons.append(_compare_multi(fact_type, left_facts, right_facts))

    changed = [c for c in comparisons if c["change"] not in ("unchanged", "absent_both")]

    return {
        "left_entity": {"entity_id": str(left_entity["id"]), "name": left_entity["name"]},
        "right_entity": {"entity_id": str(right_entity["id"]), "name": right_entity["name"]},
        "fact_comparisons": comparisons,
        "summary": {
            "fact_types_compared": len(comparisons),
            "fact_types_changed": len(changed),
            "changed_fact_types": [c["fact_type"] for c in changed],
        },
    }


def compare_submissions(conn, *, organization_id: str, left_submission_id: str,
                         right_submission_id: str) -> dict:
    """
    Convenience wrapper: compare two submissions' Policy entities (policy.*
    facts) and Asset entities (property.* facts) in one call, resolving
    each submission's Policy/Asset entity the same way property_policy_
    intelligence.analyze_submission does — get_or_create_entity keyed off
    the submission's own label. This does NOT re-run extraction or create
    any entity: if a submission was never analyzed via analyze-property-
    policy, get_or_create_entity here would create a fresh, factless
    entity for it, which would compare as "absent" on every fact type
    rather than fail outright — so this raises a clearer error instead
    when a submission has no policy/asset entity yet, rather than silently
    comparing against an empty one.

    Raises ValueError if either submission does not exist for this
    organization, or has not yet been analyzed (no Policy/Asset entity on
    record for its label).
    """
    left_submission = db.get_submission(conn, left_submission_id, organization_id)
    if not left_submission:
        raise ValueError("Left submission not found.")
    right_submission = db.get_submission(conn, right_submission_id, organization_id)
    if not right_submission:
        raise ValueError("Right submission not found.")

    left_policy = db.find_entity_by_name(
        conn, organization_id, "policy", f"{left_submission['label']} — policy",
    )
    right_policy = db.find_entity_by_name(
        conn, organization_id, "policy", f"{right_submission['label']} — policy",
    )
    if not left_policy or not right_policy:
        raise ValueError(
            "One or both submissions have not been analyzed yet — run "
            "POST /knowledge/submissions/{submission_id}/analyze-property-policy "
            "on each submission first."
        )

    policy_comparison = compare_entities(
        conn, organization_id=organization_id,
        left_entity_id=str(left_policy["id"]), right_entity_id=str(right_policy["id"]),
        fact_types=[ft for ft in ALL_FACT_TYPES if FACT_DEFINITIONS[ft].entity == "policy"],
    )

    left_asset = db.find_entity_by_name(
        conn, organization_id, "asset", f"{left_submission['label']} — insured property",
    )
    right_asset = db.find_entity_by_name(
        conn, organization_id, "asset", f"{right_submission['label']} — insured property",
    )
    asset_comparison = None
    if left_asset and right_asset:
        asset_comparison = compare_entities(
            conn, organization_id=organization_id,
            left_entity_id=str(left_asset["id"]), right_entity_id=str(right_asset["id"]),
            fact_types=[ft for ft in ALL_FACT_TYPES if FACT_DEFINITIONS[ft].entity == "asset"],
        )

    return {
        "left_submission_id": left_submission_id,
        "right_submission_id": right_submission_id,
        "policy": policy_comparison,
        "asset": asset_comparison,
    }
