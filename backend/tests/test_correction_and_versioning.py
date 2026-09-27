"""
Regression tests #3 and #4 from the audit's requested suite:

3. Conflict resolution closes out the losing fact (Issue 1's fix) — after
   resolution, exactly one fact has valid_to IS NULL for that
   entity+fact_type, and the losing fact is no longer 'contradiction_flagged'.
4. Version history is correct after a correction (original fact's value
   preserved with valid_to set; new fact has the corrected value; history
   shows both in order).

Both exercise knowledge.service directly against a real Postgres connection.
"""
import db
import knowledge.service as kc


def _open_conflict(conn, org, entity_id, value_a, value_b):
    confidence = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}
    fact_a = kc.reconcile_fact(
        conn, organization_id=org, entity_id=entity_id,
        fact_type="property.sum_insured", value=value_a,
        normalized_value=float(value_a), confidence=confidence, produced_by="test:a",
    )
    fact_b = kc.reconcile_fact(
        conn, organization_id=org, entity_id=entity_id,
        fact_type="property.sum_insured", value=value_b,
        normalized_value=float(value_b), confidence=confidence, produced_by="test:b",
    )
    assert fact_b["status"] == "contradiction_flagged"
    return fact_a, fact_b


def test_correction_closes_out_the_losing_fact(conn, test_org, make_entity):
    """Issue 1's fix: correcting one side of a conflict must close out the
    OTHER (losing) side too — valid_to set, status no longer
    'contradiction_flagged' — leaving EXACTLY ONE fact row with
    valid_to IS NULL for the entity+fact_type."""
    entity_id = make_entity(test_org, entity_type="asset")
    fact_a, fact_b = _open_conflict(conn, test_org, entity_id, "350000000", "370000000")

    conflict_before = db.get_unresolved_conflict(conn, entity_id, test_org, "property.sum_insured")
    assert conflict_before is not None

    corrected = kc.correct_fact(
        conn, str(fact_a["id"]), test_org,
        new_value="360000000", normalized_value=360000000.0, reviewed_by="pytest",
    )
    assert corrected["status"] == "corrected"
    assert corrected["value"] == "360000000"
    assert corrected["valid_to"] is None

    # The conflict must be resolved, pointing at the new corrected fact.
    conflict_after = db.get_conflict(conn, str(conflict_before["id"]), test_org)
    assert conflict_after["status"] == "resolved"
    assert str(conflict_after["resolution_fact_id"]) == str(corrected["id"])

    # fact_a (the one just corrected) must be closed by supersede_fact().
    fact_a_refreshed = db.get_fact(conn, str(fact_a["id"]), test_org)
    assert fact_a_refreshed["valid_to"] is not None

    # fact_b (the LOSING side — this is Issue 1) must ALSO be closed, and
    # must no longer read 'contradiction_flagged'.
    fact_b_refreshed = db.get_fact(conn, str(fact_b["id"]), test_org)
    assert fact_b_refreshed["valid_to"] is not None, (
        "the losing fact must have valid_to set once its conflict is resolved"
    )
    assert fact_b_refreshed["status"] != "contradiction_flagged", (
        "the losing fact must not still read contradiction_flagged once resolved"
    )
    assert fact_b_refreshed["status"] == "superseded"

    # The core invariant: exactly one row with valid_to IS NULL for this
    # entity+fact_type once the conflict is resolved — verified by an actual
    # count, not by trusting that a read-path query happens to pick the
    # right one.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM kc_facts "
            "WHERE entity_id = %s AND organization_id = %s AND fact_type = %s "
            "AND valid_to IS NULL",
            (entity_id, test_org, "property.sum_insured"),
        )
        open_count = cur.fetchone()["n"]
    assert open_count == 1, f"expected exactly one open fact row, found {open_count}"


def test_correction_with_three_way_conflict_closes_every_losing_fact(conn, test_org, make_entity):
    """A third disagreeing value can join an already-open conflict
    (add_fact_to_conflict) before anyone corrects it — the fix must close
    out ALL losing sides, not just one."""
    entity_id = make_entity(test_org, entity_type="asset")
    confidence = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}

    fact_a, fact_b = _open_conflict(conn, test_org, entity_id, "100000000", "120000000")
    fact_c = kc.reconcile_fact(
        conn, organization_id=test_org, entity_id=entity_id,
        fact_type="property.sum_insured", value="140000000",
        normalized_value=140000000.0, confidence=confidence, produced_by="test:c",
    )
    assert fact_c["status"] == "contradiction_flagged"

    conflict = db.get_unresolved_conflict(conn, entity_id, test_org, "property.sum_insured")
    fact_ids = set(db.get_conflict_fact_ids(conn, str(conflict["id"])))
    assert fact_ids == {str(fact_a["id"]), str(fact_b["id"]), str(fact_c["id"])}

    corrected = kc.correct_fact(
        conn, str(fact_a["id"]), test_org,
        new_value="150000000", normalized_value=150000000.0, reviewed_by="pytest",
    )

    for losing in (fact_b, fact_c):
        refreshed = db.get_fact(conn, str(losing["id"]), test_org)
        assert refreshed["valid_to"] is not None
        assert refreshed["status"] == "superseded"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM kc_facts "
            "WHERE entity_id = %s AND organization_id = %s AND fact_type = %s "
            "AND valid_to IS NULL",
            (entity_id, test_org, "property.sum_insured"),
        )
        open_count = cur.fetchone()["n"]
    assert open_count == 1
    assert corrected["valid_to"] is None


def test_version_history_after_correction(conn, test_org, make_entity):
    """Issue 4: after a (non-conflict) correction, the ORIGINAL fact's value
    is preserved with valid_to now set, the NEW fact carries the corrected
    value, and get_historical_values()/the history read shows both, oldest
    first."""
    entity_id = make_entity(test_org, entity_type="asset")
    confidence = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}

    original = kc.reconcile_fact(
        conn, organization_id=test_org, entity_id=entity_id,
        fact_type="property.occupancy", value="Warehouse - general storage",
        confidence=confidence, produced_by="test:original",
    )
    assert original["valid_to"] is None

    corrected = kc.correct_fact(
        conn, str(original["id"]), test_org,
        new_value="Warehouse - cold storage", reviewed_by="pytest",
    )
    assert corrected["id"] != original["id"]
    assert corrected["value"] == "Warehouse - cold storage"
    assert corrected["valid_to"] is None

    original_refreshed = kc.get_fact(conn, str(original["id"]), test_org)
    assert original_refreshed["value"] == "Warehouse - general storage", (
        "the original fact's value must be preserved, never overwritten in place"
    )
    assert original_refreshed["valid_to"] is not None

    history = kc.get_historical_values(conn, entity_id, test_org, "property.occupancy")
    assert [h["value"] for h in history] == [
        "Warehouse - general storage", "Warehouse - cold storage",
    ], "history must show both versions, oldest first"
    assert history[0]["valid_to"] is not None
    assert history[-1]["valid_to"] is None
