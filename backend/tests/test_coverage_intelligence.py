"""Phase 1E (v0.13.0) — Risk → Coverage Intelligence: mappings, limit comparison,
sublimits, deductibles, exclusions, conditions, endorsements, temporal coverage,
policy change → exposure impact, conflicts, findings, evidence, human review,
questions, graph, API and tenant isolation."""
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import coverage_intelligence as ci
import db
import knowledge.service as kc
import property_policy_intelligence as ppi
from api import businesses as api_businesses, coverage as api_coverage, knowledge as api_knowledge
from knowledge import query as kq
from tests.test_policy_intelligence import _model_stub
from tests.test_policy_period import _submission

POLICY = """[Page 1]
Property Policy
Riverbend Textiles Pvt. Ltd.
Policy Details:
Policy Number: SYN-PROP-0500
Carrier: Meridian Shield Insurance Ltd.
Period: 01 April 2026 to 31 March 2027
Coverage:
Perils Covered: Property Damage, Business Interruption
Property Damage Sum Insured: n300,000,000
Business Interruption Sum Insured: n60,000,000
Property Location: 3 River Road, Surat, Gujarat
[Page 2]
Flood sub-limit: n50,000,000
Exclusions:
1. War, terrorism and nuclear risks
2. Flood damage to stock held in open yards
Conditions
Fire protection systems must be maintained. Material changes in occupancy or operations must be notified.
"""
RENEWAL = (POLICY.replace("01 April 2026 to 31 March 2027", "01 April 2027 to 31 March 2028")
           .replace("Perils Covered: Property Damage, Business Interruption",
                    "Perils Covered: Property Damage, Business Interruption, Machinery Breakdown")
           .replace("Property Damage Sum Insured: n300,000,000", "Property Damage Sum Insured: n250,000,000"))
SCHEDULE = """[Page 1]
Coverage Schedule
Policy Number: SYN-PROP-0500
Insured: Riverbend Textiles Pvt. Ltd.
Declared Values
Buildings and Civil Works: 200,000,000
Plant and Machinery: 70,000,000
Stock: 20,000,000
Total Property Sum Insured: 290,000,000
"""
ENDORSEMENT = """[Page 1]
Policy Endorsement
Endorsement SYN-END-0500-01
Policy: SYN-PROP-0500
Effective: 19 July 2026
Change 1:
Plant and machinery values reviewed. Revised machinery declared value: n70,000,000.
"""
SURVEY = """[Page 1]
Property Risk Survey
Location: 3 River Road, Surat, Gujarat
Survey date: 10 March 2026
Operations
The insured operates cotton weaving. Estimated workforce: 300.
Observations
Overall risk rating: Moderate.
Fire protection: hydrants and portable extinguishers reported.
"""
FOLLOW_UP = """[Page 1]
Property Risk Survey (Follow-up)
Location: 3 River Road, Surat, Gujarat
Survey date: 15 September 2026
Operations
The insured operates cotton weaving and dyeing. Estimated workforce: 320.
Observations
Fire protection: hydrants, extinguishers and automatic sprinklers reported.
Declared Values
Plant and Machinery: 120,000,000
Risk Notes
Identified risk: Flood inundation of the lower yard.
Identified risk: Machinery breakdown of the main loom drive.
Identified risk: Cyber attack on plant controls.
"""


@pytest.fixture
def w(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", _model_stub)
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY), ("01b_renewal.pdf", RENEWAL),
                                       ("02_schedule.pdf", SCHEDULE), ("03_endorsement.pdf", ENDORSEMENT),
                                       ("04_survey.pdf", SURVEY), ("06_followup.pdf", FOLLOW_UP)])
    r = ppi.analyze_submission(conn, sub, test_org)
    return {"org": test_org, "sub": sub, "id": str(r["business"]["id"]), "policy": str(r["policy"]["id"])}


def _map(conn, w, as_of):
    return ci.get_coverage_map(conn, w["org"], w["id"], as_of=as_of)


def _rows(m, kind=None):
    return [r for r in m["mappings"] if kind is None or r["unit"]["kind"] == kind]


