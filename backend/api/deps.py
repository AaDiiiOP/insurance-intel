"""
Shared helpers used across the api/ routers: id validation and the
per-request organization resolver.
"""
import uuid

from fastapi import HTTPException



def _validate_uuid(value: str, label: str) -> str:
    """Reject a malformed id with 400 rather than letting Postgres raise."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {label}.")


def _resolve_organization(conn, x_organization_id: str | None = None) -> str:
    """
    Which organization the caller is acting as — Phase 2A (v0.14.0).

    Comes ONLY from the signed-in session's verified, active membership
    (auth.guard, a global dependency, sets it for every request). The
    X-Organization-Id header is no longer proof of anything: auth.guard
    rejects a header that names a different organization (403), and here it
    is ignored. Without a session this fails closed with 401, so an endpoint
    can never fall back to a default organization.

    Still application-level isolation (every query filters on
    organization_id), not PostgreSQL row-level security.
    """
    import auth
    return auth.require_context().organization_id


def _actor() -> str:
    """The signed-in person, for audit/approval records (never a typed name)."""
    import auth
    return auth.require_context().actor
