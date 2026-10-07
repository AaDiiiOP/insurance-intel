"""v0.10.2 Phase 1B audit fixes — corrections across periods, and query routing."""
import pytest

import db
import knowledge.service as kc
from knowledge import query as kq
from tests.test_knowledge_query import world, ask, abstains  # noqa: F401


# --- corrections ------------------------------------------------------------

def test_correcting_last_years_value_keeps_it_historical(conn, world):
    org, pol = world["org"], str(world["policy"]["id"])
    new = kc.correct_fact(conn, str(world["facts"]["limit25"]["id"]), org,
                          new_value="155,000,000", normalized_value=1.55e8, reviewed_by="Asha")
    assert new["valid_to"] is not None                       # stays historical
    assert kc.get_current_value(conn, pol, org, "policy.coverage_limit")["value"] == "180,000,000"
    k = kc.get_entity_knowledge(conn, pol, org)
    assert [f["value"] for f in k["current_facts"] if f["fact_type"] == "policy.coverage_limit"] == ["180,000,000"]
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?", as_of="2025-06-01")
    assert r["answer"] == "155,000,000" and r["review_status"] == "verified"
    assert r["evidence"][0]["source"] == "reviewer_correction"
    ch = ask(conn, world, "How did the coverage limit change at renewal for Orion Plastics?")["change"]
    assert ch["old_value"] == "155,000,000" and ch["new_value"] == "180,000,000"


def test_correcting_the_current_value_is_used_not_the_old_one(conn, world):
    org = world["org"]
    kc.correct_fact(conn, str(world["facts"]["limit26"]["id"]), org,
                    new_value="185,000,000", normalized_value=1.85e8, reviewed_by="Asha")
    r = ask(conn, world, "What is the coverage limit for Orion Plastics?")
    assert r["answer"] == "185,000,000" and r["authoritative"] is True
    assert r["evidence"] and r["evidence"][0]["page"] == 2


def test_an_already_replaced_value_cannot_be_corrected_again(conn, world):
    org = world["org"]
    old_id = str(world["facts"]["limit26"]["id"])
    kc.correct_fact(conn, old_id, org, new_value="185,000,000", normalized_value=1.85e8, reviewed_by="Asha")
    with pytest.raises(ValueError, match="already replaced"):
        kc.correct_fact(conn, old_id, org, new_value="999", normalized_value=999.0, reviewed_by="Ravi")


# --- routing: questions that must NOT be answered with a stored fact ---------

@pytest.mark.parametrize("question", [
    "What is the time limit for notifying a claim for Orion Plastics?",
    "What is the age limit of the building for Orion Plastics?",
    "When was the Orion Plastics building built?",
    "What is the insurer address for Orion Plastics?",
    "What is the broker contact address for Orion Plastics?",
    "Is there a waiting period for Orion Plastics?",
])
def test_misleading_questions_do_not_get_a_stored_fact(conn, world, question):
    i = kq.interpret(question)
    assert i["intent"] == "unknown", (question, i)


@pytest.mark.parametrize("question", [
    "What is the deductible for flood for Orion Plastics?",
    "What is the limit of indemnity for business interruption for Orion Plastics?",
])
def test_peril_specific_value_is_not_answered_with_the_policy_wide_value(conn, world, question):
    r = ask(conn, world, question)
    assert abstains(r) and "specific" in r["reason"].lower()
    assert r["candidates"]                                  # the policy-wide value, for a person


def test_yes_no_membership_questions(conn, world):
    yes = ask(conn, world, "Does the Orion Plastics policy cover business interruption?")
    assert yes["answer"] == ["Business Interruption"] and yes["answer_type"] == "deterministic"
    no = ask(conn, world, "Does the Orion Plastics policy cover flood?")
    assert abstains(no) and "not among" in no["reason"]
    ex = ask(conn, world, "Is flood excluded for Orion Plastics?")
    assert abstains(ex)                                     # no exclusion facts recorded