def _findings(conn, w, as_of):
    return ci.get_coverage_findings(conn, w["org"], w["id"], as_of=as_of)["findings"]


def ask(conn, w, q, org=None):
    return kq.query(conn, organization_id=org or w["org"], question=q, entity_id=w["id"], use_llm=False)


ALL_TEXT = re.compile(r"under-?insured|over-?insured|not insured|not covered|no insurance|is covered|"
                      r"breach(ed)? of|violat|should (buy|purchase)", re.I)


# --- exposure -> coverage ---------------------------------------------------------

def test_property_exposure_maps_to_property_coverage_with_explicit_link(conn, w):
    m = _map(conn, w, "2026-08-01")
    (r,) = _rows(m, "property_exposure")
    assert r["coverage"]["name"] == "Property Damage" and r["policy"]["policy_number"] == "SYN-PROP-0500"
    assert r["mapping_type"] == "EXPLICIT_POLICY_LINK"          # the schedule names the policy number
    assert {c["asset"] for c in r["unit"]["components"]} == {"Buildings and Civil Works", "Plant and Machinery", "Stock"}
    assert r["unit"]["amount"] == 290_000_000
    roles = {e["role"] for e in r["evidence"]}
    assert roles == {"exposure", "coverage_limit"} and all(e["page"] and e["document_filename"] for e in r["evidence"])
    assert m["coverages_without_documented_exposure"][0]["coverage"] == "Business Interruption"


def test_limit_comparison_states_and_wording(conn, w):
    before = _rows(_map(conn, w, "2026-08-01"), "property_exposure")[0]["limit_comparison"]
    after = _rows(_map(conn, w, "2026-09-20"), "property_exposure")[0]["limit_comparison"]
    assert before["state"] == "WITHIN_DOCUMENTED_LIMIT" and before["limit_amount"] == 300_000_000
    assert after["state"] == "EXCEEDS_DOCUMENTED_LIMIT" and after["exposure_amount"] == 340_000_000
    assert after["statement"] == ("The documented exposure amount (340,000,000) is greater than the stated "
                                  "Property Damage limit (300,000,000).")
    f = [x for x in _findings(conn, w, "2026-09-20") if x["type"] == "EXPOSURE_EXCEEDS_LIMIT"]
    assert len(f) == 1 and f[0]["statement"].startswith("Potential limit mismatch")
    early = [x["type"] for x in _findings(conn, w, "2026-08-01")]
    assert "LIMIT_EXCEEDS_EXPOSURE" in early and "EXPOSURE_EXCEEDS_LIMIT" not in early


def test_limit_unknown_is_missing_evidence_not_a_conclusion(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", _model_stub)
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY.replace("Property Damage Sum Insured: n300,000,000\n", "")),
                                       ("02_schedule.pdf", SCHEDULE)])
    bid = str(ppi.analyze_submission(conn, sub, test_org)["business"]["id"])
    m = ci.get_coverage_map(conn, test_org, bid, as_of="2026-08-01")
    (r,) = _rows(m, "property_exposure")
    assert r["limit_comparison"]["state"] == "LIMIT_UNKNOWN" and r["status"] == "NEEDS_REVIEW"
    f = ci.get_coverage_findings(conn, test_org, bid, as_of="2026-08-01")["findings"]
    assert [x["type"] for x in f if x["type"] == "MISSING_EVIDENCE"] == ["MISSING_EVIDENCE"]


# --- risk -> coverage --------------------------------------------------------------

def test_uncertain_and_unmapped_risks(conn, w):
    m = _map(conn, w, "2026-09-20")
    risks = {r["unit"]["name"]: r for r in _rows(m, "risk")}
    flood = risks["Flood inundation of the lower yard"]
    assert flood["status"] == "UNCERTAIN" and flood["mapping_type"] == "UNCERTAIN"
    assert flood["coverage"]["name"] == "Property Damage" and "review required" in flood["rationale"]
    mb = risks["Machinery breakdown of the main loom drive"]
    assert mb["status"] == "UNCERTAIN"                         # no Machinery Breakdown coverage in 2026
    cyber = risks["Cyber attack on plant controls"]
    assert cyber["status"] == "UNMAPPED" and cyber["coverage"] is None
    assert cyber["candidates_considered"] == ["Property Damage", "Business Interruption"]
    assert cyber["rationale"].startswith("No relevant coverage was identified in the available policy documents")
    types = {(x["type"], x["subject"]) for x in _findings(conn, w, "2026-09-20")}
    assert ("UNMAPPED_RISK", "Cyber attack on plant controls") in types
    assert ("UNCERTAIN_MAPPING", "Flood inundation of the lower yard") in types


