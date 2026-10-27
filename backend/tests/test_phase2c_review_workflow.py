"""Phase 2C (v0.16.0) — review queue, lifecycle, permissions, Knowledge Core and
coverage decisions through the existing services, staleness, audit and tenants.
Synthetic documents only (Phase 1E Riverbend set + a same-date conflicting survey)."""
import json
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

import db
import knowledge.service as kc
import main
from tests.auth_helpers import make_user, signed_in
from tests.test_phase2b_client_intelligence import (ENDORSEMENT, FOLLOW_UP, POLICY, RENEWAL, SCHEDULE, SURVEY,
                                                    SURVEY_DISAGREES, _client, _seed)

DOCS = [("01_policy.pdf", POLICY), ("01b_renewal.pdf", RENEWAL), ("02_schedule.pdf", SCHEDULE),
        ("03_endorsement.pdf", ENDORSEMENT), ("04_survey.pdf", SURVEY), ("04b_survey.pdf", SURVEY_DISAGREES),
        ("06_followup.pdf", FOLLOW_UP)]


@pytest.fixture
def app():
    return main.app


@pytest.fixture
def q(conn, test_org, app, monkeypatch):
    """An organization with a linked client, real engine output, one failed
    document and one fact without evidence; Owner, Admin, two Reviewers, a Broker."""
    sub, bid = _seed(conn, test_org, DOCS, monkeypatch)
    owner = signed_in(app, conn, test_org, "OWNER")
    cid = _client(owner)
    db.link_submission_to_client(conn, sub, cid, test_org)
    bad = db.create_document(conn, organization_id=test_org, carrier_id=None, filename="scan_unreadable.pdf",
                             file_type="pdf", size_bytes=10, pages=0, chars=0, status="failed",
                             error_message="No readable text (synthetic).", extracted_text=None)
    db.set_document_submission(conn, str(bad["id"]), sub)
    loose = kc.create_fact(conn, organization_id=test_org, entity_id=bid, fact_type="business.survey_risk_rating",
                           value="Low", confidence={"value": 0.5, "type": "model_self_reported"},
                           produced_by="synthetic-test", status="needs_review")
    conn.commit()
    assert owner.post(f"/clients/{cid}/link-business", json={"business_id": bid}).status_code == 200
    return {"org": test_org, "bid": bid, "cid": cid, "sub": sub, "failed_doc": str(bad["id"]),
            "loose_fact": str(loose["id"]), "owner": owner,
            "admin": signed_in(app, conn, test_org, "ADMIN"),
            "rev1": signed_in(app, conn, test_org, "REVIEWER"), "rev2": signed_in(app, conn, test_org, "REVIEWER"),
            "broker": signed_in(app, conn, test_org, "BROKER")}


