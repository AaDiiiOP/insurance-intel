"""Phase 2A (v0.14.0) — authentication, sessions, CSRF, rate limiting, roles,
memberships, invitations, password resets, tenant isolation over EVERY route
of the real app, client management, document workspace, downloads, audit."""
import io
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from docx import Document
from fastapi.testclient import TestClient

import auth
import db
import llm
import main
from tests.auth_helpers import PASSWORD, make_user, signed_in


def _docx(text: str) -> bytes:
    d = Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def app():
    return main.app


@pytest.fixture
def owner(conn, test_org, app):
    return signed_in(app, conn, test_org, "OWNER")


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: False)


# --- authentication -----------------------------------------------------------------

def test_password_is_hashed_and_login_sets_secure_cookie(conn, test_org, app):
    u = make_user(conn, test_org, "BROKER")
    assert u["password_hash"].startswith("$argon2id$") and PASSWORD not in u["password_hash"]
    c = TestClient(app)
    r = c.post("/auth/login", json={"email": u["email"].upper(), "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "aurevia_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    me = c.get("/auth/me").json()
    assert me["user"]["email"] == u["email"] and me["role"] == "BROKER"
    assert me["organization"]["organization_id"] == test_org and "clients.edit" in me["permissions"]
    assert "users.manage" not in me["permissions"]
    with conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM auth_sessions WHERE user_id = %s", (u["id"],))
        stored = cur.fetchone()["token_hash"]
    token = c.cookies.get("aurevia_session")
    assert stored == auth.token_hash(token) and stored != token          # only a hash is stored


def test_wrong_password_and_unknown_email_look_the_same(conn, test_org, app):
    u = make_user(conn, test_org)
    c = TestClient(app)
    a = c.post("/auth/login", json={"email": u["email"], "password": "wrong password!!"})
    b = c.post("/auth/login", json={"email": "nobody@example.test", "password": "wrong password!!"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    assert c.get("/auth/me").status_code == 401


def test_every_protected_route_needs_a_session(app):
    c = TestClient(app)
    checked = 0
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()) - {"HEAD"}:
            need = auth.permission_for(m, r.path)
            if need == "public" or r.path in ("/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"):
                continue
            url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), r.path)
            resp = c.request(m, url, json={})
            assert resp.status_code == 401, f"{m} {r.path} -> {resp.status_code}"
            checked += 1
    assert checked > 90


def test_every_state_changing_route_has_an_explicit_permission(app):
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()) - {"HEAD", "GET"}:
            assert auth.permission_for(m, r.path) is not None, f"unmapped {m} {r.path}"