def test_direct_mapping_after_renewal_uses_the_period_in_force(conn, w):
    m26 = _map(conn, w, "2026-10-01")
    m27 = _map(conn, w, "2027-06-01")
    assert [p["periods"] for p in m26["policies_in_force"]] == [[{"from": "2026-04-01", "to": "2027-03-31"},
                                                                 {"from": "2027-04-01", "to": "2028-03-31"}]]
    mb27 = [r for r in _rows(m27, "risk") if r["unit"]["name"].startswith("Machinery breakdown")]
    assert [(r["coverage"]["name"], r["mapping_type"], r["status"]) for r in mb27] == \
        [("Machinery Breakdown", "DIRECT", "MAPPED")]
    p27 = _rows(m27, "property_exposure")[0]["limit_comparison"]
    p26 = _rows(m26, "property_exposure")[0]["limit_comparison"]
    assert p26["limit_amount"] == 300_000_000 and p27["limit_amount"] == 250_000_000


def test_no_policy_in_force_before_the_period(conn, w):
    m = _map(conn, w, "2026-03-15")
    assert m["policies_in_force"] == [] and m["mappings"] == []
    r = kq.query(conn, organization_id=w["org"], question="What was the coverage state as of 15 March 2026?",
                 entity_id=w["id"], use_llm=False)
    assert r["answer"] is None and "No policy period" in r["reason"]


# --- exclusions, sublimits, conditions, deductibles, endorsements ------------------

def test_exclusion_review_never_says_not_insured(conn, w):
    m = _map(conn, w, "2026-09-20")
    prop = _rows(m, "property_exposure")[0]
    assert [(x["text"], x["matched"]) for x in prop["exclusions"]] == [
        ("Flood damage to stock held in open yards", ["Stock"])]
    assert prop["status"] == "PARTIALLY_MAPPED"
    f = [x for x in _findings(conn, w, "2026-09-20") if x["type"] == "EXCLUSION_REVIEW"]
    subjects = sorted(x["subject"] for x in f)
    assert subjects == ["Flood inundation of the lower yard", "Stock"]
    for x in f:
        assert x["statement"].startswith("Exclusion identified in the available policy documents")
        assert not ALL_TEXT.search(x["statement"])
        assert {e["role"] for e in x["evidence"]} >= {"exclusion"}


def test_sublimit_surfaced_for_the_matching_risk(conn, w):
    m = _map(conn, w, "2026-09-20")
    flood = [r for r in _rows(m, "risk") if r["unit"]["name"].startswith("Flood")][0]
    assert [s["text"] for s in flood["sublimits"]] == ["Flood sub-limit: n50,000,000"]
    f = [x for x in _findings(conn, w, "2026-09-20") if x["type"] == "SUBLIMIT_REVIEW"]
    assert len(f) == 1 and f[0]["statement"].startswith("Relevant sublimit identified")
    m8 = _findings(conn, w, "2026-08-01")             # no flood risk yet: applicability not established
    s8 = [x for x in m8 if x["type"] == "SUBLIMIT_REVIEW"]
    assert len(s8) == 1 and "not established" in s8[0]["statement"]


def test_condition_review_is_not_a_breach(conn, w):
    f = [x for x in _findings(conn, w, None) if x["type"] == "CONDITION_REVIEW"]
    fire = [x for x in f if x["subject"] == "Fire protection systems must be maintained."][0]
    assert "a related control is documented" in fire["statement"]
    assert "not a finding that the condition is breached" in fire["statement"]
    assert {e["role"] for e in fire["evidence"]} == {"condition", "control"}
    ops = [x for x in f if "operations" in x["subject"]][0]
    assert "operation changes" in ops["statement"] and "not evidenced" in ops["statement"]


