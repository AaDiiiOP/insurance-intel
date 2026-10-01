"""
v0.8.0 Knowledge Core v2 — effective-dated facts, as-of reads, relationship
rules, whole-entity read and human review. Real Postgres, no language model.
"""
import datetime
import uuid

import pytest

import db
import knowledge.service as kc

CONF = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}


def _fact(conn, org, entity, value, doc=None, snippet=None, fact_type="policy.coverage_limit",
          start=None, end=None):
    return kc.reconcile_fact(
        conn, organization_id=org, entity_id=str(entity), fact_type=fact_type,
        value=value, normalized_value=(float(value) if value.replace(".", "").isdigit() else None),
        confidence=CONF, produced_by="test", document_id=doc, evidence_page=1,
        evidence_snippet=snippet, evidence_source="text_layer",
        effective_from=start, effective_to=end)


@pytest.fixture
def policy(conn, test_org, make_entity):
    return make_entity(test_org, entity_type="policy")


# --- temporal ---------------------------------------------------------------

def test_2025_and_2026_limits_both_stay_queryable(conn, test_org, policy):
    a = _fact(conn, test_org, policy, "300000000", start="2025-04-01", end="2026-03-31")
    b = _fact(conn, test_org, policy, "350000000", start="2026-04-01", end="2027-03-31")
    assert str(a["id"]) != str(b["id"])
    assert b["status"] == "needs_review"            # not a contradiction

    k25 = kc.get_facts_in_force(conn, str(policy), test_org, "2025-06-01")
    k26 = kc.get_facts_in_force(conn, str(policy), test_org, "2026-06-01")
    assert [f["value"] for f in k25["in_force"]] == ["300000000"]
    assert [f["value"] for f in k26["in_force"]] == ["350000000"]

    cur = kc.get_current_value_or_conflict(conn, str(policy), test_org, "policy.coverage_limit")
    assert cur["unresolved_conflict"] is False
    assert cur["current_value"]["value"] == "350000000"


def test_late_arriving_older_policy_does_not_invert_history(conn, test_org, policy):
    _fact(conn, test_org, policy, "350000000", start="2026-04-01", end="2027-03-31")
    old = _fact(conn, test_org, policy, "300000000", start="2025-04-01", end="2026-03-31")
    assert old["valid_to"] is not None               # closed as historical
    cur = kc.get_current_value_or_conflict(conn, str(policy), test_org, "policy.coverage_limit")
    assert cur["current_value"]["value"] == "350000000"
    assert [f["value"] for f in kc.get_facts_in_force(conn, str(policy), test_org, "2025-09-01")["in_force"]] == ["300000000"]


def test_overlapping_periods_with_different_values_are_a_contradiction(conn, test_org, policy):
    _fact(conn, test_org, policy, "300000000", start="2026-01-01", end="2026-12-31")
    b = _fact(conn, test_org, policy, "350000000", start="2026-06-01", end="2027-05-31")
    assert b["status"] == "contradiction_flagged"


def test_no_periods_behaves_exactly_as_before(conn, test_org, policy):
    _fact(conn, test_org, policy, "300000000")
    b = _fact(conn, test_org, policy, "350000000")
    assert b["status"] == "contradiction_flagged"


def test_undated_facts_are_listed_separately_never_assumed_in_force(conn, test_org, policy):
    _fact(conn, test_org, policy, "300000000")
    res = kc.get_facts_in_force(conn, str(policy), test_org, "2026-01-01")
    assert res["in_force"] == []
    assert [f["value"] for f in res["undated_current"]] == ["300000000"]


def test_correction_keeps_the_period_and_in_force_returns_one_row(conn, test_org, policy):
    a = _fact(conn, test_org, policy, "300000000", start="2026-04-01", end="2027-03-31")
    kc.correct_fact(conn, str(a["id"]), test_org, new_value="320000000", normalized_value=320000000,
                    reviewed_by="tester")
    res = kc.get_facts_in_force(conn, str(policy), test_org, "2026-06-01")
    assert [f["value"] for f in res["in_force"]] == ["320000000"]


