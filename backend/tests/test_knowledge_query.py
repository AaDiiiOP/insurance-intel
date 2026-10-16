"""
v0.10.0 Knowledge query layer — the reviewer's 20 required cases.

Real Postgres, no real model (the two LLM cases monkeypatch the model call).
Every unsafe case must ABSTAIN (answer None, needs_review True), never guess.
"""
import pytest

import db
import knowledge.service as kc
from knowledge import query as kq
from knowledge.fact_types import target_entity_kind

CONF = {"value": 0.9, "type": "model_self_reported", "method": "test", "note": None}
P25 = ("2025-04-01", "2026-03-31")
P26 = ("2026-04-01", "2027-03-31")
TODAY = "2026-10-04"

POLICY_25 = ("[Page 1]\nProperty Policy\nPeriod: 1 April 2025 to 31 March 2026\n"
             "Perils Covered: Property Damage\nCoverage Limit: 150,000,000\nDeductible: 500,000\n"
             "Exclusions: War and nuclear risks\n")
POLICY_26 = ("[Page 1]\nProperty Policy\nPeriod: 1 April 2026 to 31 March 2027\n"
             "Perils Covered: Property Damage, Business Interruption\n"
             "[Page 2]\nCoverage Limit: 180,000,000\nDeductible: 500,000\n"
             "Exclusions: War and nuclear risks; Flood\n"
             "Claims must be notified within 14 days of the loss.\n")
SURVEY = ("[Page 1]\nRisk Survey\nOccupancy: Plastic moulding\n"
          "Occupancy observed: Warehousing\nSum Insured: 200,000,000\n")


@pytest.fixture
def world(conn, test_org, make_document):
    """Business -> owns Asset, has_policy Policy, with a 2025 and a 2026
    policy period, a contradiction on occupancy, and an undated sum insured."""
    org = test_org
    d25 = make_document(org, filename="2025_policy.pdf", extracted_text=POLICY_25)
    d26 = make_document(org, filename="2026_policy.pdf", extracted_text=POLICY_26)
    dsv = make_document(org, filename="risk_survey.pdf", extracted_text=SURVEY)
    biz = kc.get_or_create_entity(conn, organization_id=org, entity_type="business", name="Orion Plastics Pvt Ltd")
    asset = kc.get_or_create_entity(conn, organization_id=org, entity_type="asset", name="Orion plant asset")
    pol = kc.get_or_create_entity(conn, organization_id=org, entity_type="policy", name="Orion policy record")
    kc.create_relationship(conn, organization_id=org, from_entity_id=str(biz["id"]),
                           to_entity_id=str(asset["id"]), relationship_type="owns")
    kc.create_relationship(conn, organization_id=org, from_entity_id=str(biz["id"]),
                           to_entity_id=str(pol["id"]), relationship_type="has_policy")
    ents = {"asset": asset, "policy": pol}

    def fact(ft, value, doc, snippet, page=1, period=None, num=None):
        e = ents[target_entity_kind(ft)]
        return kc.reconcile_fact(
            conn, organization_id=org, entity_id=str(e["id"]), fact_type=ft, value=value,
            normalized_value=num, confidence=CONF, produced_by="test", document_id=doc,
            evidence_page=page, evidence_snippet=snippet, evidence_source="text_layer",
            effective_from=period[0] if period else None, effective_to=period[1] if period else None)

    f = {}
    f["limit25"] = fact("policy.coverage_limit", "150,000,000", d25, "Coverage Limit: 150,000,000", 1, P25, 1.5e8)
    f["limit26"] = fact("policy.coverage_limit", "180,000,000", d26, "Coverage Limit: 180,000,000", 2, P26, 1.8e8)
    f["ded25"] = fact("policy.deductible", "500,000", d25, "Deductible: 500,000", 1, P25, 5e5)
    f["ded26"] = fact("policy.deductible", "500,000", d26, "Deductible: 500,000", 2, P26, 5e5)
    f["cov25"] = fact("policy.coverage", "Property Damage", d25, "Perils Covered: Property Damage", 1, P25)
    f["cov26a"] = fact("policy.coverage", "Property Damage", d26, "Perils Covered: Property Damage, Business Interruption", 1, P26)
    f["cov26b"] = fact("policy.coverage", "Business Interruption", d26, "Perils Covered: Property Damage, Business Interruption", 1, P26)
    f["occ1"] = fact("property.occupancy", "Plastic moulding", dsv, "Occupancy: Plastic moulding")
    f["occ2"] = fact("property.occupancy", "Warehousing", dsv, "Occupancy observed: Warehousing")
    f["si"] = fact("property.sum_insured", "200,000,000", dsv, "Sum Insured: 200,000,000", 1, None, 2e8)
    conn.commit()
    return {"org": org, "biz": biz, "asset": asset, "policy": pol, "facts": f,
            "docs": {"d25": d25, "d26": d26, "survey": dsv}}


