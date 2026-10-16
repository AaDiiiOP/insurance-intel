"""
Phase 2A — sign-in, sessions, organization members, invitations, password
resets and audit history (v0.14.0). Business logic lives in auth.py.

No e-mail is ever sent: an invitation or reset link is shown once to the
Owner/Admin who created it, who passes it on. Tokens are single-use,
time-limited and stored only as hashes.
"""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import auth
import db
from api.deps import _validate_uuid

router = APIRouter()


class LoginBody(BaseModel):
    email: str
    password: str
    organization_id: str | None = None


class SwitchBody(BaseModel):
    organization_id: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class InviteBody(BaseModel):
    email: str
    role: str


class AcceptBody(BaseModel):
    token: str
    display_name: str | None = None
    password: str


class ResetBody(BaseModel):
    token: str
    password: str


class MemberBody(BaseModel):
    role: str | None = None
    status: str | None = None


class OrgBody(BaseModel):
    name: str


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = auth.client_ip(request)
    email = (body.email or "").strip().lower()[:320]
    with db.get_connection() as conn:
        auth.check_login_rate(conn, email, ip)
        user = db.get_user_by_email(conn, email) if email else None
        ok = auth.verify_password(user["password_hash"] if user else None, body.password)
        if not ok or user is None or not user["is_active"]:
            db.record_login_attempt(conn, email, ip, False)
            db.add_audit_event(conn, None, None, email, "auth.login_failed", "user", None, {}, ip)
            conn.commit()
            raise HTTPException(status_code=401, detail="Incorrect e-mail or password.")
        members = [m for m in db.list_user_memberships(conn, str(user["id"])) if m["status"] == "active"]
        if body.organization_id:
            members = [m for m in members if str(m["organization_id"]) == body.organization_id]
        if not members:
            db.record_login_attempt(conn, email, ip, False)
            conn.commit()
            raise HTTPException(status_code=403, detail="You have no active membership in that organization.")
        org_id = str(members[0]["organization_id"])
        db.record_login_attempt(conn, email, ip, True)
        token, s = auth.create_session(conn, user, org_id, request)
        db.add_audit_event(conn, org_id, str(user["id"]), email, "auth.login", "session", str(s["id"]), {}, ip)
    auth.set_session_cookie(response, token)
    with db.get_connection() as conn:
        ctx = auth._load_context(token, ip)
        return auth.public_context(ctx, conn)


@router.post("/auth/logout")
def logout(response: Response):
    ctx = auth.require_context()
    with db.get_connection() as conn:
        db.revoke_session(conn, ctx.session_id)
        auth.audit(conn, ctx, "auth.logout", "session", ctx.session_id)
    auth.clear_session_cookie(response)
    return {"signed_out": True}


@router.get("/auth/me")
def me():
    ctx = auth.require_context()
    with db.get_connection() as conn:
        return auth.public_context(ctx, conn)


@router.post("/auth/switch-organization")
def switch_organization(body: SwitchBody, request: Request, response: Response):
    ctx = auth.require_context()
    org_id = _validate_uuid(body.organization_id, "organization id")
    with db.get_connection() as conn:
        m = db.get_membership(conn, org_id, ctx.user_id)
        if m is None or m["status"] != "active":
            raise HTTPException(status_code=404, detail="Organization not found.")
        user = db.get_user(conn, ctx.user_id)
        db.revoke_session(conn, ctx.session_id)
        token, s = auth.create_session(conn, user, org_id, request)
        auth.audit(conn, ctx, "auth.switch_organization", "organization", org_id, organization_id=org_id)
    auth.set_session_cookie(response, token)
    with db.get_connection() as conn:
        return auth.public_context(auth._load_context(token, ctx.ip), conn)


@router.post("/auth/change-password")
def change_password(body: PasswordBody, request: Request, response: Response):
    ctx = auth.require_context()
    with db.get_connection() as conn:
        user = db.get_user(conn, ctx.user_id)
        if not auth.verify_password(user["password_hash"], body.current_password):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
        db.set_user_password(conn, ctx.user_id, auth.hash_password(body.new_password))
        db.revoke_user_sessions(conn, ctx.user_id)
        auth.audit(conn, ctx, "auth.password_changed", "user", ctx.user_id)
        conn.commit()
        user = db.get_user(conn, ctx.user_id)
        token, _ = auth.create_session(conn, user, ctx.organization_id, request)
    auth.set_session_cookie(response, token)
    return {"changed": True, "other_sessions_signed_out": True}