def test_bad_period_is_rejected(conn, test_org, policy):
    with pytest.raises(ValueError):
        _fact(conn, test_org, policy, "300000000", start="2026-04-01", end="2025-01-01")
    with pytest.raises(ValueError):
        _fact(conn, test_org, policy, "300000000", start="not-a-date")


# --- relationships ------------------------------------------------------------

def test_allowed_relationship_is_created_and_idempotent(conn, test_org, make_entity):
    biz, pol = make_entity(test_org, entity_type="business"), make_entity(test_org, entity_type="policy")
    r1 = kc.create_relationship(conn, organization_id=test_org, from_entity_id=biz,
                                to_entity_id=pol, relationship_type="has_policy")
    r2 = kc.create_relationship(conn, organization_id=test_org, from_entity_id=biz,
                                to_entity_id=pol, relationship_type="has_policy")
    assert str(r1["id"]) == str(r2["id"])
    assert len(kc.find_relationships(conn, test_org, biz, "has_policy")) == 1


def test_unknown_or_mismatched_relationship_is_rejected(conn, test_org, make_entity):
    biz, pol, asset = (make_entity(test_org, entity_type=t) for t in ("business", "policy", "asset"))
    with pytest.raises(ValueError):
        kc.create_relationship(conn, organization_id=test_org, from_entity_id=biz,
                               to_entity_id=pol, relationship_type="befriends")
    with pytest.raises(ValueError):            # a policy cannot "own" an asset
        kc.create_relationship(conn, organization_id=test_org, from_entity_id=pol,
                               to_entity_id=asset, relationship_type="owns")


def test_cross_tenant_relationship_is_rejected_by_the_service(conn, two_test_orgs, make_entity):
    org_a, org_b = two_test_orgs
    biz_a = make_entity(org_a, entity_type="business")
    pol_b = make_entity(org_b, entity_type="policy")
    with pytest.raises(ValueError):
        kc.create_relationship(conn, organization_id=org_a, from_entity_id=biz_a,
                               to_entity_id=pol_b, relationship_type="has_policy")


# --- whole-entity read + vertical slice ---------------------------------------

def test_vertical_slice_business_asset_location_policy_coverage_evidence(conn, test_org, make_entity, make_document):
    biz, asset, loc, pol, cov = (make_entity(test_org, entity_type=t) for t in
                                 ("business", "asset", "location", "policy", "coverage"))
    for a, b, t in ((biz, asset, "owns"), (asset, loc, "located_at"), (biz, pol, "has_policy"),
                    (pol, cov, "contains"), (cov, asset, "protects")):
        kc.create_relationship(conn, organization_id=test_org, from_entity_id=a, to_entity_id=b,
                               relationship_type=t)
    doc = make_document(test_org, filename="01_property_policy.pdf",
                        extracted_text="Perils Covered: Fire")
    _fact(conn, test_org, cov, "Fire", doc=doc, snippet="Perils Covered: Fire",
          fact_type="policy.coverage")

    k = kc.get_entity_knowledge(conn, str(pol), test_org)
    assert [e["name"] for e in k["relationships"]["outgoing"]] and \
        k["relationships"]["outgoing"][0]["type"] == "contains"
    assert k["relationships"]["incoming"][0]["entity_type"] == "business"

    kcov = kc.get_entity_knowledge(conn, str(cov), test_org)
    fact = kcov["current_facts"][0]
    ev = fact["evidence"][0]                   # entity -> fact -> evidence -> document/page
    assert fact["value"] == "Fire" and ev["page"] == 1 and ev["document_filename"] == "01_property_policy.pdf"


def test_entity_knowledge_is_tenant_isolated(conn, two_test_orgs, make_entity):
    org_a, org_b = two_test_orgs
    biz = make_entity(org_a, entity_type="business")
    assert kc.get_entity_knowledge(conn, str(biz), org_b) is None
    assert kc.get_entity_knowledge(conn, str(uuid.uuid4()), org_a) is None


def test_entity_knowledge_lists_unresolved_conflicts(conn, test_org, policy):
    _fact(conn, test_org, policy, "300000000")
    _fact(conn, test_org, policy, "350000000")
    k = kc.get_entity_knowledge(conn, str(policy), test_org)
    assert [c["fact_type"] for c in k["unresolved_conflicts"]] == ["policy.coverage_limit"]
    assert len(k["unresolved_conflicts"][0]["fact_ids"]) == 2