def ask(conn, w, question, **kw):
    kw.setdefault("as_of", TODAY)
    kw.setdefault("use_llm", False)
    return kq.query(conn, organization_id=w["org"], question=question, **kw)


def abstains(r):
    return r["answer"] is None and r["needs_review"] is True


# 1 direct fact lookup
def test_01_direct_fact_lookup(conn, world):
    r = ask(conn, world, "What is the deductible for Orion Plastics?")
    assert r["answer"] == "500,000" and r["answer_type"] == "deterministic"
    assert r["interpretation"]["fact_type"] == "policy.deductible"


# 2 entity-scoped lookup (explicit entity id, no name in the question)
def test_02_entity_scoped_lookup(conn, world):
    r = ask(conn, world, "What is the coverage limit?", entity_id=str(world["policy"]["id"]))
    assert r["answer"] == "180,000,000"
    assert r["entities_used"][0]["entity_id"] == str(world["policy"]["id"])


# 3 temporal lookup with as_of
def test_03_temporal_lookup(conn, world):
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?", as_of="2025-06-01")
    assert r["answer"] == "150,000,000"
    assert r["effective_period"] == {"from": P25[0], "to": P25[1]}


# 4 current vs historical
def test_04_current_vs_historical(conn, world):
    cur = ask(conn, world, "What is the coverage limit for Orion Plastics?")
    prev = ask(conn, world, "What was the previous coverage limit for Orion Plastics?")
    assert cur["answer"] == "180,000,000" and prev["answer"] == "150,000,000"
    assert cur["effective_period"]["from"] == P26[0] and prev["effective_period"]["from"] == P25[0]


# 5 contradiction surfaces and abstains
def test_05_contradiction(conn, world):
    r = ask(conn, world, "What is the occupancy of Orion Plastics?")
    assert abstains(r)
    assert r["review_status"] == "contradiction" and r["contradictions"]
    assert set(r["values"]) == {"Plastic moulding", "Warehousing"}


# 6 superseded (rejected) fact is never used
def test_06_superseded_fact_not_used(conn, world):
    kc.review_fact(conn, str(world["facts"]["cov26b"]["id"]), world["org"], action="reject", reviewed_by="Asha")
    r = ask(conn, world, "What coverage does Orion Plastics have?")
    assert r["answer"] == ["Property Damage"]


# 7 unverified fact: answered but not authoritative
def test_07_unverified_fact(conn, world):
    r = ask(conn, world, "What is the deductible for Orion Plastics?")
    assert r["review_status"] == "unverified" and r["authoritative"] is False and r["needs_review"] is True
    kc.review_fact(conn, str(world["facts"]["ded26"]["id"]), world["org"], action="verify", reviewed_by="Asha")
    r2 = ask(conn, world, "What is the deductible for Orion Plastics?")
    assert r2["review_status"] == "verified" and r2["authoritative"] is True and r2["needs_review"] is False


# 8 evidence retrieval
def test_08_evidence_returned(conn, world):
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?")
    ev = r["evidence"]
    assert ev and ev[0]["snippet"] == "Coverage Limit: 180,000,000"
    assert ev[0]["document_id"] == world["docs"]["d26"] and ev[0]["fact_id"]


# 9 source page retrieval
def test_09_source_pages(conn, world):
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?")
    assert r["source_pages"] == [{"document": "2026_policy.pdf", "page": 2}]
    assert r["source_documents"] == ["2026_policy.pdf"]


