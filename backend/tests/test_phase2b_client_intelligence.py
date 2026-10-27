"""Phase 2B (v0.15.0) — unified client intelligence: GET /clients/{id}/intelligence.

The summary must be a faithful aggregation of the existing engines (no new
reasoning), keep evidence and review state, keep conflicts and history
visible, degrade per section, and respect sign-in, roles and tenants.
Synthetic documents only (the Phase 1E Riverbend set)."""
import json
import re
import uuid

import pytest

import business_intelligence as bi
import client_intelligence
import coverage_intelligence as ci
import db
import main
import policy_intelligence as pi
import property_policy_intelligence as ppi
from tests.auth_helpers import signed_in
from tests.test_coverage_intelligence import (ALL_TEXT, ENDORSEMENT, FOLLOW_UP, POLICY, RENEWAL, SCHEDULE,
                                              SURVEY)
from tests.test_policy_intelligence import _model_stub
from tests.test_policy_period import _submission

# A second survey on the SAME date that states a different workforce: the
# engines must keep both values as a conflict, never pick one.
SURVEY_DISAGREES = SURVEY.replace("Estimated workforce: 300.", "Estimated workforce: 310.").replace(
    "Property Risk Survey", "Property Risk Survey (second copy)")


@pytest.fixture
def app():
    return main.app


