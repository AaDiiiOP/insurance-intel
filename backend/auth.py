"""
Phase 2A — authentication, sessions, roles and permissions (v0.14.0).

Design (all server-side):
  * Passwords: argon2id via argon2-cffi (maintained library). Never stored or
    logged in plaintext. Unknown e-mails still run a hash verification so the
    response time does not reveal which e-mails exist.
  * Sessions: a random 256-bit token in an HttpOnly cookie; only its SHA-256
    is stored. A new token on every sign-in / organization switch / password
    change (no session fixation). Absolute expiry + idle timeout; revocable.
  * CSRF: every state-changing request must send X-CSRF-Token equal to the
    session's own random token (returned by /auth/me and /auth/login, never in
    a cookie), plus SameSite=Lax cookies.
  * Rate limiting: failed sign-ins counted per e-mail and per IP over a window.
  * The organization of every request comes from the session's verified,
    active membership. An X-Organization-Id header is accepted only if it
    names that same organization — it is never proof of access.
  * Permissions: explicit per role; every route is mapped (unknown
    state-changing routes are denied by default).
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

import db

# --------------------------------------------------------------------------
# Configuration (environment, never hard-coded secrets)
# --------------------------------------------------------------------------
COOKIE_NAME = "aurevia_session"
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
SESSION_HOURS = int(os.getenv("AUTH_SESSION_HOURS", "12"))
IDLE_MINUTES = int(os.getenv("AUTH_IDLE_MINUTES", "120"))
LOGIN_WINDOW_MINUTES = int(os.getenv("AUTH_LOGIN_WINDOW_MINUTES", "15"))
MAX_FAILS_PER_EMAIL = int(os.getenv("AUTH_MAX_FAILS_PER_EMAIL", "5"))
MAX_FAILS_PER_IP = int(os.getenv("AUTH_MAX_FAILS_PER_IP", "30"))
INVITE_HOURS = int(os.getenv("AUTH_INVITE_HOURS", "72"))
RESET_MINUTES = int(os.getenv("AUTH_RESET_MINUTES", "60"))
MIN_PASSWORD = 10

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("not-a-real-password-" + secrets.token_hex(8))

# --------------------------------------------------------------------------
# Roles and permissions
# --------------------------------------------------------------------------
ROLES = ("OWNER", "ADMIN", "BROKER", "REVIEWER")
PERMISSIONS = {
    "view": "See the organization's clients, documents and intelligence",
    "evidence.view": "Open source documents, document text and evidence",
    "clients.edit": "Create, edit, archive clients",
    "clients.delete": "Permanently delete a client",
    "documents.upload": "Upload documents",
    "documents.delete": "Delete documents",
    "analysis.run": "Run matching, gap analysis and document analysis",
    "carriers.edit": "Manage the carrier library",
    "facts.correct": "Verify or correct extracted facts",
    "findings.review": "Review coverage findings and gap items",
    "drafts.create": "Generate and edit drafts",
    "drafts.approve": "Approve or revoke drafts",
    "export": "Export documents",
    "users.manage": "Invite users, change roles, issue password resets",
    "org.manage": "Manage organization settings",
    "audit.view": "See the audit history",
}
ROLE_PERMISSIONS = {
    "OWNER": set(PERMISSIONS),
    "ADMIN": set(PERMISSIONS),
    "BROKER": {"view", "evidence.view", "clients.edit", "documents.upload", "documents.delete",
               "analysis.run", "carriers.edit", "drafts.create", "export"},
    "REVIEWER": {"view", "evidence.view", "facts.correct", "findings.review", "drafts.approve", "export"},
}

# Routes that need no session.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("POST", "/auth/login"),
    ("POST", "/auth/invitations/accept"),
    ("POST", "/auth/password-reset/complete"),
}
# Routes that need a session but no particular permission.
SESSION_ONLY_ROUTES = {
    ("GET", "/auth/me"), ("POST", "/auth/logout"), ("POST", "/auth/switch-organization"),
    ("POST", "/auth/change-password"), ("GET", "/organizations"),
}
# Explicit permission per route. GET routes not listed need "view";
# any state-changing route not listed is DENIED (fail closed).
ROUTE_PERMISSIONS = {
    ("POST", "/organizations"): "org.manage",
    ("POST", "/carriers"): "carriers.edit",
    ("DELETE", "/carriers/{carrier_id}"): "carriers.edit",
    ("POST", "/carriers/{carrier_id}/summary"): "carriers.edit",
    ("PUT", "/carriers/{carrier_id}/summary"): "carriers.edit",
    ("POST", "/reindex"): "org.manage",
    ("GET", "/documents/{document_id}/text"): "evidence.view",
    ("GET", "/documents/{document_id}/chunks"): "evidence.view",
    ("POST", "/clients"): "clients.edit",
    ("PUT", "/clients/{client_id}"): "clients.edit",
    ("DELETE", "/clients/{client_id}"): "clients.delete",
    ("POST", "/clients/{client_id}/archive"): "clients.edit",
    ("POST", "/clients/{client_id}/restore"): "clients.edit",
    ("POST", "/clients/{client_id}/link-business"): "clients.edit",
    ("POST", "/clients/{client_id}/link-submission"): "clients.edit",
    ("POST", "/clients/extract"): "clients.edit",
    ("POST", "/clients/{client_id}/documents/upload"): "documents.upload",
    ("POST", "/clients/{client_id}/intelligence-documents"): "documents.upload",
    ("DELETE", "/clients/{client_id}/documents/{document_id}"): "documents.delete",
    ("PUT", "/clients/{client_id}/documents"): "clients.edit",
    ("GET", "/clients/{client_id}/documents/{document_id}/download"): "evidence.view",
    ("GET", "/clients/{client_id}/intelligence"): "view",
    ("GET", "/doc-intelligence/documents/{document_id}/download"): "evidence.view",
    ("POST", "/clients/{client_id}/match"): "analysis.run",
    ("POST", "/clients/{client_id}/gaps"): "analysis.run",
    ("POST", "/clients/{client_id}/drafts"): "drafts.create",
    ("PUT", "/drafts/{draft_id}"): "drafts.create",
    ("DELETE", "/drafts/{draft_id}"): "drafts.create",
    ("POST", "/drafts/{draft_id}/approve"): "drafts.approve",
    ("POST", "/drafts/{draft_id}/revoke"): "drafts.approve",
    ("GET", "/drafts/{draft_id}/export"): "export",
    ("POST", "/house-style/{kind}/learn"): "drafts.create",
    ("PUT", "/house-style/{kind}"): "drafts.create",
    ("POST", "/clients/{client_id}/drafts/fill-form"): "drafts.create",
    ("PUT", "/gap-reports/{report_id}/item"): "findings.review",
    ("POST", "/chat"): "view",
    ("POST", "/doc-intelligence/submissions"): "documents.upload",
    ("POST", "/doc-intelligence/upload"): "documents.upload",
    ("POST", "/doc-intelligence/facts/{fact_id}/review"): "facts.correct",
    ("POST", "/knowledge/entities"): "facts.correct",
    ("POST", "/knowledge/facts/{fact_id}/review"): "facts.correct",
    ("PUT", "/knowledge/facts/{fact_id}"): "facts.correct",
    ("POST", "/knowledge/submissions/{submission_id}/analyze-property-policy"): "analysis.run",
    ("POST", "/knowledge/query"): "view",
    ("POST", "/policies/compare"): "view",
    ("POST", "/coverage-findings/{finding_id}/review"): "findings.review",
    ("POST", "/coverage-mappings/{mapping_id}/review"): "findings.review",
    ("GET", "/organization/members"): "users.manage",
    ("GET", "/organization/invitations"): "users.manage",
    ("POST", "/organization/invitations"): "users.manage",
    ("DELETE", "/organization/invitations/{invitation_id}"): "users.manage",
    ("PUT", "/organization/members/{user_id}"): "users.manage",
    ("POST", "/organization/members/{user_id}/password-reset"): "users.manage",
    ("GET", "/organization/users"): "view",
    ("GET", "/audit-events"): "audit.view",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def permission_for(method: str, path: str) -> str | None:
    """'public' / 'session' / a permission name / None (= denied)."""
    if (method, path) in PUBLIC_ROUTES:
        return "public"
    if (method, path) in SESSION_ONLY_ROUTES:
        return "session"
    if (method, path) in ROUTE_PERMISSIONS:
        return ROUTE_PERMISSIONS[(method, path)]
    if method in SAFE_METHODS:
        return "view"
    return None


# --------------------------------------------------------------------------
# Request context
# --------------------------------------------------------------------------
@dataclass
class AuthContext:
    user_id: str
    email: str
    display_name: str
    organization_id: str
    role: str
    session_id: str
    csrf_token: str
    ip: str | None = None
    permissions: set = field(default_factory=set)

    @property
    def actor(self) -> str:
        return f"{self.display_name} <{self.email}>"

    def can(self, permission: str) -> bool:
        return permission in self.permissions


_current: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar("aurevia_auth", default=None)


def current() -> AuthContext | None:
    return _current.get()


def require_context() -> AuthContext:
    ctx = _current.get()
    if ctx is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return ctx


def require_permission(permission: str) -> AuthContext:
    ctx = require_context()
    if not ctx.can(permission):
        raise HTTPException(status_code=403, detail="Your role does not allow this action.")
    return ctx


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{1,63}", e):
        raise HTTPException(status_code=400, detail="Enter a valid e-mail address.")
    return e


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 256:
        raise HTTPException(status_code=400, detail="Password is too long.")


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password or "")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def audit(conn, ctx: AuthContext | None, action: str, resource_type: str | None = None,
          resource_id=None, metadata: dict | None = None, organization_id: str | None = None,
          actor_email: str | None = None) -> None:
    """Record one audit event. Never pass passwords, tokens or document text."""
    db.add_audit_event(conn, organization_id or (ctx.organization_id if ctx else None),
                       ctx.user_id if ctx else None, actor_email or (ctx.email if ctx else None),
                       action, resource_type, resource_id, metadata or {}, ctx.ip if ctx else None)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def create_session(conn, user: dict, organization_id: str, request: Request | None) -> tuple[str, dict]:
    token = secrets.token_urlsafe(32)
    row = db.create_session(conn, token_hash(token), str(user["id"]), organization_id,
                            secrets.token_urlsafe(32), _now() + timedelta(hours=SESSION_HOURS),
                            client_ip(request), request.headers.get("user-agent") if request else None)
    return token, row


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS * 3600, httponly=True,
                        secure=COOKIE_SECURE, samesite="lax", path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")


def _load_context(token: str | None, ip: str | None) -> AuthContext | None:
    if not token:
        return None
    with db.get_connection() as conn:
        s = db.get_session_by_hash(conn, token_hash(token))
        if s is None or s["revoked_at"] is not None or s["expires_at"] <= _now():
            return None
        if s["last_seen_at"] + timedelta(minutes=IDLE_MINUTES) <= _now():
            db.revoke_session(conn, str(s["id"]))
            return None
        user = db.get_user(conn, str(s["user_id"]))
        m = db.get_membership(conn, str(s["organization_id"]), str(s["user_id"]))
        if user is None or not user["is_active"] or m is None or m["status"] != "active":
            return None
        if s["created_at"] < user["password_changed_at"]:
            return None                       # password changed after this session began
        if (_now() - s["last_seen_at"]).total_seconds() > 60:
            db.touch_session(conn, str(s["id"]))
    return AuthContext(user_id=str(user["id"]), email=user["email"], display_name=user["display_name"],
                       organization_id=str(s["organization_id"]), role=m["role"], session_id=str(s["id"]),
                       csrf_token=s["csrf_token"], ip=ip, permissions=set(ROLE_PERMISSIONS[m["role"]]))


async def guard(request: Request):
    """Global FastAPI dependency: authenticate, check CSRF and permission,
    and publish the AuthContext for the request."""
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    method = request.method.upper()
    need = permission_for(method, path)
    if path in ("/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect") and \
            os.getenv("AUREVIA_ENV", "development") != "production":
        need = "public"
    _current.set(None)
    if need == "public":
        return None
    ctx = await run_in_threadpool(_load_context, request.cookies.get(COOKIE_NAME), client_ip(request))
    if ctx is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    if method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token") or ""
        if not hmac.compare_digest(sent, ctx.csrf_token):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")
    header_org = request.headers.get("x-organization-id")
    if header_org and header_org != ctx.organization_id:
        raise HTTPException(status_code=403, detail="That organization is not the one you are signed in to.")
    if need is None:
        raise HTTPException(status_code=403, detail="This action is not permitted.")
    if need not in ("session",) and not ctx.can(need):
        raise HTTPException(status_code=403, detail="Your role does not allow this action.")
    _current.set(ctx)
    return ctx


def check_login_rate(conn, email: str | None, ip: str | None) -> None:
    by_email, by_ip = db.recent_failed_attempts(conn, email, ip, LOGIN_WINDOW_MINUTES)
    if by_email >= MAX_FAILS_PER_EMAIL or by_ip >= MAX_FAILS_PER_IP:
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts. Try again later.")


def new_one_time_token() -> tuple[str, str]:
    t = secrets.token_urlsafe(32)
    return t, token_hash(t)


def public_context(ctx: AuthContext, conn) -> dict:
    org = db.get_organization(conn, ctx.organization_id)
    return {"user": {"user_id": ctx.user_id, "email": ctx.email, "display_name": ctx.display_name},
            "organization": {"organization_id": ctx.organization_id, "name": org["name"] if org else None},
            "role": ctx.role, "permissions": sorted(ctx.permissions), "csrf_token": ctx.csrf_token,
            "memberships": [{"organization_id": str(m["organization_id"]), "name": m["organization_name"],
                             "role": m["role"], "status": m["status"]}
                            for m in db.list_user_memberships(conn, ctx.user_id)]}