def test_condition_without_related_control_is_missing_evidence_not_violation(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", _model_stub)
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY), ("02_schedule.pdf", SCHEDULE)])
    bid = str(ppi.analyze_submission(conn, sub, test_org)["business"]["id"])
    f = [x for x in ci.get_coverage_findings(conn, test_org, bid, as_of="2026-08-01")["findings"]
         if x["type"] == "CONDITION_REVIEW"]
    assert len(f) == 1 and "no related control is documented" in f[0]["statement"]
    assert "Missing evidence is not a breach" in f[0]["statement"]


def test_deductibles_surfaced_and_percentage_needs_review(conn, w):
    kc.reconcile_fact(conn, organization_id=w["org"], entity_id=w["policy"], fact_type="policy.deductible",
                      value="5% of loss, minimum 500,000", normalized_value=None,
                      confidence={"value": 1.0, "type": "deterministic", "method": "t", "note": None},
                      produced_by="t", document_id=_doc(conn, w, "01_policy.pdf"), evidence_page=1,
                      evidence_snippet="Property Damage Sum Insured: n300,000,000", evidence_source="text_layer",
                      effective_from=__import__("datetime").date(2026, 4, 1),
                      effective_to=__import__("datetime").date(2027, 3, 31))
    m = _map(conn, w, "2026-08-01")
    assert m["deductibles"][0]["percentage"] is True
    assert m["deductibles"][0]["applicability"] == "stated for the policy, not for a named coverage"
    f = [x for x in _findings(conn, w, "2026-08-01") if x["type"] == "DEDUCTIBLE_REVIEW"]
    assert len(f) == 1 and "percentage-based" in f[0]["statement"]


def _doc(conn, w, name):
    return next(str(d["id"]) for d in db.list_submission_documents(conn, w["sub"], w["org"]) if d["filename"] == name)


def test_endorsement_connected_with_effective_date(conn, w):
    m = _map(conn, w, "2026-08-01")
    ref = [e for e in m["endorsements"] if e.get("endorsement_id")]
    assert ref[0]["endorsement_id"] == "SYN-END-0500-01" and ref[0]["effective_from"] == "2026-07-19"
    assert ref[0]["in_effect_on_date"] is True
    early = [e for e in _map(conn, w, "2026-06-01")["endorsements"] if e.get("endorsement_id")]
    assert early[0]["in_effect_on_date"] is False


# --- conflicts, changes ---------------------------------------------------------------

def test_coverage_conflict_is_flagged_never_resolved(conn, w, make_document):
    import policy_intelligence as pi
    cov = [c for c in pi.get_policy(conn, w["org"], w["policy"], as_of="2026-08-01")["coverages"]
           if c["name"] == "Property Damage"][0]
    doc = make_document(w["org"], filename="quote.pdf",
                        extracted_text="[Page 1]\nPeriod: 01 April 2026 to 31 March 2027\nProperty Damage Sum Insured: 280,000,000\n")
    import datetime as dt
    kc.reconcile_fact(conn, organization_id=w["org"], entity_id=cov["coverage_id"], fact_type="coverage.limit",
                      value="280,000,000", normalized_value=2.8e8,
                      confidence={"value": 1.0, "type": "deterministic", "method": "t", "note": None},
                      produced_by="t", document_id=doc, evidence_page=1,
                      evidence_snippet="Property Damage Sum Insured: 280,000,000", evidence_source="text_layer",
                      effective_from=dt.date(2026, 4, 1), effective_to=dt.date(2027, 3, 31))
    m = _map(conn, w, "2026-08-01")
    r = _rows(m, "property_exposure")[0]
    assert r["status"] == "CONFLICTING" and r["limit_comparison"]["state"] == "NEEDS_REVIEW"
    assert sorted(r["limit_comparison"]["limit_conflict"]) == ["280,000,000", "300,000,000"]
    c = [x for x in _findings(conn, w, "2026-08-01") if x["type"] == "COVERAGE_CONFLICT"]
    assert len(c) == 1 and "Property Damage limit" in c[0]["statement"] and len(c[0]["evidence"]) == 2
    assert ci.coverage_conflicts(conn, w["org"], w["id"], as_of="2026-08-01")["conflicts"][0]["type"] == "COVERAGE_CONFLICT"