# 10 tenant isolation
def test_10_tenant_isolation(conn, world):
    other = str(db.create_organization(conn, "pytest-org-query-other")["id"])
    r = kq.query(conn, organization_id=other, question="What is the coverage limit for Orion Plastics?",
                 as_of=TODAY, use_llm=False)
    assert abstains(r) and r["facts_used"] == [] and r["candidates"] == []
    r2 = kq.query(conn, organization_id=other, question="What is the coverage limit?",
                  entity_id=str(world["policy"]["id"]), as_of=TODAY, use_llm=False)
    assert abstains(r2) and r2["facts_used"] == []


# 11 unknown question (no fact type, nothing retrieved)
def test_11_unknown_question(conn, world):
    r = ask(conn, world, "Who is the broker relationship manager for Orion Plastics?")
    assert abstains(r) and r["answer_type"] == "none"


# 12 unsupported question: no entity named
def test_12_unsupported_no_entity(conn, world):
    r = ask(conn, world, "What is the deductible?")
    assert abstains(r) and "entity" in r["reason"].lower()


# 13 policy comparison is evidence-backed
def test_13_policy_comparison_evidence(conn, world, test_org, make_document):
    import policy_comparison as pc
    other = kc.create_entity(conn, organization_id=test_org, entity_type="policy", name="Orion renewal quote")
    d = make_document(test_org, filename="quote.pdf", extracted_text="[Page 1]\nCoverage Limit: 200,000,000\n")
    kc.reconcile_fact(conn, organization_id=test_org, entity_id=str(other["id"]), fact_type="policy.coverage_limit",
                      value="200,000,000", normalized_value=2e8, confidence=CONF, produced_by="test",
                      document_id=d, evidence_page=1, evidence_snippet="Coverage Limit: 200,000,000",
                      evidence_source="text_layer")
    out = pc.compare_entities(conn, organization_id=test_org, left_entity_id=str(world["policy"]["id"]),
                              right_entity_id=str(other["id"]), fact_types=["policy.coverage_limit"])
    c = out["fact_comparisons"][0]
    assert c["change"] == "increased" and c["assessment"] == "evidenced"
    assert c["left"]["evidence"][0]["page"] == 2 and c["right"]["evidence"][0]["document_filename"] == "quote.pdf"


# 14 percentage change across renewal
def test_14_percentage_change(conn, world):
    r = ask(conn, world, "How did the coverage limit change at renewal for Orion Plastics?")
    ch = r["change"]
    assert r["answer"] == "increased"
    assert ch["old_value"] == "150,000,000" and ch["new_value"] == "180,000,000"
    assert ch["absolute_change"] == 30_000_000 and ch["percent_change"] == 20.0
    assert {e["document_id"] for e in r["evidence"]} == {world["docs"]["d25"], world["docs"]["d26"]}


# 15 LLM answer accepted only with a verified quote
def test_15_llm_answer_requires_evidence(conn, world, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
        "answer": "Claims must be notified within 14 days", "passage": 1,
        "quote": "Claims must be notified within 14 days of the loss."})
    r = ask(conn, world, "How soon must claims be notified for Orion Plastics?", use_llm=True)
    assert r["answer_type"] == "llm_synthesis" and "14 days" in r["answer"]
    assert r["evidence"][0]["document_filename"] == "2026_policy.pdf"
    assert r["authoritative"] is False and r["needs_review"] is True


# 16 LLM answer with insufficient evidence is rejected
def test_16_llm_insufficient_evidence(conn, world, monkeypatch):
    import llm
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: {
        "answer": "Claims must be notified within 30 days", "passage": 1,
        "quote": "Claims must be notified within 30 days of the loss."})
    r = ask(conn, world, "How soon must claims be notified for Orion Plastics?", use_llm=True)
    assert abstains(r) and "not in the cited passage" in r["reason"]