def test_csrf_token_required_for_changes(owner):
    token = owner.headers.pop("X-CSRF-Token")
    assert owner.post("/clients", json={"business_name": "No CSRF Ltd"}).status_code == 403
    assert owner.post("/clients", json={"business_name": "Bad CSRF Ltd"},
                      headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert owner.post("/clients", json={"business_name": "Good CSRF Ltd"},
                      headers={"X-CSRF-Token": token}).status_code == 201


def test_logout_revokes_and_new_login_rotates_the_session(conn, test_org, app):
    c = signed_in(app, conn, test_org)
    old = c.cookies.get("aurevia_session")
    assert c.post("/auth/logout").status_code == 200
    c.cookies.set("aurevia_session", old)
    assert c.get("/auth/me").status_code == 401
    c2 = TestClient(app)
    c2.cookies.set("aurevia_session", "attacker-chosen-value")
    r = c2.post("/auth/login", json={"email": c.user["email"], "password": PASSWORD})
    issued = re.search(r"aurevia_session=([^;]+)", r.headers["set-cookie"]).group(1)
    assert issued not in ("attacker-chosen-value", old)              # a fresh token: no fixation


def test_expired_idle_and_suspended_sessions_are_refused(conn, test_org, app):
    c = signed_in(app, conn, test_org, "BROKER")
    sid = auth.token_hash(c.cookies.get("aurevia_session"))
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_sessions SET last_seen_at = now() - interval '1 day' WHERE token_hash = %s", (sid,))
    conn.commit()
    assert c.get("/auth/me").status_code == 401                       # idle timeout
    c = signed_in(app, conn, test_org, "BROKER")
    sid = auth.token_hash(c.cookies.get("aurevia_session"))
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 minute' WHERE token_hash = %s", (sid,))
    conn.commit()
    assert c.get("/auth/me").status_code == 401                       # absolute expiry
    c = signed_in(app, conn, test_org, "BROKER")
    db.update_membership(conn, test_org, str(c.user["id"]), None, "suspended")
    conn.commit()
    assert c.get("/clients").status_code == 401                       # suspended membership


def test_password_change_signs_out_other_sessions(conn, test_org, app):
    a = signed_in(app, conn, test_org)
    b = signed_in(app, conn, test_org, user=a.user)
    assert a.post("/auth/change-password", json={"current_password": PASSWORD,
                                                 "new_password": "a brand new passphrase"}).status_code == 200
    assert b.get("/auth/me").status_code == 401
    assert a.get("/auth/me").status_code == 200                       # the changer gets a fresh session


def test_failed_logins_are_rate_limited(conn, test_org, app, monkeypatch):
    monkeypatch.setattr(auth, "MAX_FAILS_PER_EMAIL", 3)
    u = make_user(conn, test_org)
    c = TestClient(app)
    for _ in range(3):
        assert c.post("/auth/login", json={"email": u["email"], "password": "nope nope nope"}).status_code == 401
    r = c.post("/auth/login", json={"email": u["email"], "password": PASSWORD})
    assert r.status_code == 429                                       # even the right password waits


# --- organizations, roles, members ---------------------------------------------------------

def test_header_naming_another_organization_is_refused(conn, two_test_orgs, app):
    a, b = two_test_orgs
    c = signed_in(app, conn, a)
    assert c.get("/clients", headers={"X-Organization-Id": b}).status_code == 403
    assert c.get("/clients", headers={"X-Organization-Id": a}).status_code == 200


def test_roles_are_enforced_on_the_server(conn, test_org, app):
    rev = signed_in(app, conn, test_org, "REVIEWER")
    brk = signed_in(app, conn, test_org, "BROKER")
    assert rev.post("/clients", json={"business_name": "Reviewer Co"}).status_code == 403
    cid = brk.post("/clients", json={"business_name": "Broker Co"}).json()["client_id"]
    assert brk.delete(f"/clients/{cid}").status_code == 403           # delete is Owner/Admin only
    assert brk.get("/organization/members").status_code == 403
    assert brk.get("/audit-events").status_code == 403
    assert rev.get("/audit-events").status_code == 403
    assert rev.post("/organization/invitations", json={"email": "x@example.test", "role": "BROKER"}).status_code == 403
    assert rev.get(f"/clients/{cid}").status_code == 200              # everyone can view


def test_switch_organization_needs_membership(conn, two_test_orgs, app):
    a, b = two_test_orgs
    c = signed_in(app, conn, a)
    assert c.post("/auth/switch-organization", json={"organization_id": b}).status_code == 404
    db.add_membership(conn, b, str(c.user["id"]), "BROKER")
    conn.commit()
    r = c.post("/auth/switch-organization", json={"organization_id": b})
    assert r.status_code == 200 and r.json()["organization"]["organization_id"] == b and r.json()["role"] == "BROKER"
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert {o["organization_id"] for o in c.get("/organizations").json()["organizations"]} == {a, b}


def test_invitation_is_single_use_and_role_limited(conn, test_org, app):
    admin = signed_in(app, conn, test_org, "ADMIN")
    assert admin.post("/organization/invitations", json={"email": "boss@example.test", "role": "OWNER"}).status_code == 403
    email = f"new-{uuid.uuid4().hex[:6]}@example.test"
    inv = admin.post("/organization/invitations", json={"email": email, "role": "REVIEWER"}).json()
    anon = TestClient(app)
    ok = anon.post("/auth/invitations/accept", json={"token": inv["token"], "display_name": "Neha R",
                                                     "password": "neha's long password"})
    assert ok.status_code == 200 and ok.json()["role"] == "REVIEWER"
    again = anon.post("/auth/invitations/accept", json={"token": inv["token"], "display_name": "X",
                                                        "password": "another long password"})
    assert again.status_code == 400
    r = anon.post("/auth/login", json={"email": email, "password": "neha's long password"})
    assert r.status_code == 200 and r.json()["role"] == "REVIEWER"
    with conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM auth_tokens WHERE email = %s", (email,))
        assert cur.fetchone()["token_hash"] != inv["token"]


def test_expired_invitation_is_refused(conn, test_org, app):
    owner = signed_in(app, conn, test_org)
    inv = owner.post("/organization/invitations", json={"email": f"late-{uuid.uuid4().hex[:6]}@example.test",
                                                        "role": "BROKER"}).json()
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_tokens SET expires_at = now() - interval '1 minute' WHERE id = %s",
                    (inv["invitation_id"],))
    conn.commit()
    r = TestClient(app).post("/auth/invitations/accept", json={"token": inv["token"], "display_name": "L",
                                                               "password": "late long password"})
    assert r.status_code == 400


