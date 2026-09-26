"""
Shared helpers used across the api/ routers: id validation and the
per-request organization resolver.
"""
import uuid

from fastapi import HTTPException

import db


def _validate_uuid(value: str, label: str) -> str:
    """Reject a malformed id with 400 rather than letting Postgres raise."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {label}.")


def _resolve_organization(conn, x_organization_id: str | None) -> str:
    """
    Which organization the caller is acting as.

    Reads the X-Organization-Id header if given and confirms it actually
    exists; falls back to the pilot-stage default organization when no
    header is sent, so every existing call site and the current UI (which
    sends no header) keep working exactly as before. This is the ONLY
    place in the app that decides "who is asking" — every doc-intelligence
    endpoint calls this first and passes the result to every db function
    that reads or writes org-scoped data, rather than each endpoint
    re-deciding it independently.

    This is still application-level scoping, not database-enforced
    isolation (no Postgres row-level security) — a caller who sends
    ANY organization_id, including one that is not really theirs, is
    trusted. That is the documented pilot-stage boundary, not a gap
    introduced here; what this function adds is that the boundary is now
    actually enforced at every read and write, instead of not existing at
    all.
    """
    if not x_organization_id:
        return db.get_or_create_default_organization(conn)

    organization_id = _validate_uuid(x_organization_id, "X-Organization-Id header")
    org = db.get_organization(conn, organization_id)
    if not org:
        raise HTTPException(
            status_code=404,
            detail=f"No organization with id {organization_id}.",
        )
    return organization_id
