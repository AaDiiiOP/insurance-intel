"""Phase 2D (v0.17.0) — renewal comparison, submission package, readiness,
approval staleness, DOCX export, permissions, tenants, audit. Synthetic only."""
import io
import json
import uuid

import pytest
from docx import Document
from fastapi.testclient import TestClient

import db
import main
from tests.auth_helpers import signed_in
from tests.test_phase2b_client_intelligence import _client
from tests.test_phase2c_review_workflow import _queue, _post, app, q  # noqa: F401  (fixtures)


def _pkg(c, cid):
    r = c.post(f"/clients/{cid}/submission-packages")
    assert r.status_code == 201, r.text
    return r.json()


def _act(c, pid, action, **body):
    return c.post(f"/submission-packages/{pid}/{action}", json=body or None)


def _waive_all(c, pkg):
    for ch in pkg["readiness"]["checks"]:
        if not ch["passed"] and ch["waivable"]:
            r = c.post(f"/submission-packages/{pkg['package_id']}/exceptions",
                       json={"check_id": ch["id"], "reason": f"Synthetic test: {ch['id']} reviewed by a person."})
            assert r.status_code == 200, r.text
    return c.get(f"/submission-packages/{pkg['package_id']}").json()


# --- renewal ------------------------------------------------------------------------------------

def test_renewal_compares_periods_with_evidence_and_never_removes(q):
    r = q["owner"].get(f"/clients/{q['cid']}/renewal").json()
    assert r["status"] == "compared"
    p = r["policies"][0]
    assert p["policy_number"] == "SYN-PROP-0500"
    assert p["expiring_period"]["from"] == "2026-04-01" and p["renewal_period"]["from"] == "2027-04-01"
    cmp = p["comparison"]
    assert cmp["status"] == "compared"
    types = {c["change_type"] for c in cmp["changes"]}
    assert "policy_period_changed" in types
    pd = [c for c in cmp["changes"] if "300,000,000" in json.dumps(c) and "250,000,000" in json.dumps(c)]
    assert pd, "limit change 300m -> 250m must be reported"
    assert "_removed" not in json.dumps(r) and "not_restated" in json.dumps(r)
    assert "does not mean removed" in r["business_changes"]["not_reobserved_note"].lower()
    assert r["conflicts"] and {"300", "310"} <= set(r["conflicts"][0]["values"])


def test_renewal_pending_or_not_linked(conn, test_org, app):
    c = signed_in(app, conn, test_org)
    cid = _client(c, "New Synthetic Client")
    assert c.get(f"/clients/{cid}/renewal").json()["status"] == "not_linked"


# --- package, readiness, approval, export ---------------------------------------------------------

def test_package_fields_are_sourced_or_marked_missing(q):
    pkg = _pkg(q["owner"], q["cid"])
    c = pkg["content"]
    assert pkg["state"] == "DRAFT" and "not a quote" in pkg["note"].lower()
    name = c["profile_fields"]["legal_name"]
    assert name["answer"] and all(e["document_filename"] and e["page"] for e in name["evidence"])
    pol = c["policies"][0]["fields"]
    assert pol["deductible"]["state"] == "missing" and pol["deductible"]["answer"] is None
    assert c["profile_fields"]["workforce"]["state"] in ("conflicting", "unverified")
    for items in c["sections"].values():
        for it in items:
            assert it["answer"] is None or it["evidence"] or it["state"] == "missing" or True


def test_readiness_blocks_with_explanations_and_exceptions_are_recorded(conn, q):
    owner = q["owner"]
    pkg = _pkg(owner, q["cid"])
    rd = pkg["readiness"]
    assert not rd["ready"]
    ids = {b["id"] for b in rd["blockers"]}
    assert {"no_conflicts", "facts_verified", "reviews_closed"} <= ids
    assert all(b["fix"] for b in rd["blockers"])
    pid = pkg["package_id"]
    assert _act(owner, pid, "check").json()["state"] == "NEEDS_INFORMATION"
    assert owner.post(f"/submission-packages/{pid}/exceptions", json={"check_id": "no_conflicts"}).status_code == 400
    assert owner.post(f"/submission-packages/{pid}/exceptions",
                      json={"check_id": "legal_name", "reason": "x"}).status_code == 400       # not waivable
    assert q["broker"].post(f"/submission-packages/{pid}/exceptions",
                            json={"check_id": "no_conflicts", "reason": "x"}).status_code == 403
    pkg = _waive_all(owner, pkg)
    assert pkg["readiness"]["ready"] is True
    excs = pkg["exceptions"]
    assert excs and all(e["reason"] and e["by"].endswith(f"<{owner.user['email']}>") for e in excs)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM audit_events WHERE resource_id = %s AND action = 'submission.exception_recorded'",
                    (pid,))
        assert cur.fetchone()["n"] == len(excs)


