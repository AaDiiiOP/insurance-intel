"""Phase 1C (v0.11.0) — Policy Intelligence service, comparison, history, API, isolation."""
import pytest
from tests.auth_helpers import signed_in
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
import knowledge.service as kc
import policy_intelligence as pi
import property_policy_intelligence as ppi
from api import policies as api_policies
from tests.test_policy_period import _submission
from tests.test_policy_structure import POLICY, SCHEDULE, ENDORSEMENT
from tests.test_knowledge_query import world  # noqa: F401

POLICY_2027 = (POLICY.replace("SYN-PROP-0099", "SYN-PROP-0199")
               .replace("01 April 2026 to 31 March 2027", "01 April 2027 to 31 March 2028")
               .replace("Meridian Shield Insurance Ltd.", "BluePeak Risk Underwriters Ltd.")
               .replace("Perils Covered: Property Damage, Business Interruption",
                        "Perils Covered: Property Damage, Business Interruption, Machinery Breakdown")
               .replace("Property Damage Sum Insured: n300,000,000", "Property Damage Sum Insured: n360,000,000")
               .replace("1. War, terrorism and nuclear risks", "1. War, terrorism and nuclear risks\n2. Cyber attack")
               .replace(" Material changes in occupancy must be notified.", ""))


def _model_stub(prompt, *a, **k):
    """Stands in for the model: returns the coverages and exclusions literally
    written in the chunk it is given (so the real validation rules still run)."""
    import re
    facts = []
    for line in prompt.splitlines():
        m = re.match(r"\s*Perils Covered:\s*(.+)", line)
        if m:
            for cov in [c.strip() for c in m.group(1).split(",")]:
                facts.append({"fact_type": "policy.coverage", "value": cov, "page": 1, "snippet": line.strip()})
        m = re.match(r"\s*\d\.\s*(.+)", line)
        if m:
            facts.append({"fact_type": "policy.exclusion", "value": m.group(1).strip(), "page": 2,
                          "snippet": line.strip()})
    return {"facts": facts}


