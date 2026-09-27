"""
Regression tests #1 and #2 from the audit's requested suite:

1. Singleton contradiction detection still works.
2. Multi-valued fact types are never flagged as contradicting each other.

Both exercise knowledge.service.reconcile_fact() directly against a real
Postgres connection — no mocking of the code under test.
"""
import knowledge.service as kc


def test_singleton_contradiction_is_flagged(conn, test_org, make_entity):
    """Two disagreeing property.sum_insured facts on the same entity must
    both end up 'contradiction_flagged', and a kc_conflicts row must open."""
    entity_id = make_entity(test_org, entity_type="asset")

    confidence = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}

    first = kc.reconcile_fact(
        conn, organization_id=test_org, entity_id=entity_id,
        fact_type="property.sum_insured", value="350000000",
        normalized_value=350000000.0, confidence=confidence, produced_by="test:first",
    )
    assert first["status"] == "needs_review"
    assert first["_contradiction"] is None

    second = kc.reconcile_fact(
        conn, organization_id=test_org, entity_id=entity_id,
        fact_type="property.sum_insured", value="370000000",
        normalized_value=370000000.0, confidence=confidence, produced_by="test:second",
    )
    assert second["status"] == "contradiction_flagged"
    assert second["_contradiction"] is not None

    # Re-fetch the FIRST fact directly — reconcile_fact() must have flagged
    # it too, not just the second (newer) one.
    refreshed_first = kc.get_fact(conn, str(first["id"]), test_org)
    assert refreshed_first["status"] == "contradiction_flagged"

    import db
    conflict = db.get_unresolved_conflict(conn, entity_id, test_org, "property.sum_insured")
    assert conflict is not None
    assert conflict["status"] == "unresolved"

    fact_ids_in_conflict = set(db.get_conflict_fact_ids(conn, str(conflict["id"])))
    assert fact_ids_in_conflict == {str(first["id"]), str(second["id"])}


def test_singleton_agreeing_values_do_not_conflict(conn, test_org, make_entity):
    """A negative control: two facts that agree within tolerance must NOT
    open a conflict, and the evidence should merge onto the existing fact."""
    entity_id = make_entity(test_org, entity_type="asset")
    confidence = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}

    first = kc.reconcile_fact(
        conn, organization_id=test_org, entity_id=entity_id,
        fact_type="property.sum_insured", value="500000000",
        normalized_value=500000000.0, confidence=confidence, produced_by="test:first",
    )
    second = kc.reconcile_fact(
        conn, organization_id=test_org, entity_id=entity_id,
        fact_type="property.sum_insured", value="500000000",
        normalized_value=500000000.0, confidence=confidence, produced_by="test:second",
    )

    assert second["id"] == first["id"], "agreeing values must merge onto the SAME fact row"
    assert second["_contradiction"] is None

    import db
    conflict = db.get_unresolved_conflict(conn, entity_id, test_org, "property.sum_insured")
    assert conflict is None


def test_multi_valued_fact_types_never_conflict(conn, test_org, make_entity):
    """Three different policy.exclusion values on the same entity are three
    independently-true facts, never flagged as contradicting each other."""
    entity_id = make_entity(test_org, entity_type="policy")
    confidence = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}

    values = ["Flood excluded", "War excluded", "Nuclear contamination excluded"]
    written = []
    for value in values:
        fact = kc.reconcile_fact(
            conn, organization_id=test_org, entity_id=entity_id,
            fact_type="policy.exclusion", value=value,
            confidence=confidence, produced_by="test:exclusion",
        )
        written.append(fact)
        assert fact["_contradiction"] is None
        assert fact["status"] == "needs_review"

    # All three must persist as separate CURRENT rows (valid_to IS NULL) —
    # a multi-valued fact type never merges or supersedes another distinct
    # value the way a singleton type would.
    current_facts = kc.find_facts(conn, entity_id, test_org, fact_type="policy.exclusion",
                                   current_only=True)
    assert len(current_facts) == 3
    assert {f["value"] for f in current_facts} == set(values)

    import db
    conflict = db.get_unresolved_conflict(conn, entity_id, test_org, "policy.exclusion")
    assert conflict is None, "multi-valued fact types must never open a conflict"