def test_password_reset_by_admin_is_single_use_and_signs_out(conn, test_org, app):
    owner = signed_in(app, conn, test_org)
    member = signed_in(app, conn, test_org, "BROKER")
    tok = owner.post(f"/organization/members/{member.user['id']}/password-reset").json()["token"]
    anon = TestClient(app)
    assert anon.post("/auth/password-reset/complete", json={"token": tok, "password": "short"}).status_code == 400
    assert anon.post("/auth/password-reset/complete",
                     json={"token": tok, "password": "a fresh long password"}).status_code == 200
    assert member.get("/auth/me").status_code == 401
    assert anon.post("/auth/password-reset/complete",
                     json={"token": tok, "password": "yet another password"}).status_code == 400
    assert anon.post("/auth/login", json={"email": member.user["email"],
                                          "password": "a fresh long password"}).status_code == 200


def test_last_owner_cannot_be_removed_and_admin_cannot_touch_owner(conn, test_org, app):
    owner = signed_in(app, conn, test_org)
    admin = signed_in(app, conn, test_org, "ADMIN")
    oid = str(owner.user["id"])
    assert owner.put(f"/organization/members/{oid}", json={"role": "BROKER"}).status_code == 400
    assert admin.put(f"/organization/members/{oid}", json={"status": "suspended"}).status_code == 403
    r = owner.put(f"/organization/members/{admin.user['id']}", json={"role": "BROKER"})
    assert r.status_code == 200 and r.json()["role"] == "BROKER"
    assert admin.get("/auth/me").status_code == 401                   # role change signs the member out


# --- tenant isolation over the whole application ----------------------------------------------

@pytest.fixture
def tenant_a(conn, test_org, app):
    """Real resources in organization A, created through the API."""
    c = signed_in(app, conn, test_org)
    client = c.post("/clients", json={"business_name": "Secretive Textiles Pvt Ltd",
                                      "risk_notes": "SECRET-NOTE-123"}).json()
    cid = client["client_id"]
    up = c.post(f"/clients/{cid}/documents/upload", data={"requirement": "Fire NOC"},
                files={"file": ("noc.docx", _docx("Fire NOC for Secretive Textiles SECRET-NOTE-123"),
                                "application/octet-stream")})
    assert up.status_code == 200, up.text
    intel = c.post(f"/clients/{cid}/intelligence-documents",
                   files={"file": ("policy.docx", _docx("Policy Number: SYN-SECRET-9\nPeriod: 01 April 2026 to "
                                                         "31 March 2027\nProperty Damage Sum Insured: n1,000"),
                                   "application/octet-stream")})
    assert intel.status_code == 201, intel.text
    draft = db.save_draft(conn, client_id=cid, organization_id=test_org, carrier_id=None, kind="submission",
                          title="SECRET draft", body="SECRET-NOTE-123 body", body_redacted="x", warnings=[],
                          stats={}, model="test")
    conn.commit()
    return {"c": c, "org": test_org, "client_id": cid, "client_doc": up.json()["document_id"],
            "doc": intel.json()["document_id"], "submission": intel.json()["submission_id"],
            "draft": str(draft["id"])}


