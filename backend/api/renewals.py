"""Phase 2D — renewal view and submission packages (v0.17.0). Organization and
actor come only from the session; another organization's ids are 404."""
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

import auth
import db
import renewal_submission as rs
from api.knowledge import _json_safe

from .deps import _resolve_organization, _validate_uuid

router = APIRouter()


def _run(fn, *args):
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _resolve_organization(conn)
        try:
            out = fn(conn, ctx, *args)
        except rs.PackageError as e:
            if e.status == 409:
                conn.commit()          # keep the recorded refusal / state change
            else:
                conn.rollback()
            raise HTTPException(status_code=e.status, detail=e.detail)
    return out


class ReasonBody(BaseModel):
    reason: str | None = None


class ExceptionBody(BaseModel):
    check_id: str
    reason: str | None = None


@router.get("/clients/{client_id}/renewal")
def renewal(client_id: str):
    cid = _validate_uuid(client_id, "client id")
    return _json_safe(_run(lambda conn, ctx: rs.get_renewal(conn, ctx.organization_id, cid)))


@router.get("/clients/{client_id}/submission-packages")
def packages(client_id: str):
    cid = _validate_uuid(client_id, "client id")
    return _json_safe(_run(rs.list_for_client, cid))


@router.post("/clients/{client_id}/submission-packages", status_code=201)
def create(client_id: str):
    cid = _validate_uuid(client_id, "client id")
    return _json_safe(_run(rs.create, cid))


@router.get("/submission-packages/{package_id}")
def get_package(package_id: str):
    return _json_safe(_run(rs.detail, _validate_uuid(package_id, "package id")))


@router.post("/submission-packages/{package_id}/exceptions")
def add_exception(package_id: str, body: ExceptionBody):
    return _json_safe(_run(rs.add_exception, _validate_uuid(package_id, "package id"), body.check_id, body.reason))


@router.post("/submission-packages/{package_id}/{action}")
def act(package_id: str, action: str, body: ReasonBody | None = None):
    if action not in ("check", "approve", "reopen"):
        raise HTTPException(status_code=404, detail="Unknown action.")
    return _json_safe(_run(rs.transition, _validate_uuid(package_id, "package id"), action,
                           body.reason if body else None))


@router.get("/submission-packages/{package_id}/export")
def export(package_id: str):
    data, name = _run(rs.docx_bytes, _validate_uuid(package_id, "package id"))
    return Response(content=data, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                           "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