@pytest.fixture
def analysed(conn, test_org, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", _model_stub)
    sub = _submission(conn, test_org, [("01_policy.pdf", POLICY), ("02_schedule.pdf", SCHEDULE),
                                       ("03_endorsement.pdf", ENDORSEMENT)])
    r = ppi.analyze_submission(conn, sub, test_org)
    return test_org, str(r["policy"]["id"])


def test_get_policy_structured_view(conn, analysed):
    org, pid = analysed
    v = pi.get_policy(conn, org, pid, as_of="2026-06-01")
    assert v["policy_number"]["answer"] == "SYN-PROP-0099"
    assert v["insurer"]["answer"] == "Meridian Shield Insurance Ltd."
    assert v["period"]["status"] == "current" and v["period"]["in_force"] == [{"from": "2026-04-01", "to": "2027-03-31"}]
    lims = {c["name"]: c["limit"]["answer"] for c in v["coverages"]}
    assert lims == {"Property Damage": "300,000,000", "Business Interruption": "60,000,000"}
    assert all(c["limit"]["facts_used"][0]["evidence"][0]["page"] == 1 for c in v["coverages"])
    assert len(v["conditions"]["answer"]) == 2
    assert v["definitions"]["answer"][0].startswith('"Flood" means')
    assert v["definitions"]["facts_used"][0]["evidence"][0]["page"] == 2
    assert v["sublimits"]["answer"] == ["Flood sub-limit: n50,000,000"]
    assert v["endorsement_refs"][0]["value"] == "SYN-END-0099-01"
    assert v["endorsement_refs"][0]["in_effect_on_date"] is False                 # effective 19 July
    assert pi.get_policy(conn, org, pid, as_of="2026-08-01")["endorsement_refs"][0]["in_effect_on_date"] is True
    assert {d["role"] for d in v["documents"]} == {"policy", "schedule", "endorsement"}
    assert v["conflicts"] == []
    assert pi.get_policy(conn, org, pid, as_of="2027-06-01")["period"]["status"] == "expired"
    assert pi.get_policy(conn, org, pid, as_of="2026-01-01")["period"]["status"] == "future"


def test_compare_two_policies_with_evidence(conn, analysed, monkeypatch):
    org, old_pid = analysed
    sub = _submission(conn, org, [("01_policy_2027.pdf", POLICY_2027)])
    new_pid = str(ppi.analyze_submission(conn, sub, org)["policy"]["id"])
    out = pi.compare_policies(conn, org, old_pid, new_pid)
    by = {}
    for c in out["changes"]:
        by.setdefault(c["change_type"], []).append(c)
    pd = [c for c in by["coverage_limit_increase"] if c["coverage"] == "Property Damage"][0]
    assert pd["old_value"] == "300,000,000" and pd["new_value"] == "360,000,000"
    assert pd["absolute_change"] == 60_000_000 and pd["percentage_change"] == 20.0
    assert pd["old_evidence"][0]["snippet"].startswith("Property Damage Sum Insured: n300")
    assert pd["new_evidence"][0]["snippet"].startswith("Property Damage Sum Insured: n360")
    assert [c["new_value"] for c in by["coverage_added"]] == ["Machinery Breakdown"]
    assert [c["new_value"] for c in by["exclusion_added"]] == ["Cyber attack"]
    assert by["coverage_added"][0]["new_evidence"][0]["snippet"].startswith("Perils Covered:")
    assert by["condition_removed"][0]["old_value"] == "Material changes in occupancy must be notified."
    assert by["insurer_changed"][0]["new_value"] == "BluePeak Risk Underwriters Ltd."
    assert by["policy_number_changed"][0]["old_value"] == "SYN-PROP-0099"
    assert out["changes"][0]["change_type"] == "policy_period_changed"
    assert "coverage_removed" not in by and "exclusion_removed" not in by


def test_compare_previous_vs_current_period_of_one_policy(conn, world):
    out = pi.compare_policies(conn, world["org"], str(world["policy"]["id"]), as_of="2026-10-04")
    by = {c["change_type"]: c for c in out["changes"]}
    lim = by["limit_increase"]
    assert lim["old_value"] == "150,000,000" and lim["new_value"] == "180,000,000"
    assert lim["percentage_change"] == 20.0 and lim["old_evidence"] and lim["new_evidence"]
    assert "deductible_unchanged" in by
    assert by["coverage_added"]["new_value"] == "Business Interruption"
    assert by["policy_period_changed"]["old_value"] == "2025-04-01 to 2026-03-31"


def test_one_period_is_insufficient(conn, analysed):
    org, pid = analysed
    out = pi.compare_policies(conn, org, pid)
    assert out["status"] == "insufficient" and out["changes"] == []


def test_conflicts_surface_both_values(conn, analysed, make_document):
    org, pid = analysed
    cov = [c for c in pi.get_policy(conn, org, pid)["coverages"] if c["name"] == "Property Damage"][0]
    doc = make_document(org, filename="quote.pdf", extracted_text="[Page 1]\nProperty Damage Sum Insured: 280,000,000\n")
    kc.reconcile_fact(conn, organization_id=org, entity_id=cov["coverage_id"], fact_type="coverage.limit",
                      value="280,000,000", normalized_value=2.8e8,
                      confidence={"value": 1.0, "type": "deterministic", "method": "t", "note": None},
                      produced_by="t", document_id=doc, evidence_page=1,
                      evidence_snippet="Property Damage Sum Insured: 280,000,000", evidence_source="text_layer",
                      effective_from="2026-04-01", effective_to="2027-03-31")
    conflicts = pi.find_policy_conflicts(conn, org, pid)
    assert len(conflicts) == 1 and conflicts[0]["coverage"] == "Property Damage"
    assert set(conflicts[0]["values"]) == {"300,000,000", "280,000,000"}
    assert all(f["evidence"] for f in conflicts[0]["facts"])
    view = pi.get_policy(conn, org, pid, as_of="2026-06-01")
    pdv = [c for c in view["coverages"] if c["name"] == "Property Damage"][0]["limit"]
    assert pdv["answer"] is None and pdv["review_status"] == "contradiction"


def test_history_labels(conn, world):
    org = world["org"]
    kc.correct_fact(conn, str(world["facts"]["limit26"]["id"]), org, new_value="185,000,000",
                    normalized_value=1.85e8, reviewed_by="Asha")
    h = pi.get_policy_history(conn, org, str(world["policy"]["id"]))
    labels = {(p["period"], f["value"], f["version"]) for p in h["periods"] for f in p["facts"]
              if f["fact_type"] == "policy.coverage_limit"}
    assert ("2026-04-01 to 2027-03-31", "180,000,000", "superseded") in labels
    assert ("2026-04-01 to 2027-03-31", "185,000,000", "amended") in labels
    assert ("2025-04-01 to 2026-03-31", "150,000,000", "historical") in labels


def test_policy_api_and_tenant_isolation(conn, analysed):
    org, pid = analysed
    conn.commit()
    c = signed_in([api_policies.router], conn, org)
    h = {"X-Organization-Id": org}
    assert c.get(f"/policies/{pid}?as_of=2026-06-01", headers=h).json()["policy_number"]["answer"] == "SYN-PROP-0099"
    assert any(p["policy_id"] == pid for p in c.get("/policies", headers=h).json()["policies"])
    assert c.get(f"/policies/{pid}/history", headers=h).status_code == 200
    assert c.get(f"/policies/{pid}/conflicts", headers=h).json() == {"conflicts": []}
    assert c.post("/policies/compare", json={"left_policy_id": pid}, headers=h).json()["status"] == "insufficient"
    assert c.get(f"/policies/{pid}?as_of=junk", headers=h).status_code == 400
    assert c.get("/policies/not-a-uuid", headers=h).status_code in (400, 404, 422)
    other = str(db.create_organization(conn, "pytest-org-policy-b")["id"])
    conn.commit()
    cb = signed_in([api_policies.router], conn, other)
    for url in (f"/policies/{pid}", f"/policies/{pid}/history", f"/policies/{pid}/conflicts"):
        r = cb.get(url)
        assert r.status_code == 404 and "SYN-PROP" not in r.text
    r = cb.post("/policies/compare", json={"left_policy_id": pid})
    assert r.status_code == 404
    assert cb.get("/policies").json() == {"policies": []}