def test_coverage_status_change_detected(conn, w):
    ch = ci.get_coverage_changes(conn, w["org"], w["id"], from_date="2026-08-01", to_date="2026-09-20")
    prop = [c for c in ch["changes"] if c["unit"].startswith("Insured property")][0]
    assert prop["kind"] == "status_changed"
    assert (prop["old_limit_state"], prop["new_limit_state"]) == ("WITHIN_DOCUMENTED_LIMIT", "EXCEEDS_DOCUMENTED_LIMIT")
    assert {c["kind"] for c in ch["changes"]} <= {"status_changed", "newly_mapped", "unchanged",
                                                  "not_in_effect_on_later_date"}
    assert any(c["kind"] == "newly_mapped" and c["unit"].startswith("Flood") for c in ch["changes"])


def test_policy_change_with_related_exposure_needs_review(conn, w):
    ch = ci.get_coverage_changes(conn, w["org"], w["id"], from_date="2026-10-01", to_date="2027-06-01")
    pd = [c for c in ch["policy_changes"] if c.get("change_type") == "coverage_limit_decrease"
          and c["coverage"] == "Property Damage"][0]
    assert (pd["old_value"], pd["new_value"]) == ("300,000,000", "250,000,000")
    assert pd["related_exposure"] == 340_000_000 and pd["status"] == "NEEDS_REVIEW" and pd["evidence"]
    assert any(c.get("change_type") == "coverage_added" and c["new_value"] == "Machinery Breakdown"
               for c in ch["policy_changes"])
    assert all("removed" not in c["kind"] for c in ch["changes"])


# --- evidence, review -------------------------------------------------------------------

def test_every_finding_has_document_and_page_evidence(conn, w):
    for x in _findings(conn, w, "2026-09-20"):
        assert x["evidence"], x["type"]
        assert all(e["document_filename"] and e["page"] for e in x["evidence"]), x["type"]
        assert x["conclusion"].startswith("For broker review") and not ALL_TEXT.search(x["statement"])


def test_human_review_and_stale_review(conn, w):
    f = [x for x in _findings(conn, w, None) if x["type"] == "EXPOSURE_EXCEEDS_LIMIT"][0]
    assert f["review_status"] == "OPEN"
    out = ci.review(conn, w["org"], w["id"], "finding", f["finding_id"], "ACCEPTED", "asha@broker", "Raise at renewal")
    assert out["decision"] == "ACCEPTED" and out["evidence"]
    f2 = [x for x in _findings(conn, w, None) if x["finding_id"] == f["finding_id"]][0]
    assert f2["review_status"] == "ACCEPTED" and f2["review"]["reviewer"] == "asha@broker"
    with pytest.raises(ValueError):
        ci.review(conn, w["org"], w["id"], "finding", f["finding_id"], "APPROVED", "x")
    assert ci.review(conn, w["org"], w["id"], "finding", "00000000-0000-0000-0000-000000000000", "ACCEPTED", "x") is None
    # New evidence (a later declared value) changes the finding: the old review no longer applies.
    _add_doc(conn, w, "07_more.pdf", "[Page 1]\nProperty Risk Survey\nSurvey date: 20 September 2026\n"
                                     "Declared Values\nStock: 45,000,000\n")
    f3 = [x for x in _findings(conn, w, None) if x["finding_id"] == f["finding_id"]][0]
    assert f3["review_status"] == "OPEN" and f3["review_stale"] is True
    m = ci.coverage_map_view(conn, w["org"], w["id"])
    mid = _rows(m, "property_exposure")[0]["mapping_id"]
    assert ci.review(conn, w["org"], w["id"], "mapping", mid, "REVIEWED", "asha@broker")["decision"] == "REVIEWED"
    assert _rows(ci.coverage_map_view(conn, w["org"], w["id"]), "property_exposure")[0]["review_status"] == "REVIEWED"