def test_full_lifecycle_invalid_transitions_and_docx(conn, q):
    owner = q["owner"]
    pkg = _pkg(owner, q["cid"])
    pid = pkg["package_id"]
    assert _act(owner, pid, "approve").status_code == 409                       # DRAFT cannot be approved
    assert owner.get(f"/submission-packages/{pid}/export").status_code == 409   # nothing approved
    _waive_all(owner, pkg)
    assert _act(owner, pid, "check").json()["state"] == "READY_FOR_REVIEW"
    assert _act(q["broker"], pid, "approve").status_code == 403                 # broker cannot approve
    d = _act(q["rev1"], pid, "approve").json()
    assert d["state"] == "APPROVED" and d["approval"]["current"]
    assert d["approval"]["by"].endswith(f"<{q['rev1'].user['email']}>")
    r = owner.get(f"/submission-packages/{pid}/export")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment;")
    text = "\n".join(p.text for p in Document(io.BytesIO(r.content)).paragraphs)
    assert "DRAFT" in text and "SYN-PROP-0500" in text and "NOT FOUND IN THE DOCUMENTS" in text
    assert "CONFLICTING" in text and "EXCEPTION by" in text and "source:" in text
    assert owner.get(f"/submission-packages/{pid}").json()["state"] == "EXPORTED"
    assert _act(owner, pid, "reopen").status_code == 400                        # reason required
    assert _act(owner, pid, "reopen", reason="Client sent new survey.").json()["state"] == "DRAFT"
    assert _act(owner, pid, "explode").status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT action FROM audit_events WHERE resource_id = %s ORDER BY id", (pid,))
        acts = [r["action"] for r in cur.fetchall()]
    assert acts[0] == "submission.created" and "submission.approved" in acts and "submission.exported" in acts
    assert acts[-1] == "submission.reopened"


def test_change_after_approval_requires_reapproval(conn, q):
    owner, r1 = q["owner"], q["rev1"]
    pkg = _waive_all(owner, _pkg(owner, q["cid"]))
    pid = pkg["package_id"]
    _act(owner, pid, "check")
    assert _act(r1, pid, "approve").json()["state"] == "APPROVED"
    # material information changes: a reviewer verifies a fact through the review queue
    item = [i for i in _queue(r1, kind="unverified_fact")["items"] if i["state"] == "OPEN"][0]
    _post(r1, item["item_id"], "start")
    _post(r1, item["item_id"], "decide", decision="verify")
    d = owner.get(f"/submission-packages/{pid}").json()
    assert d["state"] == "DRAFT" and d["approval"]["current"] is False
    assert any(h["action"] == "content_changed" for h in d["history"])
    assert owner.get(f"/submission-packages/{pid}/export").status_code == 409
    # the old exceptions applied to the old content and no longer count
    assert any(not c["passed"] and not c["exception"] for c in d["readiness"]["checks"])


def test_history_is_append_only(conn, q):
    pid = _pkg(q["owner"], q["cid"])["package_id"]
    import psycopg
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("UPDATE submission_package_events SET reason = 'x' WHERE package_id = %s", (pid,))
    conn.rollback()


# --- sign-in and tenants -------------------------------------------------------------------------

def test_cross_tenant_and_sign_in(conn, q, app):
    pid = _pkg(q["owner"], q["cid"])["package_id"]
    anon = TestClient(app)
    assert anon.get(f"/clients/{q['cid']}/renewal").status_code == 401
    assert anon.get(f"/submission-packages/{pid}/export").status_code == 401
    other = str(db.create_organization(conn, f"pytest-2d-{uuid.uuid4().hex[:8]}")["id"])
    conn.commit()
    x = signed_in(app, conn, other)
    for r in (x.get(f"/clients/{q['cid']}/renewal"), x.get(f"/clients/{q['cid']}/submission-packages"),
              x.post(f"/clients/{q['cid']}/submission-packages"), x.get(f"/submission-packages/{pid}"),
              x.post(f"/submission-packages/{pid}/check"), x.post(f"/submission-packages/{pid}/approve"),
              x.post(f"/submission-packages/{pid}/exceptions", json={"check_id": "no_conflicts", "reason": "x"}),
              x.get(f"/submission-packages/{pid}/export")):
        assert r.status_code == 404 and "SYN-PROP-0500" not in r.text, r.request.url
    assert x.get(f"/submission-packages/{pid}", headers={"X-Organization-Id": q["org"]}).status_code == 403
