"""
Phase 1D — Business Risk Intelligence API (v0.12.0). Thin, organization-
scoped wrappers around business_intelligence.py; another organization's
business id is a 404 exactly like a missing one. Questions go through the
existing POST /knowledge/query (entity_id = the business id).
"""
import datetime

from fastapi import APIRouter, Header, HTTPException

import business_intelligence as bi
import db
from api.deps import _resolve_organization, _validate_uuid
from api.knowledge import _json_safe

router = APIRouter()
SECTIONS = ("locations", "assets", "operations", "exposures", "risks", "controls", "claims", "risk_factors")


def _date(value, label="as_of"):
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD.")


def _run(fn, business_id, x_organization_id, **kw):
    business_id = _validate_uuid(business_id, "business id")
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        out = fn(conn, org, business_id, **kw)
    if out is None:
        raise HTTPException(status_code=404, detail="Business not found.")
    return out


@router.get("/businesses")
def list_businesses(x_organization_id: str | None = Header(None)):
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        return {"businesses": _json_safe(bi.list_businesses(conn, org))}


@router.get("/businesses/{business_id}")
@router.get("/businesses/{business_id}/profile")
def get_profile(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _json_safe(_run(bi.get_business_profile, business_id, x_organization_id, as_of=_date(as_of)))


@router.get("/businesses/{business_id}/changes")
def get_changes(business_id: str, from_date: str | None = None, to_date: str | None = None,
                x_organization_id: str | None = Header(None)):
    return _json_safe(_run(bi.get_business_changes, business_id, x_organization_id,
                           from_date=_date(from_date, "from_date"), to_date=_date(to_date, "to_date")))


@router.get("/businesses/{business_id}/conflicts")
def get_conflicts(business_id: str, x_organization_id: str | None = Header(None)):
    return {"conflicts": _json_safe(_run(bi.find_business_conflicts, business_id, x_organization_id))}


@router.get("/businesses/{business_id}/graph")
def get_graph(business_id: str, x_organization_id: str | None = Header(None)):
    return _json_safe(_run(bi.get_business_graph, business_id, x_organization_id))


@router.get("/businesses/{business_id}/{section}")
def get_section(business_id: str, section: str, as_of: str | None = None,
                x_organization_id: str | None = Header(None)):
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown section. One of: {', '.join(SECTIONS)}.")
    prof = _run(bi.get_business_profile, business_id, x_organization_id, as_of=_date(as_of))
    return {section: _json_safe(prof[section]), "as_of": prof["as_of"]}
