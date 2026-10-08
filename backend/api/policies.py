"""
Phase 1C — Policy Intelligence API (v0.11.0). Thin, organization-scoped
wrappers around policy_intelligence.py; a policy id from another organization
is a 404, exactly like a missing one. Questions about a policy go through the
existing POST /knowledge/query with entity_id = the policy id.
"""
import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import db
import policy_intelligence as pi
from api.deps import _resolve_organization, _validate_uuid
from api.knowledge import _json_safe

router = APIRouter()


def _as_of(value):
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD.")


@router.get("/policies")
def list_policies(x_organization_id: str | None = Header(None)):
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        return {"policies": _json_safe(pi.list_policies(conn, org))}


@router.get("/policies/{policy_id}")
def get_policy(policy_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    policy_id = _validate_uuid(policy_id, "policy id")
    day = _as_of(as_of)
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        out = pi.get_policy(conn, org, policy_id, as_of=day)
    if out is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    return _json_safe(out)


@router.get("/policies/{policy_id}/history")
def get_policy_history(policy_id: str, x_organization_id: str | None = Header(None)):
    policy_id = _validate_uuid(policy_id, "policy id")
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        out = pi.get_policy_history(conn, org, policy_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    return _json_safe(out)


@router.get("/policies/{policy_id}/conflicts")
def get_policy_conflicts(policy_id: str, x_organization_id: str | None = Header(None)):
    policy_id = _validate_uuid(policy_id, "policy id")
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        out = pi.find_policy_conflicts(conn, org, policy_id)
    if out is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    return {"conflicts": _json_safe(out)}


class CompareBody(BaseModel):
    left_policy_id: str
    right_policy_id: str | None = None
    as_of: str | None = None


@router.post("/policies/compare")
def compare(body: CompareBody, x_organization_id: str | None = Header(None)):
    left = _validate_uuid(body.left_policy_id, "left policy id")
    right = _validate_uuid(body.right_policy_id, "right policy id") if body.right_policy_id else None
    day = _as_of(body.as_of)
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        out = pi.compare_policies(conn, org, left, right, as_of=day)
    if out is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    return _json_safe(out)
