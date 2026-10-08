"""Phase 1C (v0.11.0) — deterministic policy-structure reader and its wiring."""
import db
import knowledge.service as kc
import policy_structure as ps
import property_policy_intelligence as ppi
from tests.test_policy_period import _submission

POLICY = """[Page 1]
Property Policy
Acme Mills Pvt. Ltd.
Policy Details:
Policy Number: SYN-PROP-0099
Carrier: Meridian Shield Insurance Ltd.
Period: 01 April 2026 to 31 March 2027
Coverage:
Perils Covered: Property Damage, Business Interruption
Property Damage Sum Insured: n300,000,000
Business Interruption Sum Insured: n60,000,000
Deductible: n500,000
[Page 2]
Definitions
"Flood" means the overflow of any natural water body.
Flood sub-limit: n50,000,000
Exclusions:
1. War, terrorism and nuclear risks
Conditions
Fire protection systems must be maintained. Material changes in occupancy must be notified.
"""
SCHEDULE = """[Page 1]
Coverage Schedule
Policy Number: SYN-PROP-0099
Declared Values
Plant and Machinery: n90,000,000
Total Property Sum Insured: n300,000,000
"""
ENDORSEMENT = """[Page 1]
Policy Endorsement
Endorsement SYN-END-0099-01
Policy: SYN-PROP-0099
Effective: 19 July 2026
Change 1:
Additional named insured endorsement: facility manager added.
"""
OTHER_ENDORSEMENT = """[Page 1]
Policy Endorsement
Endorsement SYN-END-7777-01
Policy: SYN-PROP-7777
Effective: 01 May 2026
Change 1:
Flood cover removed.
"""


def test_reader_on_a_policy():
    st = ps.extract_policy_structure(POLICY)
    by = {}
    for i in st["items"]:
        by.setdefault(i["fact_type"], []).append(i)
    assert st["role"] == "policy"
    assert by["policy.number"][0]["value"] == "SYN-PROP-0099"
    assert by["policy.insurer"][0]["value"] == "Meridian Shield Insurance Ltd."
    lims = {i["coverage"]: i["normalized_value"] for i in by["coverage.limit"]}
    assert lims == {"Property Damage": 300_000_000, "Business Interruption": 60_000_000}
    assert [i["value"] for i in by["policy.condition"]] == [
        "Fire protection systems must be maintained.", "Material changes in occupancy must be notified."]
    assert by["policy.definition"][0]["value"].startswith('"Flood" means the overflow')
    assert by["policy.definition"][0]["page"] == 2
    assert by["policy.sublimit"][0]["normalized_value"] == 50_000_000
    # nothing about exclusions/sub-limits leaks into conditions
    assert all("War" not in i["value"] for i in by["policy.condition"])


def test_reader_roles_and_endorsement_metadata():
    assert ps.classify_document_role(SCHEDULE) == "schedule"
    st = ps.extract_policy_structure(ENDORSEMENT)
    assert st["role"] == "endorsement" and not st["items"]          # "Policy:" is a reference, not a number
    assert st["endorsements"] == [{"endorsement_id": "SYN-END-0099-01", "policy_ref": "SYN-PROP-0099",
                                   "effective_date": "2026-07-19", "page": 1, "snippet": "Endorsement SYN-END-0099-01"}]


def test_sublimit_is_never_a_coverage_limit():
    assert not ppi.numeric_on_limit_line("50,000,000", POLICY)
    assert ppi.numeric_on_limit_line("300,000,000", POLICY)


def test_percentage_deductible_is_not_normalized(monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": [
        {"fact_type": "policy.deductible", "value": "5% of the loss", "page": 1, "snippet": "Deductible: 5% of the loss"}]})
    out = ppi.extract_property_policy_facts(text="[Page 1]\nDeductible: 5% of the loss\n")
    assert out and out[0]["normalized_value"] is None


def test_analyze_submission_writes_structure(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": []})
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY), ("02_schedule.pdf", SCHEDULE),
                                       ("03_endorsement.pdf", ENDORSEMENT), ("09_other.pdf", OTHER_ENDORSEMENT)])
    r = ppi.analyze_submission(conn, sub, test_org)
    s = r["policy_structure"]
    assert r["facts"] == []                                          # Phase 1A list untouched
    assert s["policy_numbers"] == ["SYN-PROP-0099"]
    roles = {d["filename"]: (d["role"], d["linked_to_policy"]) for d in s["documents"]}
    assert roles["01_policy.pdf"] == ("policy", True) and roles["02_schedule.pdf"] == ("schedule", True)
    assert roles["03_endorsement.pdf"] == ("endorsement", True)
    assert roles["09_other.pdf"] == ("endorsement", False)           # different policy -> not attached
    pol = str(r["policy"]["id"])
    refs = kc.find_facts(conn, pol, test_org, fact_type="policy.endorsement_ref")
    assert [(f["value"], str(f["effective_from"]), str(f["effective_to"])) for f in refs] == [
        ("SYN-END-0099-01", "2026-07-19", "2027-03-31")]
    num = kc.find_facts(conn, pol, test_org, fact_type="policy.number", current_only=True)
    assert len(num) == 1 and len(db.get_evidence_for_fact(conn, str(num[0]["id"]), test_org)) == 2  # policy + schedule agree
    covs = s["coverages"]
    assert set(covs) == {"Property Damage", "Business Interruption"}
    lim = kc.find_facts(conn, covs["Property Damage"], test_org, fact_type="coverage.limit")
    assert lim[0]["value"] == "300,000,000" and str(lim[0]["effective_from"]) == "2026-04-01"
    # idempotent: re-running does not duplicate entities or relationships
    r2 = ppi.analyze_submission(conn, sub, test_org)
    assert r2["policy_structure"]["coverages"] == covs
    assert len(kc.find_relationships(conn, test_org, pol, "provides", "from")) == 2


def test_conflicting_policy_numbers_are_flagged(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": []})
    bad_schedule = SCHEDULE.replace("SYN-PROP-0099", "SYN-PROP-0098")
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY), ("02_schedule.pdf", bad_schedule)])
    r = ppi.analyze_submission(conn, sub, test_org)
    roles = {d["filename"]: d for d in r["policy_structure"]["documents"]}
    assert roles["02_schedule.pdf"]["linked_to_policy"] is False
    assert "different policy" in roles["02_schedule.pdf"]["note"]