def test_endorsements_added_is_a_list_not_a_renewal_diff(conn, world):
    i = kq.interpret("What endorsements were added?")
    assert i["mode"] == "current"
    assert kq.interpret("What exclusions were added at renewal?")["mode"] == "change"


def test_question_length_is_bounded(conn, world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import knowledge as api_knowledge
    app = FastAPI(); app.include_router(api_knowledge.router)
    r = TestClient(app).post("/knowledge/query", json={"question": "x" * 5000},
                             headers={"X-Organization-Id": world["org"]})
    assert r.status_code == 400


def test_safety_probes_run_and_pass_on_safe_data(conn, world, tmp_path):
    import json
    import eval_property_policy as ev
    comp = tmp_path / "AUR-TEST-002"
    comp.mkdir()
    (comp / "phase1_ground_truth.json").write_text(json.dumps({"phase1_facts": [
        {"fact_type": "policy.coverage_limit", "value": "180,000,000", "value_inr": 180000000}]}))
    ingest = {"analysis": {"business": world["biz"], "documents_processed": [{"policy_period": ["2026-04-01", "2027-03-31"]}]}}
    out = ev.query_check_company(conn, world["org"], comp, ingest)
    names = {p["probe"]: p["passed"] for p in out["probes"]}
    assert names["policy_period"] and names["date_before_any_policy_period"]
    assert names["peril_specific_deductible"] and names["cross_tenant_query"] and names["misleading_limit_question"]
    assert "previous_with_one_period" not in names          # world has two periods
    assert ev.summarise_query_check([out])["probes_failed"] == 0


def test_undated_limit_not_given_for_a_day_outside_known_periods(conn, test_org, make_document):
    """Reproduces the v0.10.2 real-Qwen probe failure (AUR-SYN-001, Ollama 500 on the
    policy chunk): the limit exists only undated (from the schedule) while the policy
    period is known from other facts."""
    org = test_org
    d_pol = make_document(org, filename="policy.pdf", extracted_text="[Page 1]\nPeriod: 8 April 2026 to 31 March 2027\nDeductible: 1,500,000\n")
    d_sch = make_document(org, filename="schedule.pdf", extracted_text="[Page 1]\nTotal coverage limit: 600,000,000\n")
    biz = kc.create_entity(conn, organization_id=org, entity_type="business", name="AUR-TEST-001")
    pol = kc.create_entity(conn, organization_id=org, entity_type="policy", name="AUR-TEST-001 — policy")
    kc.create_relationship(conn, organization_id=org, from_entity_id=str(biz["id"]), to_entity_id=str(pol["id"]),
                           relationship_type="has_policy")
    conf = {"value": 0.9, "type": "model_self_reported", "method": "t", "note": None}
    kc.reconcile_fact(conn, organization_id=org, entity_id=str(pol["id"]), fact_type="policy.deductible", value="1,500,000",
                      normalized_value=1.5e6, confidence=conf, produced_by="t", document_id=d_pol, evidence_page=1,
                      evidence_snippet="Deductible: 1,500,000", evidence_source="text_layer",
                      effective_from="2026-04-08", effective_to="2027-03-31")
    kc.reconcile_fact(conn, organization_id=org, entity_id=str(pol["id"]), fact_type="policy.coverage_limit", value="600,000,000",
                      normalized_value=6e8, confidence=conf, produced_by="t", document_id=d_sch, evidence_page=1,
                      evidence_snippet="Total coverage limit: 600,000,000", evidence_source="text_layer")
    before = kq.query(conn, organization_id=org, question="What is the coverage limit?", entity_id=str(biz["id"]),
                      as_of="2026-04-07", use_llm=False)
    assert before["answer"] is None and "No stated policy period covers" in before["reason"]
    inside = kq.query(conn, organization_id=org, question="What is the coverage limit?", entity_id=str(biz["id"]),
                      as_of="2026-06-01", use_llm=False)
    assert inside["answer"] == "600,000,000"