@router.post("/auth/invitations/accept")
def accept_invitation(body: AcceptBody, request: Request):
    ip = auth.client_ip(request)
    with db.get_connection() as conn:
        auth.check_login_rate(conn, None, ip)
        tok = db.consume_auth_token(conn, "invite", auth.token_hash(body.token or ""))
        if tok is None:
            db.record_login_attempt(conn, None, ip, False)
            conn.commit()
            raise HTTPException(status_code=400, detail="This invitation is invalid, expired or already used.")
        user = db.get_user_by_email(conn, tok["email"])
        if user is None:
            name = " ".join((body.display_name or "").split())[:120]
            if not name:
                conn.rollback()
                raise HTTPException(status_code=400, detail="Enter your name.")
            user = db.create_user(conn, tok["email"], name, auth.hash_password(body.password))
        elif not auth.verify_password(user["password_hash"], body.password):
            conn.rollback()           # the token stays unused
            raise HTTPException(status_code=400, detail="This e-mail already has an account; enter its password.")
        db.add_membership(conn, str(tok["organization_id"]), str(user["id"]), tok["role"])
        db.add_audit_event(conn, str(tok["organization_id"]), str(user["id"]), user["email"],
                           "membership.invitation_accepted", "user", str(user["id"]), {"role": tok["role"]}, ip)
    return {"accepted": True, "email": user["email"], "role": tok["role"]}


@router.post("/auth/password-reset/complete")
def complete_reset(body: ResetBody, request: Request):
    ip = auth.client_ip(request)
    with db.get_connection() as conn:
        auth.check_login_rate(conn, None, ip)
        new_hash = auth.hash_password(body.password)
        tok = db.consume_auth_token(conn, "password_reset", auth.token_hash(body.token or ""))
        if tok is None or tok["user_id"] is None:
            db.record_login_attempt(conn, None, ip, False)
            conn.commit()
            raise HTTPException(status_code=400, detail="This reset link is invalid, expired or already used.")
        db.set_user_password(conn, str(tok["user_id"]), new_hash)
        db.revoke_user_sessions(conn, str(tok["user_id"]))
        db.add_audit_event(conn, str(tok["organization_id"]), str(tok["user_id"]), tok["email"],
                           "auth.password_reset_completed", "user", str(tok["user_id"]), {}, ip)
    return {"reset": True}


# --- organization administration ------------------------------------------------------

@router.get("/organizations")
def my_organizations():
    ctx = auth.require_context()
    with db.get_connection() as conn:
        return {"organizations": [{"organization_id": str(m["organization_id"]), "name": m["organization_name"],
                                   "role": m["role"], "status": m["status"]}
                                  for m in db.list_user_memberships(conn, ctx.user_id)]}


@router.post("/organizations")
def create_organization(body: OrgBody):
    ctx = auth.require_permission("org.manage")
    name = " ".join((body.name or "").split())[:200]
    if not name:
        raise HTTPException(status_code=400, detail="Give the organization a name.")
    with db.get_connection() as conn:
        org = db.create_organization(conn, name)
        db.add_membership(conn, str(org["id"]), ctx.user_id, "OWNER")
        auth.audit(conn, ctx, "organization.created", "organization", str(org["id"]),
                   organization_id=str(org["id"]))
    return {"organization_id": str(org["id"]), "name": org["name"], "role": "OWNER"}


@router.get("/organization/members")
def members():
    ctx = auth.require_permission("users.manage")
    with db.get_connection() as conn:
        rows = db.list_org_members(conn, ctx.organization_id)
    return {"members": [{"user_id": str(r["user_id"]), "email": r["email"], "display_name": r["display_name"],
                         "role": r["role"], "status": r["status"]} for r in rows]}


@router.get("/organization/users")
def assignable_users():
    """Names of active members, for assigning clients and review items."""
    ctx = auth.require_permission("view")
    with db.get_connection() as conn:
        rows = db.list_org_members(conn, ctx.organization_id)
    return {"users": [{"user_id": str(r["user_id"]), "display_name": r["display_name"], "role": r["role"]}
                      for r in rows if r["status"] == "active"]}


def _check_role_change(ctx, target_role: str | None, current_role: str | None):
    if target_role is not None and target_role not in auth.ROLES:
        raise HTTPException(status_code=400, detail="Unknown role.")
    if ctx.role != "OWNER" and ("OWNER" in (target_role, current_role)):
        raise HTTPException(status_code=403, detail="Only an Owner can grant or change the Owner role.")


@router.get("/organization/invitations")
def list_invitations():
    ctx = auth.require_permission("users.manage")
    with db.get_connection() as conn:
        rows = db.list_open_invitations(conn, ctx.organization_id)
    return {"invitations": [{"invitation_id": str(r["id"]), "email": r["email"], "role": r["role"],
                             "expires_at": r["expires_at"].isoformat()} for r in rows]}


