"""Phase 2C — review queue and review workflow API (v0.16.0).

Organization and reviewer come only from the signed-in session; every item and
everything it links to is looked up inside that organization (another
organization's item id is a 404). See review_workflow.py for the lifecycle."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import auth
import db
import review_workflow as rw
from api.knowledge import _json_safe

from .deps import _resolve_organization, _validate_uuid

router = APIRouter()


def _run(fn, *args, **kwargs):
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _resolve_organization(conn)          # the session's organization (header cannot change it)
        try:
            out = fn(conn, ctx, *args, **kwargs)
        except rw.ReviewError as e:
            conn.rollback()
            raise HTTPException(status_code=e.status, detail=e.detail)
    return _json_safe(out)


@router.get("/review-queue")
def review_queue(kind: str | None = None, state: str | None = None, assigned: str | None = None,
                 client_id: str | None = None, page: int = 1, page_size: int = 50):
    if kind and kind not in rw.KINDS:
        raise HTTPException(status_code=400, detail="Unknown kind.")
    if state and state not in rw.STATES + ("open", "closed"):
        raise HTTPException(status_code=400, detail="Unknown state.")
    if assigned and assigned not in ("me", "unassigned"):
        raise HTTPException(status_code=400, detail="assigned must be 'me' or 'unassigned'.")
    if client_id:
        client_id = _validate_uuid(client_id, "client id")
    page, page_size = max(1, page), min(max(1, page_size), 200)

    def go(conn, ctx):
        stats = rw.refresh(conn, ctx.organization_id)
        out = rw.list_queue(conn, ctx, kind=kind, state=state, assigned=assigned, client_id=client_id,
                            page=page, page_size=page_size)
        out["refresh"] = stats
        out["can_assign"] = ctx.can("reviews.assign")
        return out
    return _run(go)


@router.get("/review-items/{item_id}")
def review_item(item_id: str):
    item_id = _validate_uuid(item_id, "review item id")
    return _run(rw.get_detail, item_id)


class AssignBody(BaseModel):
    user_id: str | None = None


class ReasonBody(BaseModel):
    reason: str | None = None


class DecideBody(BaseModel):
    decision: str
    reason: str | None = None
    value: str | None = None
    fact_id: str | None = None


@router.post("/review-items/{item_id}/assign")
def assign(item_id: str, body: AssignBody):
    item_id = _validate_uuid(item_id, "review item id")
    uid = _validate_uuid(body.user_id, "user id") if body.user_id else None
    return _run(rw.assign, item_id, uid)


@router.post("/review-items/{item_id}/start")
def start(item_id: str):
    item_id = _validate_uuid(item_id, "review item id")
    return _run(rw.start, item_id)


@router.post("/review-items/{item_id}/needs-info")
def needs_info(item_id: str, body: ReasonBody):
    item_id = _validate_uuid(item_id, "review item id")
    return _run(rw.needs_info, item_id, body.reason)


@router.post("/review-items/{item_id}/decide")
def decide(item_id: str, body: DecideBody):
    item_id = _validate_uuid(item_id, "review item id")
    fid = _validate_uuid(body.fact_id, "fact id") if body.fact_id else None
    return _run(rw.decide, item_id, body.decision, body.reason, body.value, fid)


@router.post("/review-items/{item_id}/reopen")
def reopen(item_id: str, body: ReasonBody):
    item_id = _validate_uuid(item_id, "review item id")
    return _run(rw.reopen, item_id, body.reason)