def test_no_route_of_the_app_reaches_another_tenants_data(conn, tenant_a, app):
    other = str(db.create_organization(conn, "pytest-org-2a-intruder")["id"])
    conn.commit()
    intruder = signed_in(app, conn, other)
    ids = {"client_id": tenant_a["client_id"], "document_id": tenant_a["doc"], "draft_id": tenant_a["draft"],
           "submission_id": tenant_a["submission"]}
    leaks, statuses = [], {}
    for r in app.routes:
        for m in (getattr(r, "methods", None) or set()) - {"HEAD"}:
            if auth.permission_for(m, r.path) == "public" or r.path.startswith(("/auth", "/docs", "/openapi",
                                                                                   "/redoc")):
                continue
            url = r.path
            for k, v in ids.items():
                url = url.replace("{" + k + "}", v)
            url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), url)
            if "{" in r.path and url == r.path:
                continue
            body = {"business_id": None, "submission_id": tenant_a["submission"], "question": "client notes?",
                    "use_llm": False, "business_name": "x", "decision": "REVIEWED", "action": "verify",
                    "new_value": "1", "requirement": "x", "status": "held", "title": "t", "body": "b",
                    "left_policy_id": str(uuid.uuid4()), "guidance": "g", "name": "n", "email": "e@x.test",
                    "role": "BROKER"}
            resp = intruder.request(m, url, json=body)
            statuses[resp.status_code] = statuses.get(resp.status_code, 0) + 1
            text = resp.text
            if any(s in text for s in ("SECRET-NOTE-123", "Secretive Textiles", "SYN-SECRET-9", "SECRET draft")):
                leaks.append((m, r.path, resp.status_code))
            if "{" in r.path and m != "GET" and resp.status_code < 300 and not r.path.startswith("/house-style"):
                leaks.append((m, r.path, resp.status_code, "write accepted"))
    assert not leaks, leaks
    # The requests really reached the handlers (not stopped by CSRF / sign-in).
    assert statuses.get(404, 0) > 40 and 401 not in statuses and statuses.get(403, 0) < 5, statuses
    # A's data is intact.
    c = tenant_a["c"]
    assert c.get(f"/clients/{tenant_a['client_id']}").json()["risk_notes"] == "SECRET-NOTE-123"
    assert c.get(f"/drafts/{tenant_a['draft']}").json()["status"] == "draft"


# --- clients ---------------------------------------------------------------------------------

def test_client_create_edit_archive_search_paginate(conn, test_org, app):
    owner = signed_in(app, conn, test_org)
    broker = signed_in(app, conn, test_org, "BROKER")
    for i in range(7):
        owner.post("/clients", json={"business_name": f"Paginate Co {i}", "industry": "textiles" if i % 2 else "food"})
    page = owner.get("/clients?page=1&page_size=3").json()
    assert page["total"] == 7 and len(page["clients"]) == 3
    assert len(owner.get("/clients?page=3&page_size=3").json()["clients"]) == 1
    assert owner.get("/clients?q=textiles").json()["total"] == 3
    assert owner.get("/clients?q=%25").json()["total"] == 0            # wildcard is escaped
    cid = owner.get("/clients?q=Paginate Co 1").json()["clients"][0]["client_id"]
    r = owner.put(f"/clients/{cid}", json={"business_name": "Paginate Co 1", "trading_name": "PC One",
                                           "assigned_user_id": str(broker.user["id"]),
                                           "extracted_fields": ["industry", "not_a_field"]})
    assert r.status_code == 200 and r.json()["trading_name"] == "PC One"
    assert r.json()["field_provenance"] == {"industry": "extracted_unconfirmed"}
    assert owner.get("/clients?assigned_to=" + str(broker.user["id"])).json()["total"] == 1
    assert broker.get("/clients?assigned_to=me").json()["total"] == 1
    stranger = make_user(conn, None)
    assert owner.put(f"/clients/{cid}", json={"business_name": "Paginate Co 1",
                                              "assigned_user_id": str(stranger["id"])}).status_code == 400
    assert owner.post(f"/clients/{cid}/archive").json()["status"] == "archived"
    assert owner.get("/clients").json()["total"] == 6
    assert owner.get("/clients?status=archived").json()["total"] == 1
    assert owner.post(f"/clients/{cid}/restore").json()["status"] == "active"
    assert owner.get("/clients?status=bogus").status_code == 400
    acts = [e["action"] for e in owner.get(f"/audit-events?resource_id={cid}").json()["events"]]
    assert {"client.created", "client.updated", "client.archived", "client.restored"} <= set(acts)


# --- documents ----------------------------------------------------------------------------------