# --- human review ---------------------------------------------------------------

def test_verify_requires_evidence_and_records_reviewer(conn, test_org, policy, make_document):
    doc = make_document(test_org, filename="p.pdf", extracted_text="Deductible: 1000000")
    with_ev = _fact(conn, test_org, policy, "1000000", doc=doc, snippet="Deductible: 1000000",
                    fact_type="policy.deductible")
    done = kc.review_fact(conn, str(with_ev["id"]), test_org, action="verify", reviewed_by="Asha")
    assert done["status"] == "verified" and done["reviewed_by"] == "Asha"

    no_ev = kc.create_fact(conn, organization_id=test_org, entity_id=str(policy),
                           fact_type="policy.exclusion", value="War", confidence=CONF, produced_by="test")
    with pytest.raises(ValueError):
        kc.review_fact(conn, str(no_ev["id"]), test_org, action="verify", reviewed_by="Asha")


def test_contradicted_fact_cannot_be_verified_only_corrected(conn, test_org, policy):
    _fact(conn, test_org, policy, "300000000")
    b = _fact(conn, test_org, policy, "350000000")
    with pytest.raises(ValueError):
        kc.review_fact(conn, str(b["id"]), test_org, action="verify", reviewed_by="Asha")


def test_reject_keeps_the_row_but_removes_it_from_current(conn, test_org, policy):
    f = _fact(conn, test_org, policy, "300000000")
    out = kc.review_fact(conn, str(f["id"]), test_org, action="reject", reviewed_by="Asha")
    assert out["status"] == "superseded" and out["valid_to"] is not None
    assert db.get_fact(conn, str(f["id"]), test_org) is not None        # kept for audit
    assert kc.get_current_value_or_conflict(conn, str(policy), test_org,
                                            "policy.coverage_limit")["current_value"] is None


def test_review_validation_and_tenant_isolation(conn, two_test_orgs, make_entity):
    org_a, org_b = two_test_orgs
    ent = make_entity(org_a, entity_type="policy")
    f = _fact(conn, org_a, ent, "300000000")
    with pytest.raises(ValueError):
        kc.review_fact(conn, str(f["id"]), org_b, action="reject", reviewed_by="Asha")   # other tenant
    with pytest.raises(ValueError):
        kc.review_fact(conn, str(f["id"]), org_a, action="approve", reviewed_by="Asha")  # bad action
    with pytest.raises(ValueError):
        kc.review_fact(conn, str(f["id"]), org_a, action="reject", reviewed_by="  ")     # no reviewer


# --- API ---------------------------------------------------------------------------

def test_api_endpoints_round_trip(conn, test_org, policy):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import knowledge as api_knowledge

    conn.commit() if hasattr(conn, "commit") else None
    f = _fact(conn, test_org, policy, "300000000", start="2026-04-01", end="2027-03-31")
    conn.commit() if hasattr(conn, "commit") else None

    app = FastAPI()
    app.include_router(api_knowledge.router)
    client = TestClient(app)
    h = {"X-Organization-Id": test_org}

    r = client.get(f"/knowledge/entities/{policy}/knowledge?as_of=2026-06-01", headers=h)
    assert r.status_code == 200
    assert [x["value"] for x in r.json()["in_force"]["in_force"]] == ["300000000"]
    assert client.get(f"/knowledge/entities/{policy}/knowledge?as_of=junk", headers=h).status_code == 400
    assert client.get(f"/knowledge/entities/{policy}/relationships", headers=h).status_code == 200
    assert client.get(f"/knowledge/entities/{uuid.uuid4()}/knowledge", headers=h).status_code == 404

    bad = client.post(f"/knowledge/facts/{f['id']}/review",
                      json={"action": "verify", "reviewed_by": "Asha"}, headers=h)
    assert bad.status_code == 400                     # no evidence -> cannot verify
    ok = client.post(f"/knowledge/facts/{f['id']}/review",
                     json={"action": "reject", "reviewed_by": "Asha"}, headers=h)
    assert ok.status_code == 200 and ok.json()["status"] == "superseded"