def _queue(c, **params):
    r = c.get("/review-queue", params={"page_size": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _one(c, kind, pred=lambda i: True):
    items = [i for i in _queue(c, kind=kind)["items"] if pred(i)]
    assert items, f"no {kind} item"
    return items[0]


def _detail(c, item_id):
    r = c.get(f"/review-items/{item_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _post(c, item_id, action, **body):
    return c.post(f"/review-items/{item_id}/{action}", json=body)


def _audit(conn, org, resource_id=None, action=None):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM audit_events WHERE organization_id = %s ORDER BY id", (org,))
        rows = cur.fetchall()
    return [r for r in rows if (resource_id is None or r["resource_id"] == resource_id)
            and (action is None or r["action"] == action)]


# --- the queue holds genuine items only ----------------------------------------------------------

def test_queue_lists_real_items_linked_to_client_and_evidence(conn, q):
    d = _queue(q["owner"])
    kinds = {i["kind"] for i in d["items"]}
    assert kinds == {"fact_conflict", "unverified_fact", "missing_evidence", "coverage_finding", "failed_document"}
    conflict = _one(q["owner"], "fact_conflict")
    assert conflict["client_id"] == q["cid"] and conflict["business_id"] == q["bid"]
    assert conflict["client_tab"] == "conflicts" and sorted(conflict["summary"]["values"]) == ["300", "310"]
    missing = _one(q["owner"], "missing_evidence")
    assert missing["subject_id"] == q["loose_fact"] and missing["summary"]["evidence_count"] == 0
    failed = _one(q["owner"], "failed_document")
    assert failed["subject_id"] == q["failed_doc"] and failed["client_id"] == q["cid"]
    assert "System finding" in d["note"] and "not a decision" in d["note"]
    # every unverified-fact item really is a current needs_review fact with evidence
    for i in _queue(q["owner"], kind="unverified_fact")["items"]:
        f = db.get_fact(conn, i["subject_id"], q["org"])
        assert f["status"] == "needs_review" and f["valid_to"] is None
        assert kc.get_evidence_for_fact(conn, i["subject_id"], q["org"])
    # a second refresh creates nothing new
    again = _queue(q["owner"])
    assert again["refresh"]["created"] == 0 and again["total"] == d["total"]


def test_conflict_detail_shows_every_competing_value_with_evidence(q):
    det = _detail(q["rev1"], _one(q["rev1"], "fact_conflict")["item_id"])
    vals = {f["value"]: f for f in det["subject"]["competing_values"]}
    assert set(vals) == {"300", "310"}
    assert all(f["evidence"] and f["evidence"][0]["page"] for f in vals.values())
    assert "never picks the newest" in det["subject"]["rule"]
    assert det["history"][0]["by_system"] and det["history"][0]["action"] == "created"


def test_empty_queue(conn, test_org, app):
    d = _queue(signed_in(app, conn, test_org))
    assert d["items"] == [] and d["total"] == 0


def test_decided_coverage_findings_are_not_queued(q):
    owner = q["owner"]
    audit = owner.get(f"/businesses/{q['bid']}/coverage-audit").json()
    f0, f1 = audit["findings"][0]["finding_id"], audit["findings"][1]["finding_id"]
    # decided in the Coverage panel before the queue ever saw it -> never queued
    owner.post(f"/coverage-findings/{f0}/review", json={"business_id": q["bid"], "decision": "ACCEPTED"})
    assert not [i for i in _queue(owner, kind="coverage_finding")["items"] if i["subject_id"] == f0]
    # queued first, then decided in the Coverage panel -> closed by the system, marked as such
    owner.post(f"/coverage-findings/{f1}/review", json={"business_id": q["bid"], "decision": "REVIEWED"})
    item = [i for i in _queue(owner, kind="coverage_finding")["items"] if i["subject_id"] == f1]
    assert item and item[0]["state"] == "RESOLVED"
    hist = _detail(owner, item[0]["item_id"])["history"]
    assert hist[-1]["by_system"] and "outside the queue" in hist[-1]["reason"]


# --- lifecycle -----------------------------------------------------------------------------------

def test_valid_and_invalid_transitions(q):
    r1 = q["rev1"]
    it = _one(r1, "coverage_finding")["item_id"]
    assert _post(r1, it, "decide", decision="ACCEPTED").status_code == 409          # not in review yet
    assert _post(r1, it, "needs-info", reason="x").status_code == 409               # not in review yet
    assert _post(r1, it, "reopen", reason="x").status_code == 409                   # not closed
    assert _post(r1, it, "start").json()["item"]["state"] == "IN_REVIEW"
    assert _post(r1, it, "start").status_code == 409                                # already in review
    assert _post(r1, it, "needs-info").status_code == 400                           # reason required
    assert _post(r1, it, "needs-info", reason="Need the 2027 schedule.").json()["item"]["state"] == "NEEDS_INFO"
    assert _post(r1, it, "start").json()["item"]["state"] == "IN_REVIEW"
    assert _post(r1, it, "decide", decision="MAYBE").status_code == 400
    det = _post(r1, it, "decide", decision="REVIEWED").json()
    assert det["item"]["state"] == "RESOLVED"
    assert [h["to_state"] for h in det["history"]] == ["OPEN", "IN_REVIEW", "NEEDS_INFO", "IN_REVIEW", "RESOLVED"]
    assert det["history"][-1]["decision"] == "REVIEWED" and not det["history"][-1]["by_system"]
    assert _post(r1, it, "reopen").status_code == 400                               # reason required
    assert _post(r1, it, "reopen", reason="Client sent a new schedule.").json()["item"]["state"] == "REOPENED"


def test_fact_items_cannot_be_reopened_by_hand(q):
    r1 = q["rev1"]
    it = _one(r1, "unverified_fact")["item_id"]
    _post(r1, it, "start")
    _post(r1, it, "decide", decision="verify")
    r = _post(r1, it, "reopen", reason="changed my mind")
    assert r.status_code == 409 and "automatically" in r.json()["detail"]


# --- assignment and permissions ------------------------------------------------------------------

def test_assignment_rules(q):
    owner, rev1, rev2, broker = q["owner"], q["rev1"], q["rev2"], q["broker"]
    it = _one(owner, "fact_conflict")["item_id"]
    assert _post(rev1, it, "assign", user_id=str(rev2.user["id"])).status_code == 403   # reviewers cannot assign
    assert _post(owner, it, "assign", user_id=str(broker.user["id"])).status_code == 400  # broker cannot do facts
    assert _post(owner, it, "assign", user_id=str(uuid.uuid4())).status_code == 400     # not a member
    d = _post(owner, it, "assign", user_id=str(rev1.user["id"])).json()
    assert d["item"]["state"] == "ASSIGNED" and d["item"]["assignee"]["user_id"] == str(rev1.user["id"])
    r = _post(rev2, it, "start")
    assert r.status_code == 403 and "another reviewer" in r.json()["detail"]
    d = _post(q["admin"], it, "assign", user_id=str(rev2.user["id"])).json()          # reassignment
    assert d["item"]["assignee"]["user_id"] == str(rev2.user["id"])
    assert _post(rev1, it, "start").status_code == 403
    assert _post(rev2, it, "start").json()["item"]["state"] == "IN_REVIEW"
    assert _post(owner, it, "assign").status_code == 409             # unassign (no user) only from ASSIGNED
    acts = [h["action"] for h in _detail(owner, it)["history"]]
    assert acts == ["created", "assign", "assign", "start"]


def test_broker_can_only_work_failed_documents(q):
    broker = q["broker"]
    fact_item = _one(broker, "unverified_fact")["item_id"]
    assert _post(broker, fact_item, "start").status_code == 403
    cov_item = _one(broker, "coverage_finding")["item_id"]
    assert _post(broker, cov_item, "start").status_code == 403
    doc_item = _one(broker, "failed_document")["item_id"]
    assert _post(broker, doc_item, "start").status_code == 200
    assert _post(broker, doc_item, "decide", decision="resolved").status_code == 400     # reason required
    d = _post(broker, doc_item, "decide", decision="resolved", reason="Re-uploaded a readable copy.").json()
    assert d["item"]["state"] == "RESOLVED"
    assert _one(broker, "failed_document")["state"] == "RESOLVED"                        # stays closed on refresh
    assert "assign" not in _detail(broker, doc_item)["allowed_actions"]


# --- Knowledge Core decisions through the existing services --------------------------------------

def test_verify_through_queue_uses_the_knowledge_core(conn, q):
    r1 = q["rev1"]
    item = _one(r1, "unverified_fact")
    _post(r1, item["item_id"], "start")
    d = _post(r1, item["item_id"], "decide", decision="verify").json()
    assert d["item"]["state"] == "RESOLVED"
    f = db.get_fact(conn, item["subject_id"], q["org"])
    assert f["status"] == "verified" and f["reviewed_by"].endswith(f"<{r1.user['email']}>")
    assert d["history"][-1]["evidence"]                       # the evidence the decision was based on


def test_correction_needs_a_reason_and_keeps_the_original(conn, q):
    r1 = q["rev1"]
    item = _one(r1, "unverified_fact", lambda i: i["summary"]["fact_type"] == "business.legal_name")
    _post(r1, item["item_id"], "start")
    assert _post(r1, item["item_id"], "decide", decision="correct", value="Riverbend Textiles Ltd").status_code == 400
    d = _post(r1, item["item_id"], "decide", decision="correct", value="Riverbend Textiles Ltd",
              reason="Synthetic test: name on the certificate.").json()
    old = db.get_fact(conn, item["subject_id"], q["org"])
    new = db.get_fact(conn, d["history"][-1]["metadata"]["new_fact_id"], q["org"])
    assert old["value"] != "Riverbend Textiles Ltd" and old["valid_to"] is not None    # original kept, closed
    assert new["value"] == "Riverbend Textiles Ltd" and new["status"] == "corrected"
    assert _audit(conn, q["org"], item["subject_id"], "fact.corrected")[0]["metadata"]["reason"].startswith("Synthetic")


def test_conflict_resolved_only_by_a_persons_choice(conn, q):
    r1 = q["rev1"]
    item = _one(r1, "fact_conflict")
    det = _post(r1, item["item_id"], "start").json()
    competing = {f["value"]: f["fact_id"] for f in det["subject"]["competing_values"]}
    assert _post(r1, item["item_id"], "decide", decision="correct", reason="x").status_code == 400  # no fact chosen
    assert _post(r1, item["item_id"], "decide", decision="correct", fact_id=competing["310"]).status_code == 400
    d = _post(r1, item["item_id"], "decide", decision="correct", fact_id=competing["310"],
              reason="Synthetic test: the second survey is the signed copy.").json()
    assert d["item"]["state"] == "RESOLVED"
    assert db.get_conflict(conn, item["subject_id"], q["org"])["status"] == "resolved"
    for v, fid in competing.items():                          # both originals still exist, values unchanged
        assert db.get_fact(conn, fid, q["org"])["value"] == v
    new = db.get_fact(conn, d["history"][-1]["metadata"]["new_fact_id"], q["org"])
    assert new["value"] == "310" and new["status"] == "corrected"
    assert len(d["history"][-1]["evidence"]) >= 2             # evidence of both sides recorded
    assert _one(r1, "fact_conflict")["state"] == "RESOLVED"   # refresh does not reopen it


def test_missing_evidence_cannot_be_verified_only_rejected_with_reason(conn, q):
    r1 = q["rev1"]
    item = _one(r1, "missing_evidence")
    _post(r1, item["item_id"], "start")
    assert _post(r1, item["item_id"], "decide", decision="verify").status_code == 400
    assert _post(r1, item["item_id"], "decide", decision="reject").status_code == 400
    d = _post(r1, item["item_id"], "decide", decision="reject", reason="Not stated in any document.").json()
    assert d["item"]["state"] == "DISMISSED"
    assert db.get_fact(conn, q["loose_fact"], q["org"])["status"] == "superseded"


def test_verified_fact_reopens_when_a_later_document_contradicts_it(conn, q):
    r1 = q["rev1"]
    item = _one(r1, "unverified_fact", lambda i: i["summary"]["fact_type"] == "business.workforce")
    _post(r1, item["item_id"], "start")
    assert _post(r1, item["item_id"], "decide", decision="verify").json()["item"]["state"] == "RESOLVED"
    f = db.get_fact(conn, item["subject_id"], q["org"])
    doc = kc.get_evidence_for_fact(conn, item["subject_id"], q["org"])[0]["document_id"]
    kc.reconcile_fact(conn, organization_id=q["org"], entity_id=str(f["entity_id"]), fact_type="business.workforce",
                      value="330", confidence={"value": 1.0, "type": "deterministic"}, produced_by="synthetic-test",
                      normalized_value=330, document_id=str(doc), evidence_page=1,
                      evidence_snippet="Estimated workforce: 330.", effective_from=f["effective_from"],
                      effective_to=f["effective_to"])
    conn.commit()
    assert db.get_fact(conn, item["subject_id"], q["org"])["status"] == "contradiction_flagged"
    reopened = [i for i in _queue(r1, kind="unverified_fact")["items"] if i["item_id"] == item["item_id"]][0]
    assert reopened["state"] == "REOPENED"
    last = _detail(r1, item["item_id"])["history"][-1]
    assert last["by_system"] and last["actor"] == "system" and "conflicts" in last["reason"]
    assert any("330" in i["summary"]["values"] for i in _queue(r1, kind="fact_conflict")["items"])


# --- coverage findings ---------------------------------------------------------------------------

def test_coverage_decisions_need_reason_to_dismiss_and_detect_staleness(conn, q):
    r1 = q["rev1"]
    item = _one(r1, "coverage_finding")
    _post(r1, item["item_id"], "start")
    assert _post(r1, item["item_id"], "decide", decision="REJECTED").status_code == 400
    d = _post(r1, item["item_id"], "decide", decision="REJECTED", reason="Exclusion does not apply to this site.").json()
    assert d["item"]["state"] == "DISMISSED" and d["subject"]["finding"]["review_status"] == "REJECTED"
    assert d["subject"]["finding"]["review"]["comment"] == "Exclusion does not apply to this site."
    # Simulate "the finding's evidence changed after the decision": the stored decision now
    # refers to a different evidence fingerprint than the engine computes.
    with conn.cursor() as cur:
        cur.execute("UPDATE kc_coverage_reviews SET fingerprint = 'evidence-before-change' "
                    "WHERE organization_id = %s AND subject_id::text = %s", (q["org"], item["subject_id"]))
    conn.commit()
    after = [i for i in _queue(r1, kind="coverage_finding")["items"] if i["item_id"] == item["item_id"]][0]
    assert after["state"] == "REOPENED" and after["summary"]["review_stale"] is True
    det = _detail(r1, item["item_id"])
    assert det["subject"]["finding"]["review_stale"] is True
    assert det["history"][-1]["by_system"] and "evidence changed" in det["history"][-1]["reason"]


def test_coverage_item_bound_to_its_own_business(conn, q, app, monkeypatch):
    """A second business in the same organization: the item decides only its own finding."""
    _, bid2 = _seed(conn, q["org"], [("01_policy.pdf", POLICY.replace("Riverbend", "Lakeside")),
                                     ("02_schedule.pdf", SCHEDULE.replace("Riverbend", "Lakeside"))], monkeypatch)
    for it in _queue(q["owner"], kind="coverage_finding")["items"]:
        assert it["business_id"] in (q["bid"], bid2)
        audit = q["owner"].get(f"/businesses/{it['business_id']}/coverage-audit").json()
        assert it["subject_id"] in {f["finding_id"] for f in audit["findings"]}


# --- audit and immutability ----------------------------------------------------------------------

def test_every_mutation_is_audited_with_the_session_identity(conn, q):
    owner, r1 = q["owner"], q["rev1"]
    it = _one(owner, "coverage_finding")["item_id"]
    _post(owner, it, "assign", user_id=str(r1.user["id"]))
    _post(r1, it, "start")
    _post(r1, it, "needs-info", reason="Need survey photos.")
    _post(r1, it, "start")
    _post(r1, it, "decide", decision="ACCEPTED")
    _post(r1, it, "reopen", reason="New survey arrived.")
    rows = _audit(conn, q["org"], it)
    assert [r["action"] for r in rows] == ["review.assign", "review.start", "review.needs_info", "review.start",
                                           "review.decide", "review.reopen"]
    assert rows[0]["actor_email"] == owner.user["email"] and all(r["actor_email"] == r1.user["email"] for r in rows[1:])
    assert rows[4]["metadata"]["decision"] == "ACCEPTED" and rows[5]["metadata"]["reason"] == "New survey arrived."
    assert _audit(conn, q["org"], action="coverage.review_recorded")
    text = json.dumps([dict(r) for r in rows], default=str)
    assert "password" not in text.lower() and "csrf" not in text.lower()


def test_review_history_cannot_be_changed(conn, q):
    _queue(q["owner"])
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM review_events WHERE organization_id = %s LIMIT 1", (q["org"],))
        eid = cur.fetchone()["id"]
    for sql in ("UPDATE review_events SET reason = 'rewritten' WHERE id = %s", "DELETE FROM review_events WHERE id = %s"):
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(sql, (eid,))
        conn.rollback()


def test_direct_endpoints_require_reasons_and_are_audited(conn, q):
    owner = q["owner"]
    item = _one(owner, "unverified_fact")
    fid = item["subject_id"]
    assert owner.put(f"/knowledge/facts/{fid}", json={"new_value": "x"}).status_code == 400
    assert owner.post(f"/knowledge/facts/{fid}/review", json={"action": "reject"}).status_code == 400
    assert owner.post(f"/knowledge/facts/{fid}/review", json={"action": "verify"}).status_code == 200
    assert _audit(conn, q["org"], fid, "fact.verified")
    audit = owner.get(f"/businesses/{q['bid']}/coverage-audit").json()
    f2 = audit["findings"][0]["finding_id"]
    body = {"business_id": q["bid"], "decision": "REJECTED"}
    assert owner.post(f"/coverage-findings/{f2}/review", json=body).status_code == 400
    assert owner.post(f"/coverage-findings/{f2}/review", json=dict(body, comment="Not relevant.")).status_code == 200
    assert _audit(conn, q["org"], f2, "coverage.review_recorded")[0]["metadata"]["reason"] == "Not relevant."


# --- sign-in and tenants -------------------------------------------------------------------------

def test_requires_sign_in(q, app):
    anon = TestClient(app)
    assert anon.get("/review-queue").status_code == 401
    it = _one(q["owner"], "fact_conflict")["item_id"]
    assert anon.get(f"/review-items/{it}").status_code == 401
    assert anon.post(f"/review-items/{it}/start").status_code == 401


def test_other_organization_sees_and_changes_nothing(conn, q, app):
    other_org = str(db.create_organization(conn, f"pytest-2c-other-{uuid.uuid4().hex[:8]}")["id"])
    conn.commit()
    intruder = signed_in(app, conn, other_org, "OWNER")
    it = _one(q["owner"], "fact_conflict")
    assert _queue(intruder)["total"] == 0
    assert intruder.get(f"/review-items/{it['item_id']}").status_code == 404
    for action, body in (("start", {}), ("assign", {"user_id": str(intruder.user["id"])}),
                         ("decide", {"decision": "correct", "reason": "x", "fact_id": str(uuid.uuid4())}),
                         ("needs-info", {"reason": "x"}), ("reopen", {"reason": "x"})):
        assert _post(intruder, it["item_id"], action, **body).status_code == 404, action
    r = intruder.get("/review-queue", headers={"X-Organization-Id": q["org"]})
    assert r.status_code == 403
    assert _detail(q["owner"], it["item_id"])["item"]["state"] == "OPEN"
    # assigning A's item to a user of another organization is refused
    outsider = make_user(conn, other_org, "REVIEWER")
    assert _post(q["owner"], it["item_id"], "assign", user_id=str(outsider["id"])).status_code == 400


def test_queue_filters(q):
    owner, r1 = q["owner"], q["rev1"]
    it = _one(owner, "fact_conflict")["item_id"]
    _post(owner, it, "assign", user_id=str(r1.user["id"]))
    mine = _queue(r1, assigned="me")["items"]
    assert [i["item_id"] for i in mine] == [it]
    assert all(i["assignee"] is None for i in _queue(r1, assigned="unassigned")["items"])
    assert all(i["kind"] == "coverage_finding" for i in _queue(r1, kind="coverage_finding")["items"])
    assert all(i["client_id"] == q["cid"] for i in _queue(r1, client_id=q["cid"])["items"])
    assert all(i["state"] in ("RESOLVED", "DISMISSED") for i in _queue(r1, state="closed")["items"])
    assert q["owner"].get("/review-queue?kind=nope").status_code == 400
    assert q["owner"].get("/review-queue?assigned=everyone").status_code == 400
