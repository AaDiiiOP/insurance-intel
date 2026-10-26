"""
Phase 2A live acceptance check — runs against a RUNNING Aurevia backend over
real HTTP (the Docker deployment, or any uvicorn), with synthetic data only.

    python3 phase2a_live_check.py [BASE_URL]          (default http://localhost:8000)

Needs an existing Owner (created with manage_users.py create-owner): the
e-mail from AUREVIA_CHECK_EMAIL (or asked), the password from
AUREVIA_CHECK_PASSWORD or asked for interactively (never on the command line).
Optional: DATABASE_URL (set inside the backend container) enables the
database schema check (v0.14.1), the session expiry / idle checks and the
"no secrets stored" database scan.

Everything is done inside a NEW throwaway organization the Owner creates, so
no existing client or document is touched. Prints PASS/FAIL per check and
exits 1 on any failure.
"""
import io
import os
import sys
import uuid

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
EMAIL = os.environ.get("AUREVIA_CHECK_EMAIL")
PASSWORD = os.environ.get("AUREVIA_CHECK_PASSWORD")
results = []
secrets_seen = []          # tokens and passwords that must never appear in audit data


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))


def client():
    return httpx.Client(base_url=BASE, timeout=60)


def login(c, email, password, org=None):
    r = c.post("/auth/login", json={"email": email, "password": password, "organization_id": org})
    if r.status_code == 200:
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r


def docx(text):
    from docx import Document
    d = Document()
    for line in text.splitlines():
        d.add_paragraph(line)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def invite_and_join(owner, role, org_label):
    email = f"check-{role.lower()}-{uuid.uuid4().hex[:6]}@example.test"
    pw = f"Check-{uuid.uuid4().hex}"
    inv = owner.post("/organization/invitations", json={"email": email, "role": role})
    assert inv.status_code == 201, inv.text
    token = inv.json()["token"]
    secrets_seen.extend([token, pw])
    acc = client().post("/auth/invitations/accept", json={"token": token, "display_name": f"Check {role}",
                                                          "password": pw})
    assert acc.status_code == 200, acc.text
    c = client()
    r = login(c, email, pw, org_label)
    assert r.status_code == 200, r.text
    c.email, c.password, c.token = email, pw, token
    return c