def _seed(conn, org, docs, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", _model_stub)
    sub = _submission(conn, org, docs)
    r = ppi.analyze_submission(conn, sub, org)
    conn.commit()
    return sub, str(r["business"]["id"])


def _client(c, name="Riverbend Textiles (synthetic)"):
    r = c.post("/clients", json={"business_name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["client_id"]


@pytest.fixture
def rb(conn, test_org, app, monkeypatch):
    """A client linked to an analyzed business with a renewal, endorsement,
    two dated surveys and a same-date conflict."""
    sub, bid = _seed(conn, test_org, [("01_policy.pdf", POLICY), ("01b_renewal.pdf", RENEWAL),
                                      ("02_schedule.pdf", SCHEDULE), ("03_endorsement.pdf", ENDORSEMENT),
                                      ("04_survey.pdf", SURVEY), ("04b_survey.pdf", SURVEY_DISAGREES),
                                      ("06_followup.pdf", FOLLOW_UP)], monkeypatch)
    c = signed_in(app, conn, test_org)
    cid = _client(c)
    db.link_submission_to_client(conn, sub, cid, test_org)
    conn.commit()
    assert c.post(f"/clients/{cid}/link-business", json={"business_id": bid}).status_code == 200
    return {"c": c, "org": test_org, "cid": cid, "bid": bid, "sub": sub}


def _get(c, cid, as_of=None):
    r = c.get(f"/clients/{cid}/intelligence" + (f"?as_of={as_of}" if as_of else ""))
    assert r.status_code == 200, r.text
    return r.json()


# --- aggregation is faithful to the existing engines --------------------------------------------

def test_summary_matches_the_existing_engines(conn, rb):
    d = _get(rb["c"], rb["cid"], "2026-09-20")
    s = d["sections"]
    assert d["business"]["business_id"] == rb["bid"]
    prof = bi.get_business_profile(conn, rb["org"], rb["bid"], as_of="2026-09-20")
    for k in client_intelligence.PROFILE_SECTIONS:
        assert s["profile"]["counts"][k] == len(prof[k] or []), k
    assert s["profile"]["fields"]["legal_name"]["answer"] == prof["legal_name"]["answer"]
    assert [p["policy_id"] for p in s["policies"]["items"]] == [p["policy_id"] for p in prof["policies"]]
    audit = ci.get_coverage_audit(conn, rb["org"], rb["bid"], as_of="2026-09-20")
    assert s["coverage"]["finding_counts"] == audit["finding_counts"]
    want = {}
    for m in audit["coverage_map"]["mappings"]:
        want[m["status"]] = want.get(m["status"], 0) + 1
    assert s["coverage"]["mapping_status_counts"] == want
    assert {f["finding_id"] for f in s["coverage"]["open_findings"]} == {
        f["finding_id"] for f in audit["findings"] if f["review_status"] == "OPEN"}
    biz_conf = bi.find_business_conflicts(conn, rb["org"], rb["bid"])
    pol_conf = sum(len(pi.find_policy_conflicts(conn, rb["org"], p["policy_id"])) for p in prof["policies"])
    assert s["conflicts"]["total"] == len(biz_conf) + pol_conf
    pol = s["policies"]["items"][0]
    view = pi.get_policy(conn, rb["org"], pol["policy_id"], as_of="2026-09-20")
    assert pol["fields"]["policy_number"]["answer"] == view["policy_number"]["answer"] == "SYN-PROP-0500"
    assert pol["period_status"] == view["period"]["status"]


def test_documents_section_reports_processing(conn, rb):
    docs = _get(rb["c"], rb["cid"])["sections"]["documents"]
    assert docs["status"] == "available" and docs["intelligence_documents"] == 7
    assert docs["by_status"] == {"parsed": 7} and docs["errors"] == []


# --- evidence and missing evidence --------------------------------------------------------------

def test_values_carry_document_page_and_snippet(rb):
    d = _get(rb["c"], rb["cid"], "2026-09-20")
    name = d["sections"]["profile"]["fields"]["legal_name"]
    assert name["answer"] and name["evidence"]
    for e in name["evidence"]:
        assert e["document_id"] and e["document_filename"] and e["page"] and e["snippet"]
    num = d["sections"]["policies"]["items"][0]["fields"]["policy_number"]
    assert num["evidence"] and all(e["page"] for e in num["evidence"])
    for f in d["sections"]["coverage"]["open_findings"]:
        assert "evidence_sufficient" in f
    prov = d["sections"]["profile"]["provenance"]
    assert prov["facts"] > 0 and sum(prov["by_source"].values()) == prov["facts"]


def test_missing_values_say_so_and_never_invent(rb):
    d = _get(rb["c"], rb["cid"], "2026-09-20")
    ded = d["sections"]["policies"]["items"][0]["fields"]["deductible"]
    assert ded["answer"] is None and ded["state"] == "missing" and ded["evidence"] == []
    assert ded["reason"]
    prof = d["sections"]["profile"]
    assert set(prof["missing_sections"]) == {k for k, n in prof["counts"].items() if n == 0}


# --- conflicts and history ----------------------------------------------------------------------

def test_conflicting_values_are_all_kept_and_flagged(rb):
    d = _get(rb["c"], rb["cid"], "2026-03-15")
    wf = d["sections"]["profile"]["fields"]["workforce"]
    assert wf["state"] == "conflicting" and set(wf["values"]) >= {"300", "310"}
    conf = d["sections"]["conflicts"]
    keys = {(c["key"], tuple(sorted(c["values"]))) for c in conf["business"]}
    assert any("workforce" in k and {"300", "310"} <= set(v) for k, v in keys)
    assert all(c["evidence"] for c in conf["business"])
    assert any(a["kind"] == "conflict" for a in d["attention"])


def test_history_is_point_in_time_and_not_reobserved_is_not_removed(rb):
    early = _get(rb["c"], rb["cid"], "2026-08-01")
    late = _get(rb["c"], rb["cid"], "2026-09-20")
    assert early["historical"] is True and early["as_of"] == "2026-08-01"
    assert early["sections"]["profile"]["counts"]["risks"] == 0      # risks first documented 15 Sep
    assert late["sections"]["profile"]["counts"]["risks"] == 3
    ch = late["sections"]["changes"]
    assert ch["status"] == "available" and "not_reobserved" in ch
    assert "removed" not in json.dumps(late).lower()
    renewal = _get(rb["c"], rb["cid"], "2027-06-01")
    numbers = {p["fields"]["policy_number"]["answer"] for p in renewal["sections"]["policies"]["items"]}
    assert numbers == {"SYN-PROP-0500"}


def test_no_advice_or_underinsured_wording(rb):
    for day in ("2026-08-01", "2026-09-20"):
        text = json.dumps(_get(rb["c"], rb["cid"], day))
        # The two disclaimers deliberately name what is NOT being said.
        for note in (client_intelligence.NOTE, ci.NOTE):
            text = text.replace(json.dumps(note)[1:-1], "")
        assert not ALL_TEXT.search(text), text[max(0, ALL_TEXT.search(text).start() - 200):][:400]


# --- empty, partial and failing ------------------------------------------------------------------

def test_new_client_without_business_is_not_linked_everywhere(conn, test_org, app):
    c = signed_in(app, conn, test_org)
    cid = _client(c, "Empty Synthetic Client")
    d = _get(c, cid)
    assert d["business"] is None
    for k in ("profile", "policies", "coverage", "conflicts", "changes"):
        assert d["sections"][k]["status"] == "not_linked" and d["sections"][k]["reason"]
    assert d["sections"]["documents"] == {"status": "available", "intelligence_documents": 0, "by_status": {},
                                          "paperwork_documents": 0, "paperwork_verdicts": {}, "errors": []}
    assert d["attention"] == []


def test_partly_processed_client_shows_failed_documents(conn, test_org, app):
    c = signed_in(app, conn, test_org)
    cid = _client(c, "Partly Processed Synthetic Client")
    sub = db.create_submission(conn, organization_id=test_org, label=f"partial-{uuid.uuid4().hex[:6]}")
    ok = db.create_document(conn, organization_id=test_org, carrier_id=None, filename="policy.pdf", file_type="pdf",
                            size_bytes=10, pages=1, chars=10, status="parsed", error_message=None,
                            extracted_text="[Page 1]\nProperty Policy")
    bad = db.create_document(conn, organization_id=test_org, carrier_id=None, filename="scan.pdf", file_type="pdf",
                             size_bytes=10, pages=0, chars=0, status="failed",
                             error_message="No readable text (synthetic failure).", extracted_text=None)
    for d in (ok, bad):
        db.set_document_submission(conn, str(d["id"]), str(sub["id"]))
    db.link_submission_to_client(conn, str(sub["id"]), cid, test_org)
    conn.commit()
    d = _get(c, cid)
    docs = d["sections"]["documents"]
    assert docs["by_status"] == {"parsed": 1, "failed": 1}
    assert docs["errors"] == [{"document_id": str(bad["id"]), "filename": "scan.pdf", "status": "failed",
                               "error": "No readable text (synthetic failure)."}]
    assert any(a["kind"] == "processing_error" for a in d["attention"])
    assert d["sections"]["profile"]["status"] == "not_linked"


def test_one_engine_failing_leaves_the_others_working(rb, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("coverage engine down (synthetic)")
    monkeypatch.setattr(ci, "get_coverage_audit", boom)
    d = _get(rb["c"], rb["cid"], "2026-09-20")
    s = d["sections"]
    assert s["coverage"]["status"] == "unavailable" and "RuntimeError" in s["coverage"]["error"]
    assert "coverage engine down" not in s["coverage"]["error"]       # no internals leaked
    for k in ("profile", "policies", "conflicts", "changes", "documents"):
        assert s[k]["status"] == "available", k                     # later sections still ran
    assert any(a["kind"] == "unavailable" for a in d["attention"])


def test_bad_input_is_refused(rb):
    c = rb["c"]
    assert c.get(f"/clients/{rb['cid']}/intelligence?as_of=next-week").status_code == 400
    assert c.get("/clients/not-a-uuid/intelligence").status_code in (400, 422)
    assert c.get(f"/clients/{uuid.uuid4()}/intelligence").status_code == 404


# --- sign-in, roles, tenants --------------------------------------------------------------------

def test_requires_sign_in(rb, app):
    from fastapi.testclient import TestClient
    assert TestClient(app).get(f"/clients/{rb['cid']}/intelligence").status_code == 401


@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "BROKER", "REVIEWER"])
def test_every_role_can_read_the_same_summary(conn, rb, app, role):
    c = signed_in(app, conn, rb["org"], role)
    assert _get(c, rb["cid"], "2026-09-20")["sections"] == _get(rb["c"], rb["cid"], "2026-09-20")["sections"]


def test_other_organization_gets_404_with_a_real_client_id(conn, rb, app):
    other_org = str(db.create_organization(conn, f"pytest-2b-other-{uuid.uuid4().hex[:8]}")["id"])
    conn.commit()
    other = signed_in(app, conn, other_org)
    r = other.get(f"/clients/{rb['cid']}/intelligence")
    assert r.status_code == 404 and "Riverbend" not in r.text
    r = other.get(f"/clients/{rb['cid']}/intelligence", headers={"X-Organization-Id": rb["org"]})
    assert r.status_code == 403


def test_link_to_another_tenants_business_exposes_nothing(conn, rb, app):
    """Even if a client row pointed at another organization's business (tampered
    data), every engine call is scoped to the caller's organization."""
    other_org = str(db.create_organization(conn, f"pytest-2b-tamper-{uuid.uuid4().hex[:8]}")["id"])
    conn.commit()
    other = signed_in(app, conn, other_org)
    cid = _client(other, "Tampered Synthetic Client")
    with conn.cursor() as cur:
        cur.execute("UPDATE clients SET business_entity_id = %s WHERE id = %s", (rb["bid"], cid))
    conn.commit()
    d = _get(other, cid)
    assert d["business"] is None
    assert all(d["sections"][k]["status"] == "not_linked" for k in ("profile", "policies", "coverage"))
    assert "SYN-PROP-0500" not in json.dumps(d) and "Riverbend" not in json.dumps(d).replace("Tampered", "")


# --- Knowledge Core fix found by Phase 2B (v0.15.0) ---------------------------------------------

def test_later_period_keeps_both_sides_of_an_earlier_conflict(conn, test_org, monkeypatch):
    """Two same-date surveys disagree (300 vs 310); a later survey (320) then
    starts a new period. Before v0.15.0 only one of the two earlier facts was
    closed, so the earlier period showed a single value. Both must stay."""
    _, bid = _seed(conn, test_org, [("01_policy.pdf", POLICY), ("04_survey.pdf", SURVEY),
                                    ("04b_survey.pdf", SURVEY_DISAGREES), ("06_followup.pdf", FOLLOW_UP)],
                   monkeypatch)
    march = bi.get_business_profile(conn, test_org, bid, as_of="2026-03-15")["workforce"]
    assert march["answer"] is None and march["review_status"] == "contradiction"
    assert sorted(march["values"]) == ["300", "310"]
    sept = bi.get_business_profile(conn, test_org, bid, as_of="2026-09-20")["workforce"]
    assert sept["answer"] == "320"
    with conn.cursor() as cur:
        cur.execute("SELECT value, valid_to IS NULL AS open FROM kc_facts WHERE entity_id = %s "
                    "AND fact_type = 'business.workforce' ORDER BY value", (bid,))
        rows = {r["value"]: r["open"] for r in cur.fetchall()}
    assert rows == {"300": False, "310": False, "320": True}
