"""
Regression tests #6 and #7 from the audit's requested suite:

6. Cross-tenant evidence creation is rejected (org A's fact/document, org B's
   organization_id argument).
7. Cross-tenant relationship creation is rejected (entities in two different
   orgs).

Both call repositories/knowledge.py directly (bypassing the API layer), the
same way the original Phase 1A hardening pass verified this — proving the
check lives in the repository function itself, not only in api/knowledge.py.
"""
import pytest

import db
import knowledge.service as kc
from repositories.knowledge import NotYoursError


def test_cross_tenant_evidence_creation_is_rejected(conn, two_test_orgs, make_entity, make_document):
    org_a, org_b = two_test_orgs

    entity_a = make_entity(org_a, entity_type="asset")
    fact_a = kc.create_fact(
        conn, organization_id=org_a, entity_id=entity_a, fact_type="property.sum_insured",
        value="100000000", confidence={"value": 0.9, "type": "model_self_reported",
                                        "method": "test", "note": None},
        produced_by="test",
    )
    document_a = make_document(org_a, extracted_text="Org A's document text.")

    # org B trying to attach evidence using org A's fact_id.
    with pytest.raises(NotYoursError):
        db.create_evidence(
            conn, organization_id=org_b, fact_id=str(fact_a["id"]),
            document_id=document_a, page=1, section=None, snippet="irrelevant", source="text_layer",
        )

    # org A's fact but org B's document.
    document_b = make_document(org_b, extracted_text="Org B's document text.")
    with pytest.raises(NotYoursError):
        db.create_evidence(
            conn, organization_id=org_a, fact_id=str(fact_a["id"]),
            document_id=document_b, page=1, section=None, snippet="irrelevant", source="text_layer",
        )

    # Sanity: the SAME-organization call must still work normally.
    evidence = db.create_evidence(
        conn, organization_id=org_a, fact_id=str(fact_a["id"]),
        document_id=document_a, page=1, section=None, snippet="irrelevant", source="text_layer",
    )
    assert str(evidence["fact_id"]) == str(fact_a["id"])


def test_cross_tenant_relationship_creation_is_rejected(conn, two_test_orgs, make_entity):
    org_a, org_b = two_test_orgs

    business_a = make_entity(org_a, entity_type="business", name="Org A business")
    asset_b = make_entity(org_b, entity_type="asset", name="Org B asset")

    with pytest.raises(NotYoursError):
        db.create_relationship(
            conn, organization_id=org_a, from_entity_id=business_a,
            to_entity_id=asset_b, relationship_type="owns",
        )

    # Also rejected the other way: relationship claimed under org B, but
    # from_entity_id belongs to org A.
    with pytest.raises(NotYoursError):
        db.create_relationship(
            conn, organization_id=org_b, from_entity_id=business_a,
            to_entity_id=asset_b, relationship_type="owns",
        )

    # Sanity: two entities genuinely in the same organization still work.
    asset_a = make_entity(org_a, entity_type="asset", name="Org A asset")
    relationship = db.create_relationship(
        conn, organization_id=org_a, from_entity_id=business_a,
        to_entity_id=asset_a, relationship_type="owns",
    )
    assert str(relationship["from_entity_id"]) == business_a
    assert str(relationship["to_entity_id"]) == asset_a


def test_cross_tenant_entity_read_returns_none(conn, two_test_orgs, make_entity):
    """A read-side sanity check alongside the write-side tests above: an
    entity created under org A must not be visible under org B's id."""
    org_a, org_b = two_test_orgs
    entity_a = make_entity(org_a, entity_type="asset")

    assert kc.get_entity(conn, entity_a, org_b) is None
    assert kc.get_entity(conn, entity_a, org_a) is not None
