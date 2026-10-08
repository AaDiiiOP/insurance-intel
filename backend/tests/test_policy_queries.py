"""Phase 1C (v0.11.0) — policy questions through POST /knowledge/query's engine."""
import knowledge.service as kc
from knowledge import query as kq
import policy_intelligence as pi
from tests.test_policy_intelligence import analysed, _model_stub  # noqa: F401
from tests.test_knowledge_query import world, ask as world_ask, abstains  # noqa: F401


def q(conn, org, pid, question, as_of="2026-08-01"):
    return kq.query(conn, organization_id=org, question=question, entity_id=pid, as_of=as_of, use_llm=False)


def test_insurer_number_conditions_definitions_sublimits(conn, analysed):
    org, pid = analysed
    r = q(conn, org, pid, "Which insurer issued this policy?")
    assert r["answer"] == "Meridian Shield Insurance Ltd." and r["evidence"][0]["snippet"].startswith("Carrier:")
    assert q(conn, org, pid, "What is the policy number?")["answer"] == "SYN-PROP-0099"
    assert len(q(conn, org, pid, "What conditions apply?")["answer"]) == 2
    d = q(conn, org, pid, "What does flood mean?")
    assert d["answer"][0].startswith('"Flood" means') and d["evidence"][0]["page"] == 2
    assert abstains(q(conn, org, pid, "What does earthquake mean?"))
    assert q(conn, org, pid, "What sublimits apply?")["answer"] == ["Flood sub-limit: n50,000,000"]


def test_coverage_specific_limit_and_ranking(conn, analysed):
    org, pid = analysed
    r = q(conn, org, pid, "What is the limit for business interruption?")
    assert r["answer"] == "60,000,000" and r["interpretation"]["fact_type"] == "coverage.limit"
    assert r["evidence"][0]["snippet"].startswith("Business Interruption Sum Insured")
    top = q(conn, org, pid, "Which coverage has the highest limit?")
    assert top["answer"] == "Property Damage (300,000,000)" and len(top["evidence"]) == 2
    low = q(conn, org, pid, "Which coverage has the lowest limit?")
    assert low["answer"].startswith("Business Interruption")


def test_contradiction_listing_and_ranking_abstains_on_conflict(conn, analysed, make_document):
    org, pid = analysed
    assert q(conn, org, pid, "Which coverage has a contradiction?")["answer"] == []
    cov = [c for c in pi.get_policy(conn, org, pid)["coverages"] if c["name"] == "Property Damage"][0]
    doc = make_document(org, filename="quote.pdf", extracted_text="[Page 1]\nProperty Damage Sum Insured: 280,000,000\n")
    kc.reconcile_fact(conn, organization_id=org, entity_id=cov["coverage_id"], fact_type="coverage.limit",
                      value="280,000,000", normalized_value=2.8e8,
                      confidence={"value": 1.0, "type": "deterministic", "method": "t", "note": None},
                      produced_by="t", document_id=doc, evidence_page=1,
                      evidence_snippet="Property Damage Sum Insured: 280,000,000", evidence_source="text_layer",
                      effective_from="2026-04-01", effective_to="2027-03-31")
    c = q(conn, org, pid, "Which coverage has a contradiction?")
    assert c["answer"] == ["Property Damage: 300,000,000 vs 280,000,000"] and c["review_status"] == "contradiction"
    rank = q(conn, org, pid, "Which coverage has the highest limit?")
    assert abstains(rank) and "Cannot rank" in rank["reason"]
    assert abstains(q(conn, org, pid, "What is the limit for property damage?"))


def test_endorsements_effective_and_status(conn, analysed):
    org, pid = analysed
    e = q(conn, org, pid, "Which endorsements became effective during the policy period?")
    assert e["answer"] == ["SYN-END-0099-01 (effective 2026-07-19)"] and e["evidence"]
    assert q(conn, org, pid, "Is the policy in force?", as_of="2026-08-01")["answer"] == "current on 2026-08-01"
    assert q(conn, org, pid, "Is the policy expired?", as_of="2027-06-01")["answer"] == "expired on 2027-06-01"


def test_what_changed_from_previous_policy(conn, world):
    r = world_ask(conn, world, "What changed from the previous policy for Orion Plastics?")
    assert r["interpretation"]["intent"] == "policy_changes"
    assert any(a.startswith("limit_increase: 150,000,000 -> 180,000,000, +20.0%") for a in r["answer"])
    assert any(a.startswith("coverage_added: Business Interruption") for a in r["answer"])
    assert r["evidence"]


def test_one_period_change_question_abstains(conn, analysed):
    org, pid = analysed
    r = q(conn, org, pid, "What changed during renewal?")
    assert abstains(r) and "Only one stated policy period" in r["reason"]


def test_policy_questions_from_another_tenant(conn, analysed):
    import db
    org, pid = analysed
    other = str(db.create_organization(conn, "pytest-org-pq-b")["id"])
    for question in ("Which insurer issued this policy?", "Which coverage has the highest limit?",
                     "Which coverage has a contradiction?", "What changed during renewal?"):
        r = q(conn, other, pid, question)
        assert r["answer"] is None and not r["facts_used"] and not r["evidence"]