def _add_doc(conn, w, name, text):
    row = db.create_document(conn, organization_id=w["org"], carrier_id=None, filename=name, file_type="pdf",
                             size_bytes=len(text), pages=1, chars=len(text), status="parsed", error_message=None,
                             extracted_text=text)
    db.set_document_submission(conn, str(row["id"]), w["sub"])
    ppi.apply_business_documents(conn, w["sub"], w["org"], [str(row["id"])])


# --- questions ----------------------------------------------------------------------------

def test_coverage_questions(conn, w):
    expect = {
        "What risks have relevant coverage?": "cov_mapped",
        "What risks have no clearly mapped coverage?": "cov_unmapped",
        "Which exposures exceed documented policy limits?": "cov_limits",
        "What exclusions affect documented risks?": "cov_exclusions",
        "What conditions require review?": "cov_conditions",
        "Which risks changed coverage status?": "cov_status_changes",
        "Which policies protect this business exposure?": "cov_mapped",
        "Show the evidence for this coverage mapping.": "cov_mapped",
        "Which findings require broker review?": "cov_findings",
        "What coverage conflicts exist?": "cov_conflicts",
        "What risks are currently unmapped?": "cov_unmapped",
        "What was the coverage state as of 1 June 2026?": "cov_state",
    }
    for q, intent in expect.items():
        r = ask(conn, w, q)
        assert r["interpretation"]["intent"] == intent, q
        assert r["authoritative"] is False and r["needs_review"] is True
        if r["answer"] is not None:
            assert r["evidence"], q
            assert not any(ALL_TEXT.search(a) for a in r["answer"]), q
    lim = ask(conn, w, "Which exposures exceed documented policy limits?")
    assert lim["answer"][0].startswith("EXCEEDS_DOCUMENTED_LIMIT: The documented exposure amount (340,000,000)")
    un = ask(conn, w, "What risks have no clearly mapped coverage?")
    assert any(a.startswith("Cyber attack on plant controls → no coverage identified: UNMAPPED") for a in un["answer"])
    state = ask(conn, w, "What was the coverage state as of 1 June 2026?")
    assert state["answer"][0] == "Policies in force on 2026-06-01: SYN-PROP-0500."
    # Existing Phase 1B/1C/1D questions keep their intents.
    for q, intent in {"What is the deductible?": "fact", "What coverage changed during renewal?": "fact",
                      "Which coverage has a contradiction?": "conflicts", "What risks are documented?": "business_risks",
                      "What exposures are documented?": "business_exposures", "What conditions apply?": "fact",
                      "Is flood excluded for Orion Plastics?": "fact", "What changed during renewal?": "policy_changes",
                      "Which coverage has the highest limit?": "coverage_rank"}.items():
        assert kq.interpret(q)["intent"] == intent, q


def test_advice_questions_are_declined(conn, w):
    for q in ("Is this company adequately insured?", "Should the company buy more insurance?",
              "Is the business underinsured?", "Does the business have enough insurance?"):
        r = ask(conn, w, q)
        assert r["interpretation"]["intent"] == "cov_advice", q
        assert r["answer"] is None and r["answer_type"] == "declined_judgment"
        assert r["reason"].startswith("Aurevia does not decide whether a business is adequately insured")
        assert "Potential limit mismatch" in r["reason"]


# --- graph, API, tenants -------------------------------------------------------------------

def test_coverage_graph_marks_derived_edges(conn, w):
    import business_intelligence as bi
    g = ci.get_coverage_graph(conn, w["org"], w["id"], as_of="2026-09-20")
    base = bi.get_business_graph(conn, w["org"], w["id"])
    assert len([e for e in g["edges"] if not e["derived"]]) == len(base["edges"])
    mapped = [e for e in g["edges"] if e["type"] == "mapped_to_coverage"]
    nodes = {n["id"]: n for n in g["nodes"]}
    assert {nodes[e["from"]]["type"] for e in mapped} == {"asset", "risk"}
    assert all(e["derived"] and e["evidence"] for e in mapped)
    assert any(e["type"] == "potentially_affected_by_exclusion" for e in g["edges"])
    assert any(e["type"] == "condition_refers_to_control" for e in g["edges"])


