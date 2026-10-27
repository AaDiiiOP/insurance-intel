"""
Phase 1E — Risk → Coverage Intelligence API (v0.13.0). Organization-scoped
wrappers around coverage_intelligence.py; another organization's business id
(or finding / mapping id) is a 404 exactly like a missing one.

Registered BEFORE api/businesses.py in main.py, because that router has a
catch-all /businesses/{id}/{section}.
"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import auth
import coverage_intelligence as ci
import db
import review_workflow
from api.businesses import _date
from api.deps import _actor, _resolve_organization, _validate_uuid
from api.knowledge import _json_safe

router = APIRouter()


def _run(fn, business_id, x_organization_id, **kw):
    business_id = _validate_uuid(business_id, "business id")
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        out = fn(conn, org, business_id, **kw)
    if out is None:
        raise HTTPException(status_code=404, detail="Business not found.")
    return _json_safe(out)


@router.get("/businesses/{business_id}/coverage-map")
def coverage_map(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _run(ci.coverage_map_view, business_id, x_organization_id, as_of=_date(as_of))


@router.get("/businesses/{business_id}/coverage-findings")
def coverage_findings(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _run(ci.get_coverage_findings, business_id, x_organization_id, as_of=_date(as_of))


@router.get("/businesses/{business_id}/unmapped-risks")
def unmapped(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _run(ci.unmapped_risks, business_id, x_organization_id, as_of=_date(as_of))


@router.get("/businesses/{business_id}/coverage-conflicts")
def conflicts(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _run(ci.coverage_conflicts, business_id, x_organization_id, as_of=_date(as_of))


@router.get("/businesses/{business_id}/coverage-changes")
def changes(business_id: str, from_date: str | None = None, to_date: str | None = None,
            x_organization_id: str | None = Header(None)):
    return _run(ci.get_coverage_changes, business_id, x_organization_id,
                from_date=_date(from_date, "from_date"), to_date=_date(to_date, "to_date"))


@router.get("/businesses/{business_id}/coverage-audit")
def audit(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _run(ci.get_coverage_audit, business_id, x_organization_id, as_of=_date(as_of))


@router.get("/businesses/{business_id}/coverage-graph")
def graph(business_id: str, as_of: str | None = None, x_organization_id: str | None = Header(None)):
    return _run(ci.get_coverage_graph, business_id, x_organization_id, as_of=_date(as_of))


class ReviewBody(BaseModel):
    business_id: str
    decision: str
    reviewer: str | None = None   # ignored: the signed-in user is the reviewer
    comment: str | None = None


def _review(subject_type, subject_id, body: ReviewBody, x_organization_id):
    subject_id = _validate_uuid(subject_id, f"{subject_type} id")
    business_id = _validate_uuid(body.business_id, "business id")
    with db.get_connection() as conn:
        org = _resolve_organization(conn, x_organization_id)
        comment = " ".join((body.comment or "").split())[:1000] or None
        if body.decision == "REJECTED" and not comment:
            raise HTTPException(status_code=400, detail="A reason is required to reject (dismiss) a finding.")
        try:
            out = ci.review(conn, org, business_id, subject_type, subject_id, body.decision, _actor(),
                            comment)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if out is not None:
            review_workflow.audit_coverage(conn, auth.current(), subject_type, subject_id, business_id,
                                           body.decision, comment)
    if out is None:
        raise HTTPException(status_code=404, detail=f"No current {subject_type} with that id for this business.")
    return _json_safe(out)


@router.post("/coverage-findings/{finding_id}/review")
def review_finding(finding_id: str, body: ReviewBody, x_organization_id: str | None = Header(None)):
    return _review("finding", finding_id, body, x_organization_id)


@router.post("/coverage-mappings/{mapping_id}/review")
def review_mapping(mapping_id: str, body: ReviewBody, x_organization_id: str | None = Header(None)):
    return _review("mapping", mapping_id, body, x_organization_id)