@router.post("/organization/invitations", status_code=201)
def invite(body: InviteBody):
    ctx = auth.require_permission("users.manage")
    email = auth.normalize_email(body.email)
    _check_role_change(ctx, body.role, None)
    from datetime import datetime, timedelta, timezone
    token, h = auth.new_one_time_token()
    with db.get_connection() as conn:
        existing = db.get_user_by_email(conn, email)
        if existing and db.get_membership(conn, ctx.organization_id, str(existing["id"])):
            raise HTTPException(status_code=409, detail="That person is already a member.")
        row = db.create_auth_token(conn, "invite", h, ctx.organization_id, email, body.role, None, ctx.user_id,
                                   datetime.now(timezone.utc) + timedelta(hours=auth.INVITE_HOURS))
        auth.audit(conn, ctx, "membership.invited", "invitation", str(row["id"]),
                   {"email": email, "role": body.role})
    return {"invitation_id": str(row["id"]), "email": email, "role": body.role,
            "expires_at": row["expires_at"].isoformat(), "token": token,
            "note": "Shown once. Give this code to the person; it works once and expires."}


@router.delete("/organization/invitations/{invitation_id}")
def revoke_invitation(invitation_id: str):
    ctx = auth.require_permission("users.manage")
    invitation_id = _validate_uuid(invitation_id, "invitation id")
    with db.get_connection() as conn:
        row = db.revoke_auth_token(conn, ctx.organization_id, invitation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Invitation not found.")
        auth.audit(conn, ctx, "membership.invitation_revoked", "invitation", invitation_id)
    return {"revoked": True}


@router.put("/organization/members/{user_id}")
def update_member(user_id: str, body: MemberBody):
    ctx = auth.require_permission("users.manage")
    user_id = _validate_uuid(user_id, "user id")
    if body.status is not None and body.status not in ("active", "suspended"):
        raise HTTPException(status_code=400, detail="Status must be active or suspended.")
    with db.get_connection() as conn:
        m = db.get_membership(conn, ctx.organization_id, user_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Member not found.")
        _check_role_change(ctx, body.role, m["role"])
        losing_owner = m["role"] == "OWNER" and ((body.role and body.role != "OWNER") or body.status == "suspended")
        if losing_owner and db.count_active_owners(conn, ctx.organization_id) <= 1:
            raise HTTPException(status_code=400, detail="The organization must keep at least one active Owner.")
        row = db.update_membership(conn, ctx.organization_id, user_id, body.role, body.status)
        if body.status == "suspended" or (body.role and body.role != m["role"]):
            db.revoke_user_sessions(conn, user_id, ctx.organization_id)
        auth.audit(conn, ctx, "membership.updated", "user", user_id,
                   {"from_role": m["role"], "to_role": row["role"], "from_status": m["status"],
                    "to_status": row["status"]})
    return {"user_id": user_id, "role": row["role"], "status": row["status"]}


@router.post("/organization/members/{user_id}/password-reset", status_code=201)
def issue_reset(user_id: str):
    ctx = auth.require_permission("users.manage")
    user_id = _validate_uuid(user_id, "user id")
    from datetime import datetime, timedelta, timezone
    with db.get_connection() as conn:
        m = db.get_membership(conn, ctx.organization_id, user_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Member not found.")
        _check_role_change(ctx, None, m["role"])
        user = db.get_user(conn, user_id)
        token, h = auth.new_one_time_token()
        row = db.create_auth_token(conn, "password_reset", h, ctx.organization_id, user["email"], None, user_id,
                                   ctx.user_id, datetime.now(timezone.utc) + timedelta(minutes=auth.RESET_MINUTES))
        auth.audit(conn, ctx, "auth.password_reset_issued", "user", user_id)
    return {"token": token, "expires_at": row["expires_at"].isoformat(),
            "note": "Shown once. Give this code to the member; it works once and expires."}


@router.get("/audit-events")
def audit_events(limit: int = 50, offset: int = 0, resource_type: str | None = None,
                 resource_id: str | None = None, action: str | None = None):
    ctx = auth.require_permission("audit.view")
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    with db.get_connection() as conn:
        rows = db.list_audit_events(conn, ctx.organization_id, limit, offset, resource_type, resource_id, action)
    return {"events": [{"id": r["id"], "action": r["action"], "actor": r["actor_email"],
                        "resource_type": r["resource_type"], "resource_id": r["resource_id"],
                        "metadata": r["metadata"], "at": r["created_at"].isoformat()} for r in rows],
            "limit": limit, "offset": offset}