# 17 duplicate / ambiguous entity names abstain
def test_17_duplicate_entity_names(conn, world, test_org):
    kc.create_entity(conn, organization_id=test_org, entity_type="business", name="Orion Plastics Unit 2")
    kc.create_entity(conn, organization_id=test_org, entity_type="business", name="Orion")
    r = ask(conn, world, "What is the deductible for Orion?")
    assert abstains(r) or r["entities_used"][0]["name"] == "Orion"
    # an alias of the real business still resolves to it
    alias = kc.get_or_create_entity(conn, organization_id=test_org, entity_type="business",
                                    name="Orion Plastics Private Limited")
    assert str(alias["id"]) == str(world["biz"]["id"])


# 18 relationship traversal business -> policy
def test_18_relationship_traversal(conn, world):
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?")
    assert r["relationships_used"][0]["type"] == "has_policy"
    assert {e["entity_type"] for e in r["entities_used"]} == {"business", "policy"}


# 19 cross-document reconciliation: same value from 2 docs, 2 periods
def test_19_cross_document_reconciliation(conn, world):
    r = ask(conn, world, "Did the deductible change at renewal for Orion Plastics?")
    assert r["answer"] == "unchanged" and r["change"]["percent_change"] == 0.0


# 20 date-specific query in the question text; undated facts not assumed in force
def test_20_date_specific_query(conn, world):
    r = ask(conn, world, "What was the coverage limit for Orion Plastics on 1 June 2025?")
    assert r["interpretation"]["mode"] == "as_of" and r["answer"] == "150,000,000"
    r2 = ask(conn, world, "What was the sum insured for Orion Plastics on 1 June 2025?")
    assert abstains(r2) and r2["candidates"]           # undated -> not assumed in force


def test_policy_period_answer(conn, world):
    r = ask(conn, world, "What is the policy period for Orion Plastics?")
    assert r["answer"] == "2026-04-01 to 2027-03-31" and r["authoritative"] is True
    assert len(r["values"]) == 2


def test_api_query_round_trip(conn, world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import knowledge as api_knowledge
    from tests.auth_helpers import signed_in
    c = signed_in([api_knowledge.router], conn, world["org"]); h = {"X-Organization-Id": world["org"]}
    r = c.post("/knowledge/query", json={"question": "What is the coverage limit for Orion Plastics?",
                                         "as_of": TODAY, "use_llm": False}, headers=h)
    assert r.status_code == 200 and r.json()["answer"] == "180,000,000"
    assert c.post("/knowledge/query", json={"question": " "}, headers=h).status_code == 400
    assert c.post("/knowledge/query", json={"question": "x", "as_of": "junk"}, headers=h).status_code == 400


def test_21_expired_period_is_not_current(conn, world):
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?", as_of="2028-01-01")
    assert abstains(r) and "No stated policy period covers" in r["reason"]


def test_query_check_grades_answers(conn, world, tmp_path):
    import json
    import eval_property_policy as ev
    comp = tmp_path / "AUR-TEST-001"
    comp.mkdir()
    gt = {"phase1_facts": [
        {"fact_type": "policy.coverage_limit", "value": "180,000,000", "value_inr": 180000000},
        {"fact_type": "policy.deductible", "value": "999", "value_inr": 999},
        {"fact_type": "property.occupancy", "value": "Plastic moulding"},
    ]}
    (comp / "phase1_ground_truth.json").write_text(json.dumps(gt))
    ingest = {"analysis": {"business": world["biz"],
                           "documents_processed": [{"policy_period": list(P26)}]}}
    out = ev.query_check_company(conn, world["org"], comp, ingest)
    verdicts = {q["fact_type"]: q["verdict"] for q in out["questions"]}
    assert verdicts == {"policy.coverage_limit": "correct", "policy.deductible": "wrong",
                        "property.occupancy": "abstained"}
    s = ev.summarise_query_check([out])
    assert s["wrong"] == 1 and s["hard_gate_zero_wrong_answers"] is False
    assert s["interpretation_ok_pct"] == 100.0


def test_benchmark_questions_interpreted():
    import eval_property_policy as ev
    for ft, q in ev.QUERY_QUESTIONS.items():
        i = kq.interpret(q)
        assert (i["intent"], i["fact_type"], i["mode"]) == ("fact", ft, "current"), q
    assert kq.interpret("What exclusions were added at renewal?")["mode"] == "change"