def main():
    global EMAIL, PASSWORD
    if not EMAIL:
        EMAIL = input("Owner e-mail: ").strip()
    if not PASSWORD:
        import getpass
        PASSWORD = getpass.getpass("Owner password: ")
    secrets_seen.append(PASSWORD)

    # --- 0. database schema (v0.14.1) ---------------------------------------------
    if os.environ.get("DATABASE_URL"):
        try:
            from repositories.connection import find_schema_drift
            drift = find_schema_drift()
            missing = drift["missing_tables"] + drift["missing_columns"]
            check("database has every table and column the code expects", not missing, ", ".join(missing))
        except ImportError:
            print("SKIP  schema check (run from the backend folder)")
    else:
        print("SKIP  schema check (no DATABASE_URL here)")

    # --- 1. unauthenticated access ------------------------------------------------
    anon = client()
    check("health is public", anon.get("/health").status_code == 200)
    denied = [("GET", "/clients"), ("GET", "/businesses"), ("GET", "/policies"), ("GET", "/knowledge/entities"),
              ("POST", "/knowledge/query"), ("GET", "/audit-events"), ("GET", "/organization/members"),
              ("POST", "/clients"), ("GET", "/carriers"), ("GET", "/doc-intelligence/submissions")]
    codes = {f"{m} {p}": anon.request(m, p, json={}).status_code for m, p in denied}
    check("unauthenticated requests are refused (401)", all(v == 401 for v in codes.values()), str(codes))

    # --- 2. sign-in ---------------------------------------------------------------
    owner = client()
    bad = owner.post("/auth/login", json={"email": EMAIL, "password": PASSWORD + "x"})
    unknown = owner.post("/auth/login", json={"email": "nobody-" + uuid.uuid4().hex[:6] + "@example.test",
                                              "password": "whatever-123456"})
    check("wrong password and unknown e-mail give the same 401",
          bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json())
    r = login(owner, EMAIL, PASSWORD)
    check("owner signs in", r.status_code == 200, r.text[:200])
    if r.status_code != 200:
        return finish()
    cookie = r.headers.get("set-cookie", "")
    check("session cookie is HttpOnly + SameSite=Lax", "httponly" in cookie.lower() and "samesite=lax" in cookie.lower())
    session_token = owner.cookies.get("aurevia_session")
    secrets_seen.append(session_token)
    home_org = r.json()["organization"]["organization_id"]

    # --- 3. a throwaway organization for everything below ----------------------------
    created = owner.post("/organizations", json={"name": f"Phase 2A check {uuid.uuid4().hex[:6]}"})
    check("owner creates a check organization", created.status_code == 200, created.text[:200])
    org_a = created.json()["organization_id"]
    sw = owner.post("/auth/switch-organization", json={"organization_id": org_a})
    check("switch organization (member)", sw.status_code == 200 and sw.json()["organization"]["organization_id"] == org_a)
    owner.headers["X-CSRF-Token"] = sw.json()["csrf_token"]
    check("switch organization (not a member) is refused",
          owner.post("/auth/switch-organization", json={"organization_id": str(uuid.uuid4())}).status_code == 404)
    check("header naming another organization is refused (403)",
          owner.get("/clients", headers={"X-Organization-Id": home_org}).status_code == 403)

    # --- 4. CSRF ------------------------------------------------------------------
    tok = owner.headers.pop("X-CSRF-Token")
    check("change without CSRF token is refused (403)",
          owner.post("/clients", json={"business_name": "No CSRF"}).status_code == 403)
    owner.headers["X-CSRF-Token"] = tok

    # --- 5. roles -------------------------------------------------------------------
    admin = invite_and_join(owner, "ADMIN", org_a)
    broker = invite_and_join(owner, "BROKER", org_a)
    reviewer = invite_and_join(owner, "REVIEWER", org_a)
    check("ADMIN cannot grant OWNER",
          admin.post("/organization/invitations", json={"email": "o@example.test", "role": "OWNER"}).status_code == 403)
    check("REVIEWER cannot create clients",
          reviewer.post("/clients", json={"business_name": "Reviewer Co"}).status_code == 403)
    cid = broker.post("/clients", json={"business_name": "Check Textiles Pvt Ltd", "industry": "textiles"}).json()["client_id"]
    check("BROKER creates clients", bool(cid))
    check("BROKER cannot delete clients", broker.delete(f"/clients/{cid}").status_code == 403)
    check("BROKER and REVIEWER cannot read audit history",
          broker.get("/audit-events").status_code == 403 and reviewer.get("/audit-events").status_code == 403)
    check("BROKER cannot manage members", broker.get("/organization/members").status_code == 403)
    check("REVIEWER can view clients", reviewer.get(f"/clients/{cid}").status_code == 200)

    # --- 6. clients -----------------------------------------------------------------
    for i in range(3):
        broker.post("/clients", json={"business_name": f"Check Foods {i}", "industry": "food"})
    check("search", owner.get("/clients?q=textiles").json()["total"] == 1)
    check("pagination", len(owner.get("/clients?page=1&page_size=2").json()["clients"]) == 2)
    upd = owner.put(f"/clients/{cid}", json={"business_name": "Check Textiles Pvt Ltd",
                                             "assigned_user_id": broker.get("/auth/me").json()["user"]["user_id"]})
    check("assign a broker", upd.status_code == 200 and broker.get("/clients?assigned_to=me").json()["total"] == 1)
    check("archive", owner.post(f"/clients/{cid}/archive").json().get("status") == "archived"
          and owner.get("/clients?status=archived").json()["total"] == 1)
    check("restore", owner.post(f"/clients/{cid}/restore").json().get("status") == "active")

    # --- 7. documents -----------------------------------------------------------------
    data = docx("Synthetic fire NOC for Check Textiles Pvt Ltd")
    up = broker.post(f"/clients/{cid}/documents/upload", data={"requirement": "Fire NOC"},
                     files={"file": ("noc.docx", data, "application/octet-stream")})
    check("upload client document", up.status_code == 200, up.text[:200])
    dup = broker.post(f"/clients/{cid}/documents/upload", data={"requirement": "Fire NOC"},
                      files={"file": ("again.docx", data, "application/octet-stream")})
    check("duplicate upload is refused (409)", dup.status_code == 409)
    if up.status_code != 200:
        print("      server said:", up.status_code, up.text[:300])
        print("      (the backend log has the full error: docker-compose logs --tail 80 backend)")
        return finish()
    did = up.json().get("document_id")
    dl = reviewer.get(f"/clients/{cid}/documents/{did}/download")
    check("authorized download returns the original", dl.status_code == 200 and dl.content == data
          and dl.headers.get("x-content-type-options") == "nosniff")
    pol = docx("Policy Number: SYN-CHECK-1\nPeriod: 01 April 2026 to 31 March 2027\n"
               "Property Damage Sum Insured: n100,000,000")
    intel = broker.post(f"/clients/{cid}/intelligence-documents",
                        files={"file": ("policy.docx", pol, "application/octet-stream")})
    check("upload intelligence document", intel.status_code == 201, intel.text[:200])
    check("bad file type refused (400)", broker.post(f"/clients/{cid}/intelligence-documents",
                                                     files={"file": ("x.exe", b"MZ", "application/x")}).status_code == 400)
    ws = owner.get(f"/clients/{cid}/workspace").json()
    check("workspace lists both documents", len(ws.get("documents", [])) == 2)

    # --- 8. existing intelligence endpoints -----------------------------------------------
    ok = all(owner.get(p).status_code == 200 for p in ("/businesses", "/policies", "/knowledge/entities",
                                                        "/doc-intelligence/submissions", "/carriers"))
    q = owner.post("/knowledge/query", json={"question": "What is the coverage limit?", "use_llm": False})
    check("intelligence endpoints work when signed in", ok and q.status_code == 200)

    # --- 9. cross-tenant ---------------------------------------------------------------
    created_b = owner.post("/organizations", json={"name": f"Phase 2A check B {uuid.uuid4().hex[:6]}"})
    org_b = created_b.json()["organization_id"]
    sw = owner.post("/auth/switch-organization", json={"organization_id": org_b})
    owner.headers["X-CSRF-Token"] = sw.json()["csrf_token"]
    intruder = invite_and_join(owner, "OWNER", org_b)
    probes = {
        "client": intruder.get(f"/clients/{cid}").status_code,
        "workspace": intruder.get(f"/clients/{cid}/workspace").status_code,
        "download": intruder.get(f"/clients/{cid}/documents/{did}/download").status_code,
        "intel download": intruder.get(f"/doc-intelligence/documents/{intel.json().get('document_id')}/download").status_code,
        "edit": intruder.put(f"/clients/{cid}", json={"business_name": "pwned"}).status_code,
        "archive": intruder.post(f"/clients/{cid}/archive").status_code,
        "delete": intruder.delete(f"/clients/{cid}").status_code,
    }
    listed = intruder.get("/clients?status=all").json()
    check("another organization's user gets 404 on every probe",
          all(v == 404 for v in probes.values()) and all(c["client_id"] != cid for c in listed["clients"]),
          str(probes))
    check("A's client unchanged", reviewer.get(f"/clients/{cid}").json()["business_name"] == "Check Textiles Pvt Ltd")

    # --- 10. rate limiting ------------------------------------------------------------------
    sw = owner.post("/auth/switch-organization", json={"organization_id": org_a})
    owner.headers["X-CSRF-Token"] = sw.json()["csrf_token"]
    target = invite_and_join(owner, "BROKER", org_a)
    rl = client()
    codes = [rl.post("/auth/login", json={"email": target.email, "password": "wrong-password-xx"}).status_code
             for _ in range(5)]
    right = rl.post("/auth/login", json={"email": target.email, "password": target.password}).status_code
    check("5 failed sign-ins then even the right password waits (429)", codes == [401] * 5 and right == 429,
          f"{codes} then {right}")

    # --- 11. logout / sessions --------------------------------------------------------------
    old = broker.cookies.get("aurevia_session")
    check("logout", broker.post("/auth/logout").status_code == 200)
    replay = client()
    replay.cookies.set("aurevia_session", old)
    check("a signed-out session cannot be reused", replay.get("/auth/me").status_code == 401)
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        import hashlib
        import psycopg
        with psycopg.connect(db_url) as conn:
            for label, sql in (("expired", "expires_at = now() - interval '1 minute'"),
                               ("idle", "last_seen_at = now() - interval '1 day'")):
                c = client()
                login(c, admin.email, admin.password, org_a)
                h = hashlib.sha256(c.cookies.get("aurevia_session").encode()).hexdigest()
                conn.execute(f"UPDATE auth_sessions SET {sql} WHERE token_hash = %s", (h,))
                conn.commit()
                check(f"{label} session is refused", c.get("/auth/me").status_code == 401)
    else:
        print("SKIP  session expiry / idle checks (no DATABASE_URL here)")

    # --- 12. audit -----------------------------------------------------------------------------
    ev = owner.get("/audit-events?limit=200").json()["events"]
    acts = {e["action"] for e in ev}
    need = {"membership.invited", "membership.invitation_accepted", "client.created", "client.updated",
            "client.archived", "client.restored", "document.uploaded", "document.downloaded"}
    check("audit events recorded", need <= acts, str(sorted(need - acts)))
    text = str(ev)
    check("audit API shows no password or token", not any(s and s in text for s in secrets_seen))
    if db_url:
        import psycopg
        with psycopg.connect(db_url) as conn:
            dump = " ".join(str(r) for r in conn.execute(
                "SELECT action, actor_email, resource_id, metadata::text FROM audit_events").fetchall())
            tokens = " ".join(str(r) for r in conn.execute(
                "SELECT token_hash FROM auth_tokens UNION ALL SELECT token_hash FROM auth_sessions").fetchall())
            users = " ".join(str(r) for r in conn.execute("SELECT password_hash FROM users").fetchall())
        check("database stores no password, session token or invitation code in clear",
              not any(s and (s in dump or s in tokens or s in users) for s in secrets_seen))
        check("passwords are argon2id hashes", users.count("$argon2id$") >= 5)
    else:
        print("SKIP  database secrets scan (no DATABASE_URL here)")
    return finish()


def finish():
    failed = [n for n, ok, _ in results if not ok]
    print(f"\nPhase 2A live check: {len(results)} checks, {len(results) - len(failed)} passed, {len(failed)} failed"
          + (" -> PASS" if not failed else " -> FAIL"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