def test_api_and_tenant_isolation(conn, w):
    conn.commit()
    from tests.auth_helpers import signed_in
    routers = [api_coverage.router, api_businesses.router, api_knowledge.router]
    c = signed_in(routers, conn, w["org"])
    h = {"X-Organization-Id": w["org"]}
    urls = ["coverage-map", "coverage-map?as_of=2026-09-20", "coverage-findings", "unmapped-risks",
            "coverage-conflicts", "coverage-changes", "coverage-changes?from_date=2026-08-01&to_date=2026-09-20",
            "coverage-audit", "coverage-graph"]
    for u in urls:
        assert c.get(f"/businesses/{w['id']}/{u}", headers=h).status_code == 200, u
    assert c.get(f"/businesses/{w['id']}/coverage-map?as_of=junk", headers=h).status_code == 400
    audit = c.get(f"/businesses/{w['id']}/coverage-audit", headers=h).json()
    assert {"risks", "exposures", "mapped", "unmapped", "limit_comparisons", "findings", "changes",
            "review_counts"} <= set(audit)
    assert "score" not in str(sorted(audit)).lower()
    other = str(db.create_organization(conn, "pytest-cov-b")["id"])
    conn.commit()
    cb = signed_in(routers, conn, other)
    for u in urls:
        r = cb.get(f"/businesses/{w['id']}/{u}")
        assert r.status_code == 404 and "SYN-PROP-0500" not in r.text, u
    fid = audit["findings"][0]["finding_id"]
    body = {"business_id": w["id"], "decision": "REVIEWED", "reviewer": "x"}
    assert cb.post(f"/coverage-findings/{fid}/review", json=body).status_code == 404
    assert c.post(f"/coverage-findings/{fid}/review", json=dict(body, decision="nope"), headers=h).status_code == 400
    ok = c.post(f"/coverage-findings/{fid}/review", json=body, headers=h)
    assert ok.status_code == 200 and ok.json()["decision"] == "REVIEWED"
    assert ok.json()["reviewer"].endswith(f"<{c.user['email']}>")     # identity from the session, not "x"
    q = ask(conn, w, "Which findings require broker review?", org=other)
    assert q["answer"] is None and not q["evidence"]


def test_core_fix_earlier_period_disagreement_is_flagged_after_renewal(conn, w, make_document):
    """Knowledge Core regression found by Phase 1E: a 2026 value read AFTER the 2027
    renewal used to be stored as history and silently answered for 2026."""
    import datetime as dt
    import policy_intelligence as pi
    cov = [c for c in pi.get_policy(conn, w["org"], w["policy"], as_of="2026-08-01")["coverages"]
           if c["name"] == "Business Interruption"][0]
    doc = make_document(w["org"], filename="bi_quote.pdf", extracted_text="[Page 1]\nBusiness Interruption Sum Insured: 75,000,000\n")
    kw = dict(organization_id=w["org"], entity_id=cov["coverage_id"], fact_type="coverage.limit",
              confidence={"value": 1.0, "type": "deterministic", "method": "t", "note": None}, produced_by="t",
              document_id=doc, evidence_page=1, evidence_source="text_layer",
              effective_from=dt.date(2026, 4, 1), effective_to=dt.date(2027, 3, 31))
    same = kc.reconcile_fact(conn, value="60,000,000", normalized_value=6e7,
                             evidence_snippet="Business Interruption Sum Insured: 75,000,000", **kw)
    assert same["_contradiction"] is None                      # agreeing restatement merges
    out = kc.reconcile_fact(conn, value="75,000,000", normalized_value=7.5e7,
                            evidence_snippet="Business Interruption Sum Insured: 75,000,000", **kw)
    assert out["_contradiction"]["other_value"] == "60,000,000"
    v26 = [c for c in pi.get_policy(conn, w["org"], w["policy"], as_of="2026-08-01")["coverages"]
           if c["name"] == "Business Interruption"][0]["limit"]
    assert v26["answer"] is None and v26["review_status"] == "contradiction"
    v27 = [c for c in pi.get_policy(conn, w["org"], w["policy"], as_of="2027-06-01")["coverages"]
           if c["name"] == "Business Interruption"][0]["limit"]
    assert v27["answer"] == "60,000,000"                         # the later period is untouched
