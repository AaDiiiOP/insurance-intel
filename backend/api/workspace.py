"""
Phase 2A — broker workspace endpoints around a client (v0.14.0): archive /
restore, link the client to its Knowledge Core business, attach intelligence
submissions, upload documents for intelligence, one document library, and
authorized download of original files.

Everything is organization-scoped through the signed-in session; another
organization's id is a 404. The existing ingestion pipeline (parsers, quality
check, Knowledge Core analysis) is reused unchanged.
"""
import hashlib
import re

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from pydantic import BaseModel

import auth
import db
from parsers import ParseError, extension_of, parse_file_with_quality

from .deps import _resolve_organization, _validate_uuid

router = APIRouter()

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def content_type_for(filename: str) -> str:
    return CONTENT_TYPES.get(extension_of(filename), "application/octet-stream")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client_or_404(conn, client_id: str, org: str) -> dict:
    row = db.get_client(conn, client_id, org)
    if not row:
        raise HTTPException(status_code=404, detail="Client not found.")
    return row


def _download(row: dict) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", row["filename"])[:150] or "document"
    return Response(content=bytes(row["data"]), media_type=row["content_type"],
                    headers={"Content-Disposition": f'attachment; filename="{safe}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post("/clients/{client_id}/archive")
def archive_client(client_id: str):
    client_id = _validate_uuid(client_id, "client id")
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _client_or_404(conn, client_id, ctx.organization_id)
        row = db.set_client_status(conn, client_id, ctx.organization_id, "archived")
        auth.audit(conn, ctx, "client.archived", "client", client_id)
    return {"client_id": client_id, "status": row["status"], "archived_at": row["archived_at"].isoformat()}


@router.post("/clients/{client_id}/restore")
def restore_client(client_id: str):
    client_id = _validate_uuid(client_id, "client id")
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _client_or_404(conn, client_id, ctx.organization_id)
        row = db.set_client_status(conn, client_id, ctx.organization_id, "active")
        auth.audit(conn, ctx, "client.restored", "client", client_id)
    return {"client_id": client_id, "status": row["status"]}


class LinkBusiness(BaseModel):
    business_id: str | None = None      # None unlinks


@router.post("/clients/{client_id}/link-business")
def link_business(client_id: str, body: LinkBusiness):
    """Link the client record to the Knowledge Core business built from its
    documents (one identity — no second business record is created)."""
    client_id = _validate_uuid(client_id, "client id")
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _client_or_404(conn, client_id, ctx.organization_id)
        bid = None
        if body.business_id:
            bid = _validate_uuid(body.business_id, "business id")
            ent = db.get_entity(conn, bid, ctx.organization_id)
            if ent is None or ent["entity_type"] != "business":
                raise HTTPException(status_code=404, detail="Business not found.")
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM clients WHERE organization_id = %s AND business_entity_id = %s "
                            "AND id <> %s", (ctx.organization_id, bid, client_id))
                if cur.fetchone():
                    raise HTTPException(status_code=409, detail="That business is linked to another client.")
        db.set_client_workspace(conn, client_id, ctx.organization_id, business_entity_id=bid)
        auth.audit(conn, ctx, "client.business_linked" if bid else "client.business_unlinked", "client",
                   client_id, {"business_id": bid})
    return {"client_id": client_id, "business_id": bid}


class LinkSubmission(BaseModel):
    submission_id: str


@router.post("/clients/{client_id}/link-submission")
def link_submission(client_id: str, body: LinkSubmission):
    client_id = _validate_uuid(client_id, "client id")
    sid = _validate_uuid(body.submission_id, "submission id")
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _client_or_404(conn, client_id, ctx.organization_id)
        sub = db.get_submission(conn, sid, ctx.organization_id)
        if not sub:
            raise HTTPException(status_code=404, detail="Submission not found.")
        db.link_submission_to_client(conn, sid, client_id, ctx.organization_id)
        auth.audit(conn, ctx, "client.submission_linked", "client", client_id, {"submission_id": sid})
    return {"client_id": client_id, "submission_id": sid}


def _client_submission(conn, client: dict, org: str) -> dict:
    subs = db.list_client_submissions(conn, str(client["id"]), org)
    if subs:
        return subs[0]
    label = f"{client['business_name']} [{str(client['id'])[:8]}]"
    sub = db.create_submission(conn, organization_id=org, label=label)
    db.link_submission_to_client(conn, str(sub["id"]), str(client["id"]), org)
    return sub


@router.post("/clients/{client_id}/intelligence-documents", status_code=201)
async def upload_intelligence_document(client_id: str, file: UploadFile = File(...)):
    """Store a policy / schedule / survey / loss-run document for this client's
    intelligence. The text is read with the existing parser (quality check, OCR
    fallback); analysis is a separate, explicit step (the existing
    /knowledge/submissions/{id}/analyze-property-policy endpoint)."""
    client_id = _validate_uuid(client_id, "client id")
    ctx = auth.require_context()
    data = await file.read()
    filename = (file.filename or "unnamed")[:200]
    try:
        parsed, quality = parse_file_with_quality(filename, data)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    digest = sha256(data)
    with db.get_connection() as conn:
        client = _client_or_404(conn, client_id, ctx.organization_id)
        dup = db.find_duplicate_file(conn, ctx.organization_id, client_id, digest)
        if dup:
            raise HTTPException(status_code=409, detail=f"This file was already uploaded for this client "
                                                        f"as '{dup['filename']}'.")
        sub = _client_submission(conn, client, ctx.organization_id)
        doc = db.create_document(conn, organization_id=ctx.organization_id, carrier_id=None, filename=filename,
                                 file_type=extension_of(filename), size_bytes=len(data), pages=quality["pages"],
                                 chars=parsed.chars, status="parsed", error_message=None,
                                 extracted_text=parsed.text)
        db.set_document_submission(conn, str(doc["id"]), str(sub["id"]))
        db.save_document_quality(conn, document_id=str(doc["id"]), used_ocr=quality["used_ocr"],
                                 pages_total=quality["pages"],
                                 pages_ocr_low_confidence=len(quality.get("low_confidence_pages", [])),
                                 avg_ocr_confidence=quality.get("avg_ocr_confidence"),
                                 page_confidences=quality.get("page_confidences"), notes=quality.get("notes", []))
        db.save_stored_file(conn, ctx.organization_id, filename, content_type_for(filename), digest, data,
                            ctx.user_id, document_id=str(doc["id"]), client_id=client_id)
        auth.audit(conn, ctx, "document.uploaded", "document", str(doc["id"]),
                   {"client_id": client_id, "filename": filename, "kind": "intelligence", "size": len(data)})
    return {"document_id": str(doc["id"]), "submission_id": str(sub["id"]), "filename": filename,
            "status": "parsed", "pages": quality["pages"], "used_ocr": quality["used_ocr"],
            "next_step": "Run analysis to build the business, policy and coverage intelligence."}


@router.get("/clients/{client_id}/workspace")
def client_workspace(client_id: str):
    """The client record plus one document library: documents the client sent
    (checked against requirements) and documents read for intelligence, each
    with processing status and whether the original file can be downloaded."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        org = _resolve_organization(conn)
        client = _client_or_404(conn, client_id, org)
        files = db.list_stored_files_meta(conn, org, client_id)
        by_doc = {str(f["document_id"]): f for f in files if f["document_id"]}
        by_cdoc = {str(f["client_document_id"]): f for f in files if f["client_document_id"]}
        docs = []
        for d in db.list_client_documents(conn, client_id, org):
            docs.append({"document_id": str(d["id"]), "kind": "client_paperwork", "filename": d["filename"],
                         "requirement": d["requirement"], "status": d["verdict"], "doc_type": d.get("doc_type"),
                         "uploaded_at": d["uploaded_at"].isoformat(),
                         "downloadable": str(d["id"]) in by_cdoc,
                         "download_url": f"/clients/{client_id}/documents/{d['id']}/download"
                         if str(d["id"]) in by_cdoc else None})
        subs = db.list_client_submissions(conn, client_id, org)
        suggested = None
        for s in subs:
            for d in db.list_submission_documents(conn, str(s["id"]), org):
                docs.append({"document_id": str(d["id"]), "kind": "intelligence", "filename": d["filename"],
                             "submission_id": str(s["id"]), "status": d.get("status"),
                             "error": d.get("error_message"), "pages": d.get("pages"),
                             "uploaded_at": d["uploaded_at"].isoformat() if d.get("uploaded_at") else None,
                             "downloadable": str(d["id"]) in by_doc,
                             "download_url": f"/doc-intelligence/documents/{d['id']}/download"
                             if str(d["id"]) in by_doc else None})
            ent = db.find_entity_by_name(conn, org, "business", s["label"])
            if ent and not client.get("business_entity_id"):
                suggested = {"business_id": str(ent["id"]), "name": ent["name"],
                             "basis": f"built from this client's submission '{s['label']}'"}
        business = None
        if client.get("business_entity_id"):
            ent = db.get_entity(conn, str(client["business_entity_id"]), org)
            business = ent and {"business_id": str(ent["id"]), "name": ent["name"]}
        assigned = None
        if client.get("assigned_user_id"):
            u = db.get_user(conn, str(client["assigned_user_id"]))
            assigned = u and {"user_id": str(u["id"]), "display_name": u["display_name"]}
    return {"client": {"client_id": client_id, "business_name": client["business_name"],
                       "trading_name": client.get("trading_name"), "status": client["status"],
                       "industry": client.get("industry"), "assigned_to": assigned,
                       "field_provenance": client.get("field_provenance") or {}},
            "business": business, "suggested_business": suggested,
            "submissions": [{"submission_id": str(s["id"]), "label": s["label"]} for s in subs],
            "documents": docs}


@router.get("/clients/{client_id}/documents/{document_id}/download")
def download_client_document(client_id: str, document_id: str):
    client_id = _validate_uuid(client_id, "client id")
    document_id = _validate_uuid(document_id, "document id")
    ctx = auth.require_context()
    with db.get_connection() as conn:
        _client_or_404(conn, client_id, ctx.organization_id)
        if not any(str(d["id"]) == document_id for d in db.list_client_documents(conn, client_id, ctx.organization_id)):
            raise HTTPException(status_code=404, detail="Document not found.")
        row = db.get_stored_file(conn, ctx.organization_id, client_document_id=document_id)
        if row is None:
            raise HTTPException(status_code=404, detail="The original file was not stored (uploaded before "
                                                        "v0.14.0); its extracted text is still available.")
        auth.audit(conn, ctx, "document.downloaded", "client_document", document_id, {"client_id": client_id})
    return _download(row)


@router.get("/doc-intelligence/documents/{document_id}/download")
def download_intelligence_document(document_id: str):
    document_id = _validate_uuid(document_id, "document id")
    ctx = auth.require_context()
    with db.get_connection() as conn:
        row = db.get_stored_file(conn, ctx.organization_id, document_id=document_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found, or its original file was not stored.")
        auth.audit(conn, ctx, "document.downloaded", "document", document_id)
    return _download(row)
