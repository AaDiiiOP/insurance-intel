"""Phase 1D (v0.12.0) — Business Risk Intelligence: reader, Knowledge Core wiring,
temporal observations, change detection, contradictions, queries, API, isolation."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import business_intelligence as bi
import business_structure as bs
import db
import knowledge.service as kc
import property_policy_intelligence as ppi
from api import businesses as api_businesses
from knowledge import query as kq
from tests.test_policy_period import _submission

POLICY = """[Page 1]
Property Policy
Acme Mills Pvt. Ltd.
Policy Number: SYN-PROP-0077
Carrier: Meridian Shield Insurance Ltd.
Period: 01 April 2026 to 31 March 2027
Property Location: 12 Mill Road, Pune, Maharashtra
Risk Factors: Cotton dust accumulation in carding area
"""
SCHEDULE = """[Page 1]
Coverage Schedule
Policy Number: SYN-PROP-0077
Insured: Acme Mills Pvt. Ltd.
Declared Values
Buildings and Civil Works: 200,000,000
Plant and Machinery: 120,000,000
Total Property Sum Insured: 320,000,000
"""
SURVEY = """[Page 1]
Property Risk Survey
Acme Mills Pvt. Ltd.
Survey
Location: 12 Mill Road, Pune, Maharashtra
Survey date: 10 March 2026
Operations
The insured operates cotton spinning. Estimated workforce: 400.
Observations
Overall risk rating: Moderate.
Fire protection: hydrants and portable extinguishers reported.
"""
LOSS_RUN = """[Page 1]
Synthetic Loss Run
Policy Link
Policy: SYN-PROP-0077
Insured: Acme Mills Pvt. Ltd.
Claims
Synthetic claims in period: 1.
Total incurred amount: 500,000.
"""
FOLLOW_UP = """[Page 1]
Property Risk Survey (Follow-up)
Acme Mills Pvt. Ltd.
Survey
Location: 12 Mill Road, Pune, Maharashtra
Additional Location: 7 Depot Lane, Chakan, Maharashtra
Survey date: 15 September 2026
Operations
The insured operates cotton spinning. Estimated workforce: 430.
Observations
Overall risk rating: Moderate.
Fire protection: hydrants, extinguishers and automatic sprinklers reported.
Security: CCTV reported.
Declared Values
Plant and Machinery: 150,000,000
Risk Notes
Identified risk: Cotton dust explosion in carding area.
Flood exposure: site lies next to a seasonal stream.
"""
SAME_DAY = """[Page 1]
Property Risk Survey (Addendum)
Acme Mills Pvt. Ltd.
Survey date: 15 September 2026
Operations
The insured operates cotton spinning. Estimated workforce: 445.
"""


def _stub(*a, **k):
    return {"facts": []}


@pytest.fixture
def biz(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", _stub)
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY), ("02_schedule.pdf", SCHEDULE),
                                       ("04_survey.pdf", SURVEY), ("05_loss_run.pdf", LOSS_RUN)])
    r = ppi.analyze_submission(conn, sub, test_org)
    return {"org": test_org, "sub": sub, "id": str(r["business"]["id"]), "policy": str(r["policy"]["id"]),
            "analysis": r}


def _add(conn, org, sub, filename, text):
    row = db.create_document(conn, organization_id=org, carrier_id=None, filename=filename, file_type="pdf",
                             size_bytes=len(text), pages=1, chars=len(text), status="parsed",
                             error_message=None, extracted_text=text)
    db.set_document_submission(conn, str(row["id"]), sub)
    return ppi.apply_business_documents(conn, sub, org, [str(row["id"])])


def ask(conn, b, question, as_of=None, org=None):
    return kq.query(conn, organization_id=org or b["org"], question=question, entity_id=b["id"],
                    as_of=as_of, use_llm=False)


# --- reader -------------------------------------------------------------------

def test_reader_reads_only_labelled_business_facts():
    st = bs.extract_business_structure(FOLLOW_UP)
    kinds = {(i["kind"], i["name"]) for i in st["items"]}
    assert st["role"] == "risk_survey" and st["observed_on"] == "2026-09-15"
    assert ("location", "7 Depot Lane, Chakan, Maharashtra") in kinds
    assert ("operation", "cotton spinning") in kinds and ("control", "Security") in kinds
    assert ("risk", "Cotton dust explosion in carding area") in kinds
    assert ("exposure", "Flood: site lies next to a seasonal stream") in kinds
    assert ("asset", "Plant and Machinery") in kinds
    # nothing inferred from an industry: a spinning mill without a stated risk has no risk
    assert not [i for i in bs.extract_business_structure(SURVEY)["items"] if i["kind"] in ("risk", "exposure")]


# --- Knowledge Core wiring ----------------------------------------------------

def test_profile_from_documents(conn, biz):
    p = bi.get_business_profile(conn, biz["org"], biz["id"])
    assert [i["name"] for i in p["locations"]] == ["12 Mill Road, Pune, Maharashtra"]
    assert [i["name"] for i in p["operations"]] == ["cotton spinning"]
    assert p["workforce"]["answer"] == "400" and p["workforce"]["observed_on"] == "2026-03-10"
    assert p["survey_risk_rating"]["answer"] == "Moderate"
    assert p["legal_name"]["answer"] == "Acme Mills Pvt. Ltd."
    assets = {i["name"]: i["value"]["answer"] for i in p["assets"]}
    assert assets == {"Buildings and Civil Works": "200,000,000", "Plant and Machinery": "120,000,000"}
    assert [(i["name"], i["value"]["answer"]) for i in p["controls"]] == [
        ("Fire Protection", "hydrants and portable extinguishers reported")]
    assert p["claims"][0]["claims_count"]["answer"] == "1" and p["claims"][0]["total_incurred"]["answer"] == "500,000"
    assert [i["name"] for i in p["risk_factors"]] == ["Cotton dust accumulation in carding area"]
    assert p["risks"] == [] and p["exposures"] == []
    assert p["conflicts"] == [] and p["policies"][0]["policy_id"] == biz["policy"]
    # every value carries evidence with a page
    ev = p["workforce"]["facts_used"][0]["evidence"][0]
    assert ev["page"] == 1 and ev["snippet"].startswith("The insured operates cotton spinning")


def test_relationships_and_graph(conn, biz):
    g = bi.get_business_graph(conn, biz["org"], biz["id"])
    types = {e["type"] for e in g["edges"]}
    assert {"has_location", "performs_operation", "has_control", "owns", "has_claim", "has_risk_factor",
            "has_policy", "supports_operation", "insures"} <= types
    loc = [n for n in g["nodes"] if n["type"] == "location"][0]
    assert loc["facts"][0]["evidence"][0]["document_filename"] in ("04_survey.pdf", "01_policy.pdf")
    # risk -> mitigated_by -> control is allowed when a person records it; never created by inference
    risk = kc.create_entity(conn, organization_id=biz["org"], entity_type="risk", name="Acme — fire")
    ctrl = [n for n in g["nodes"] if n["type"] == "control"][0]
    kc.create_relationship(conn, organization_id=biz["org"], from_entity_id=str(risk["id"]),
                           to_entity_id=ctrl["id"], relationship_type="mitigated_by")
    with pytest.raises(ValueError):
        kc.create_relationship(conn, organization_id=biz["org"], from_entity_id=ctrl["id"],
                               to_entity_id=str(risk["id"]), relationship_type="mitigated_by")


def test_changes_between_observations(conn, biz):
    assert bi.get_business_changes(conn, biz["org"], biz["id"])["changes"] is not None
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    ch = bi.get_business_changes(conn, biz["org"], biz["id"])
    by = {c["key"]: c for c in ch["changes"]}
    assert ch["to"] == "2026-09-15"
    assert by["business.workforce"]["kind"] == "changed" and by["business.workforce"]["old_value"] == "400"
    assert by["asset:Plant and Machinery"]["percentage_change"] == 25.0
    assert by["asset:Plant and Machinery"]["old_evidence"] and by["asset:Plant and Machinery"]["new_evidence"]
    assert by["location:7 Depot Lane, Chakan, Maharashtra"]["kind"] == "added"
    assert by["control:Security"]["kind"] == "added" and by["control:Fire Protection"]["kind"] == "changed"
    assert by["risk:Cotton dust explosion in carding area"]["kind"] == "added"
    assert by["business.survey_risk_rating"]["kind"] == "unchanged"
    assert "removed" not in {c["kind"] for c in ch["changes"]}           # absence is never removal
    assert any(n["key"].startswith("claim:") for n in ch["not_reobserved"])


def test_as_of_profile_and_history(conn, biz):
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    before = bi.get_business_profile(conn, biz["org"], biz["id"], as_of="2026-06-01")
    after = bi.get_business_profile(conn, biz["org"], biz["id"])
    assert before["workforce"]["answer"] == "400" and after["workforce"]["answer"] == "430"
    assert len(before["locations"]) == 1 and len(after["locations"]) == 2
    assert before["risks"] == [] and len(after["risks"]) == 1
    early = bi.get_business_profile(conn, biz["org"], biz["id"], as_of="2026-01-01")
    assert early["workforce"]["answer"] is None and "on or before" in early["workforce"]["reason"]


def test_same_date_contradiction(conn, biz):
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    _add(conn, biz["org"], biz["sub"], "07_addendum.pdf", SAME_DAY)
    p = bi.get_business_profile(conn, biz["org"], biz["id"])
    assert p["workforce"]["answer"] is None and p["workforce"]["review_status"] == "contradiction"
    assert [set(c["values"]) for c in p["conflicts"]] == [{"430", "445"}]
    ch = bi.get_business_changes(conn, biz["org"], biz["id"])
    assert any(c["kind"] == "conflicting" and c["key"] == "business.workforce" for c in ch["changes"])
    r = ask(conn, biz, "How many employees does the business have?")
    assert r["answer"] is None and r["review_status"] == "contradiction"


def test_correction_resolves_and_is_used(conn, biz):
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    _add(conn, biz["org"], biz["sub"], "07_addendum.pdf", SAME_DAY)
    conflict = bi.find_business_conflicts(conn, biz["org"], biz["id"])[0]
    kc.correct_fact(conn, conflict["facts"][0]["fact_id"], biz["org"], new_value="430",
                    normalized_value=430.0, reviewed_by="Asha")
    p = bi.get_business_profile(conn, biz["org"], biz["id"])
    assert p["workforce"]["answer"] == "430" and p["workforce"]["review_status"] == "verified"
    assert bi.find_business_conflicts(conn, biz["org"], biz["id"]) == []


# --- queries --------------------------------------------------------------------

def test_business_questions(conn, biz):
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    locs = ask(conn, biz, "What are the business's operating locations?")
    assert set(locs["answer"]) == {"12 Mill Road, Pune, Maharashtra", "7 Depot Lane, Chakan, Maharashtra"}
    assert locs["evidence"]
    assert ask(conn, biz, "What operations does the business perform?")["answer"] == ["cotton spinning"]
    assert "Plant and Machinery: 150,000,000" in ask(conn, biz, "What major assets are documented?")["answer"]
    assert any("Security" in a for a in ask(conn, biz, "What controls are documented?")["answer"])
    assert ask(conn, biz, "What losses have been reported?")["answer"] == ["1 claim(s) in period, total incurred 500,000"]
    assert ask(conn, biz, "Does the business have sprinklers?")["answer"][0].startswith("Fire Protection")
    risks = ask(conn, biz, "What risks are documented?")
    assert risks["answer"] == ["Cotton dust explosion in carding area"]
    changed = ask(conn, biz, "Which locations were added?")
    assert changed["answer"] == ["added location:7 Depot Lane, Chakan, Maharashtra: 7 Depot Lane, Chakan, Maharashtra"]
    assets_changed = ask(conn, biz, "Which assets changed in value?")
    assert assets_changed["answer"] == ["changed asset:Plant and Machinery: 120,000,000 -> 150,000,000 (+25.0%)"]
    prof = ask(conn, biz, "What was the business profile as of 1 June 2026?")
    assert "Workforce: 400" in prof["answer"] and not any("Depot" in a for a in prof["answer"])


def test_unsupported_risk_and_missing_evidence_abstain(conn, biz):
    r = ask(conn, biz, "Does this company have flood risk?")
    assert r["answer"] is None
    assert r["reason"] == "No documented evidence of flood risk was found in the available documents."
    assert ask(conn, biz, "What exposures are documented?")["answer"] is None
    assert ask(conn, biz, "Does the business have a backup power control?")["answer"] is None
    assert ask(conn, biz, "What risks are documented?")["answer"] is None      # survey states none


# --- API + isolation ------------------------------------------------------------

def test_api_and_tenant_isolation(conn, biz):
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    conn.commit()
    app = FastAPI(); app.include_router(api_businesses.router)
    c = TestClient(app)
    h = {"X-Organization-Id": biz["org"]}
    assert any(b["business_id"] == biz["id"] for b in c.get("/businesses", headers=h).json()["businesses"])
    for url in ("", "/profile", "/changes", "/conflicts", "/graph", "/locations", "/assets", "/operations",
                "/exposures", "/risks", "/controls", "/claims"):
        assert c.get(f"/businesses/{biz['id']}{url}", headers=h).status_code == 200, url
    assert c.get(f"/businesses/{biz['id']}/profile?as_of=junk", headers=h).status_code == 400
    assert c.get(f"/businesses/{biz['id']}/nonsense", headers=h).status_code == 404
    assert c.get(f"/businesses/{biz['id']}/locations?as_of=2026-06-01", headers=h).json()["locations"][0]["name"] \
        == "12 Mill Road, Pune, Maharashtra"
    other = str(db.create_organization(conn, "pytest-org-biz-b")["id"])
    conn.commit()
    hb = {"X-Organization-Id": other}
    for url in ("", "/profile", "/changes", "/conflicts", "/graph", "/locations", "/risks", "/claims"):
        r = c.get(f"/businesses/{biz['id']}{url}", headers=hb)
        assert r.status_code == 404 and "Mill Road" not in r.text, url
    assert c.get("/businesses", headers=hb).json() == {"businesses": []}
    q = kq.query(conn, organization_id=other, question="What are the business's operating locations?",
                 entity_id=biz["id"], use_llm=False)
    assert q["answer"] is None and not q["evidence"]


def test_renewal_question_with_one_policy_period_still_abstains(conn, biz):
    """Regression found by the Phase 1C probe in the v0.12.0 dry run: the
    business-change fallback must not answer a question about the policy."""
    _add(conn, biz["org"], biz["sub"], "06_follow_up.pdf", FOLLOW_UP)
    r = ask(conn, biz, "What changed during renewal?")
    assert r["answer"] is None and "Only one stated policy period" in r["reason"]
    generic = ask(conn, biz, "What changed since the previous period?")
    assert generic["interpretation"]["intent"] == "business_changes" and generic["answer"]