def test_document_upload_download_duplicate_and_access(conn, tenant_a, app):
    c, cid = tenant_a["c"], tenant_a["client_id"]
    ws = c.get(f"/clients/{cid}/workspace").json()
    kinds = {d["kind"]: d for d in ws["documents"]}
    assert set(kinds) == {"client_paperwork", "intelligence"} and all(d["downloadable"] for d in ws["documents"])
    r = c.get(kinds["client_paperwork"]["download_url"])
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert r.headers["content-disposition"].startswith("attachment;") and r.headers["x-content-type-options"] == "nosniff"
    assert c.get(kinds["intelligence"]["download_url"]).status_code == 200
    dup = c.post(f"/clients/{cid}/documents/upload", data={"requirement": "Again"},
                 files={"file": ("copy.docx", _docx("Fire NOC for Secretive Textiles SECRET-NOTE-123"),
                                 "application/octet-stream")})
    assert dup.status_code == 409
    bad = c.post(f"/clients/{cid}/intelligence-documents", files={"file": ("x.exe", b"MZ....", "application/x")})
    assert bad.status_code == 400
    reviewer = signed_in(app, conn, tenant_a["org"], "REVIEWER")
    assert reviewer.get(kinds["client_paperwork"]["download_url"]).status_code == 200      # evidence.view
    assert reviewer.post(f"/clients/{cid}/intelligence-documents",
                         files={"file": ("p.docx", _docx("new"), "application/octet-stream")}).status_code == 403
    wrong_client = c.post("/clients", json={"business_name": "Other Client"}).json()["client_id"]
    assert c.get(f"/clients/{wrong_client}/documents/{tenant_a['client_doc']}/download").status_code == 404
    assert c.delete(f"/clients/{wrong_client}/documents/{tenant_a['client_doc']}").status_code == 404
    acts = [e["action"] for e in c.get("/audit-events?action=document.downloaded").json()["events"]]
    assert len(acts) >= 3


def test_client_links_to_its_business_once(conn, tenant_a, app, monkeypatch):
    import property_policy_intelligence as ppi
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": []})
    c = tenant_a["c"]
    r = c.post(f"/knowledge/submissions/{tenant_a['submission']}/analyze-property-policy")
    assert r.status_code == 200, r.text
    ws = c.get(f"/clients/{tenant_a['client_id']}/workspace").json()
    sug = ws["suggested_business"]
    assert sug and "submission" in sug["basis"]
    assert c.post(f"/clients/{tenant_a['client_id']}/link-business",
                  json={"business_id": sug["business_id"]}).status_code == 200
    ws = c.get(f"/clients/{tenant_a['client_id']}/workspace").json()
    assert ws["business"]["business_id"] == sug["business_id"] and ws["suggested_business"] is None
    other_client = c.post("/clients", json={"business_name": "Second"}).json()["client_id"]
    assert c.post(f"/clients/{other_client}/link-business",
                  json={"business_id": sug["business_id"]}).status_code == 409
    assert c.post(f"/clients/{other_client}/link-business",
                  json={"business_id": str(uuid.uuid4())}).status_code == 404


# --- approvals and audit ------------------------------------------------------------------------

def test_approval_identity_comes_from_the_session(conn, tenant_a, app):
    reviewer = signed_in(app, conn, tenant_a["org"], "REVIEWER")
    broker = signed_in(app, conn, tenant_a["org"], "BROKER")
    d = tenant_a["draft"]
    assert broker.post(f"/drafts/{d}/approve", json={"approved_by": "Someone Else"}).status_code == 403
    r = reviewer.post(f"/drafts/{d}/approve", json={"approved_by": "Someone Else"})
    assert r.status_code == 200
    approved = [h for h in r.json()["history"] if h["event"] == "approved"][0]
    assert approved["actor"] == f"Test Reviewer <{reviewer.user['email']}>"
    ex = broker.get(f"/drafts/{d}/export")
    assert ex.status_code == 200 and ex.content[:2] == b"PK"
    events = {e["action"] for e in tenant_a["c"].get(f"/audit-events?resource_id={d}").json()["events"]}
    assert {"draft.approved", "draft.exported"} <= events


def test_audit_history_is_append_only(conn, tenant_a):
    import psycopg
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("UPDATE audit_events SET action = 'tampered' WHERE organization_id = %s", (tenant_a["org"],))
    conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_events WHERE organization_id = %s", (tenant_a["org"],))
    conn.rollback()
    ev = tenant_a["c"].get("/audit-events").json()["events"]
    assert ev and all("password" not in str(e["metadata"]).lower() for e in ev)


def test_login_failures_are_audited_without_secrets(conn, test_org, app):
    u = make_user(conn, test_org)
    TestClient(app).post("/auth/login", json={"email": u["email"], "password": "the wrong one!!"})
    with conn.cursor() as cur:
        cur.execute("SELECT action, metadata FROM audit_events WHERE actor_email = %s", (u["email"],))
        rows = cur.fetchall()
    assert any(r["action"] == "auth.login_failed" for r in rows)
    assert all("wrong" not in str(r["metadata"]) for r in rows)
