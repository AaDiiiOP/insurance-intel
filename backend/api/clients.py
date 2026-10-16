"""
Phase 2 — /clients* CRUD, redacted profile, and Phase 3.5 — documents the
client actually sent.
"""
import logging
import os

import extraction
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

import db
import llm
import tokenise
import verify
from llm import LLMError
from parsers import ParseError, extension_of, parse_file

import auth

from .deps import _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class ClientIn(BaseModel):
    """
    A client intake, filled in by the broking team on the client's behalf.

    Every field optional but business_name: a broker often has half the
    picture after a first call, and refusing to save that would push them
    back to a notebook.
    """

    business_name: str
    entity_type: str | None = None
    industry: str | None = None
    city: str | None = None
    state: str | None = None
    turnover: str | None = None
    years_trading: str | None = None
    employee_count: str | None = None
    pan: str | None = None
    gstin: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    categories: list[str] = []
    sum_insured: str | None = None
    risk_notes: str | None = None
    documents_held: list[str] = []
    documents_missing: list[str] = []
    # Phase 2A workspace fields.
    trading_name: str | None = None
    notes: str | None = None
    assigned_user_id: str | None = None
    # Fields pre-filled from a document (/clients/extract) and NOT yet confirmed
    # by a person. Everything else is recorded as entered by a person.
    extracted_fields: list[str] = []


# Which intake fields are treated as identifying, and the placeholder kind
# each becomes. Everything not listed here is business context — industry,
# turnover band, city — which the model needs in order to match at all.
SENSITIVE_FIELDS = {
    "business_name": "BUSINESS",
    "pan": "PAN",
    "gstin": "GSTIN",
    "contact_name": "PERSON",
    "contact_phone": "PHONE",
    "contact_email": "EMAIL",
}


def _build_redacted_profile(body: ClientIn, existing_map: dict) -> tuple[str, dict]:
    """
    Turn an intake into the text the model will see, plus the placeholder map.

    Identifying fields are registered by name first, so they get stable
    placeholders regardless of whether they also appear in the free-text
    notes. Then the notes are scanned for anything the form did not capture —
    a PAN mentioned in passing, a phone number in a sentence.
    """
    redactor = tokenise.Redactor(existing_map)

    for field, kind in SENSITIVE_FIELDS.items():
        value = getattr(body, field, None)
        if value and str(value).strip():
            redactor.register(kind, str(value).strip())

    lines = [f"Business: {redactor.register('BUSINESS', body.business_name)}"]
    for label, value in (
        ("Entity type", body.entity_type),
        ("Industry", body.industry),
        ("Location", ", ".join(x for x in (body.city, body.state) if x)),
        ("Annual turnover", body.turnover),
        ("Years trading", body.years_trading),
        ("Employees", body.employee_count),
        ("Sum insured sought", body.sum_insured),
    ):
        if value and str(value).strip():
            lines.append(f"{label}: {str(value).strip()}")

    if body.categories:
        lines.append(f"Cover requested: {', '.join(body.categories)}")

    if body.risk_notes and body.risk_notes.strip():
        lines.append("")
        lines.append("Broker notes:")
        lines.append(redactor.redact(body.risk_notes.strip()))

    # A final pass over the assembled text catches anything identifying that
    # slipped in through a field not marked sensitive — a business name typed
    # into "industry", say.
    profile = redactor.redact("\n".join(lines))
    return profile, redactor.mapping


def _provenance(body: "ClientIn") -> dict:
    allowed = set(db.CLIENT_FIELDS)
    return {f: "extracted_unconfirmed" for f in body.extracted_fields if f in allowed}


def _check_assignee(conn, organization_id: str, user_id: str | None) -> str | None:
    if not user_id:
        return None
    user_id = _validate_uuid(user_id, "assigned user id")
    m = db.get_membership(conn, organization_id, user_id)
    if m is None or m["status"] != "active":
        raise HTTPException(status_code=400, detail="The assigned user is not an active member.")
    return user_id


def _client_response(row: dict) -> dict:
    out = {
        "status": row.get("status", "active"),
        "archived_at": row["archived_at"].isoformat() if row.get("archived_at") else None,
        "assigned_user_id": str(row["assigned_user_id"]) if row.get("assigned_user_id") else None,
        "business_entity_id": str(row["business_entity_id"]) if row.get("business_entity_id") else None,
        "field_provenance": row.get("field_provenance") or {},
        "client_id": str(row["id"]),
        "categories": row.get("categories") or [],
        "documents_held": row.get("documents_held") or [],
        "documents_missing": row.get("documents_missing") or [],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }
    for field in db.CLIENT_FIELDS:
        out[field] = row.get(field)
    return out


@router.post("/clients", status_code=201)
def create_client(body: ClientIn, x_organization_id: str | None = Header(None)):
    if not body.business_name.strip():
        raise HTTPException(status_code=400, detail="A business name is required.")

    profile, mapping = _build_redacted_profile(body, {})
    fields = {f: getattr(body, f, None) for f in db.CLIENT_FIELDS}

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.create_client(
            conn,
            fields=fields,
            categories=body.categories,
            redacted_profile=profile,
            organization_id=organization_id,
        )
        db.save_client_tokens(conn, str(row["id"]), organization_id, mapping)
        row = db.set_client_workspace(conn, str(row["id"]), organization_id,
                                      assigned_user_id=_check_assignee(conn, organization_id, body.assigned_user_id),
                                      field_provenance=_provenance(body))
        auth.audit(conn, auth.current(), "client.created", "client", str(row["id"]),
                   {"extracted_unconfirmed": sorted(_provenance(body))})
        if body.documents_held or body.documents_missing:
            db.set_client_documents(
                conn,
                str(row["id"]),
                organization_id,
                held=body.documents_held,
                missing=body.documents_missing,
            )
            row["documents_held"] = body.documents_held
            row["documents_missing"] = body.documents_missing

    return _client_response(row)


@router.get("/clients")
def list_clients(q: str | None = None, status: str = "active", assigned_to: str | None = None,
                 page: int = 1, page_size: int = 25, x_organization_id: str | None = Header(None)):
    """Search / filter / paginate in SQL. status: active (default) / archived / all."""
    if status not in ("active", "archived", "all"):
        raise HTTPException(status_code=400, detail="status must be active, archived or all.")
    page, page_size = max(1, int(page)), max(1, min(int(page_size), 100))
    q = " ".join((q or "").split())[:100] or None
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if assigned_to == "me":
            assigned_to = auth.require_context().user_id
        elif assigned_to:
            assigned_to = _validate_uuid(assigned_to, "assigned user id")
        rows, total = db.search_clients(conn, organization_id, q=q, status=status, assigned_user_id=assigned_to,
                                        limit=page_size, offset=(page - 1) * page_size)
    return {
        "total": total, "page": page, "page_size": page_size,
        "clients": [
            {
                "client_id": str(r["id"]),
                "trading_name": r["trading_name"],
                "status": r["status"],
                "assigned_to": ({"user_id": str(r["assigned_user_id"]), "display_name": r["assigned_name"]}
                                if r["assigned_user_id"] else None),
                "business_entity_id": str(r["business_entity_id"]) if r["business_entity_id"] else None,
                "document_count": r["document_count"],
                "business_name": r["business_name"],
                "industry": r["industry"],
                "city": r["city"],
                "state": r["state"],
                "turnover": r["turnover"],
                "categories": r["categories"] or [],
                "created_at": r["created_at"].isoformat(),
                "last_matched_at": (
                    r["last_matched_at"].isoformat() if r["last_matched_at"] else None
                ),
            }
            for r in rows
        ]
    }


@router.get("/clients/{client_id}")
def get_client(client_id: str, x_organization_id: str | None = Header(None)):
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_client(conn, client_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        latest = db.get_latest_match(conn, client_id, organization_id)

    payload = _client_response(row)
    payload["has_match"] = latest is not None
    return payload


@router.put("/clients/{client_id}")
def update_client(client_id: str, body: ClientIn,
                  x_organization_id: str | None = Header(None)):
    client_id = _validate_uuid(client_id, "client id")
    if not body.business_name.strip():
        raise HTTPException(status_code=400, detail="A business name is required.")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if not db.get_client(conn, client_id, organization_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        # Seed with the existing map so placeholders stay stable across edits:
        # a match run yesterday and one today should refer to the same
        # {{BUSINESS_1}}, not to two different tokens for one company.
        existing = db.get_client_tokens(conn, client_id, organization_id)

    profile, mapping = _build_redacted_profile(body, existing)
    fields = {f: getattr(body, f, None) for f in db.CLIENT_FIELDS}

    with db.get_connection() as conn:
        row = db.update_client(
            conn,
            client_id,
            organization_id,
            fields=fields,
            categories=body.categories,
            redacted_profile=profile,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        db.save_client_tokens(conn, client_id, organization_id, mapping)
        row = db.set_client_workspace(conn, client_id, organization_id,
                                      assigned_user_id=_check_assignee(conn, organization_id, body.assigned_user_id),
                                      field_provenance=_provenance(body))
        auth.audit(conn, auth.current(), "client.updated", "client", client_id,
                   {"extracted_unconfirmed": sorted(_provenance(body))})
        db.set_client_documents(
            conn,
            client_id,
            organization_id,
            held=body.documents_held,
            missing=body.documents_missing,
        )
        row["documents_held"] = body.documents_held
        row["documents_missing"] = body.documents_missing

    return _client_response(row)


@router.delete("/clients/{client_id}")
def delete_client(client_id: str, x_organization_id: str | None = Header(None)):
    """
    Delete a client and everything derived from them.

    Postgres cascades to the token map and match history. Nothing about a
    client is ever written to Chroma, so there is nothing to clean up there —
    which is the point of never indexing client text.
    """
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if not db.delete_client(conn, client_id, organization_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        auth.audit(conn, auth.current(), "client.deleted", "client", client_id)
    return {"deleted": client_id}


@router.get("/clients/{client_id}/profile")
def get_redacted_profile(client_id: str, x_organization_id: str | None = Header(None)):
    """
    Exactly what the model is shown for this client.

    Exposed so the team can see the redaction working rather than take it on
    trust — the placeholder map itself is deliberately NOT returned.
    """
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_client(conn, client_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        mapping = db.get_client_tokens(conn, client_id, organization_id)

    return {
        "client_id": client_id,
        "redacted_profile": row.get("redacted_profile") or "",
        "placeholders": sorted(mapping.keys()),
    }


@router.post("/clients/extract")
async def extract_from_document(file: UploadFile = File(...)):
    """
    Read a client's own document into proposed intake fields.

    Returns a DRAFT for the broker to check and correct — nothing is saved
    here. Extraction is a reading aid, not a source of record: a model that
    misreads a turnover figure should cost a correction, not a wrong policy.
    """
    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )

    data = await file.read()
    filename = file.filename or "unnamed"

    try:
        parsed = parse_file(filename, data)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        result = extraction.extract_client(parsed.text)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Client extraction failed")
        raise HTTPException(status_code=500, detail=f"Could not read that document: {exc}")

    result["filename"] = filename
    result["chars"] = parsed.chars
    return result


# --------------------------------------------------------------------------
# Phase 3.5 — documents the client actually sent
# --------------------------------------------------------------------------


@router.post("/clients/{client_id}/documents/upload")
async def upload_client_document(
    client_id: str,
    requirement: str = Form(...),
    file: UploadFile = File(...),
    x_organization_id: str | None = Header(None),
):
    """
    Attach a document against the requirement it is meant to satisfy, and
    check it.

    Verification is substance-based, never template-based: there is no
    official layout for most of these, and rejecting a valid document for
    looking informal would train a broker to ignore the result.
    """
    client_id = _validate_uuid(client_id, "client id")

    requirement = " ".join((requirement or "").split())
    if not requirement:
        raise HTTPException(
            status_code=400, detail="Say which requirement this document is for."
        )

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        client = db.get_client(conn, client_id, organization_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found.")

    data = await file.read()
    filename = file.filename or "unnamed"

    try:
        parsed = parse_file(filename, data)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if llm.is_available():
        result = verify.verify(
            text=parsed.text, requirement=requirement, client=client
        )
    else:
        # Store it anyway. The structural checks still run and are useful on
        # their own, and losing an upload because a model is down would be a
        # worse outcome than an unassessed document.
        identifiers = verify.find_identifiers(parsed.text)
        result = {
            "verdict": "unchecked",
            "doc_type": "",
            "satisfies": False,
            "reason": (
                f"Saved, but not assessed — Ollama is not reachable at "
                f"{OLLAMA_HOST}. Re-upload once it is running."
            ),
            "identifiers": identifiers,
            "notes": verify.cross_check(identifiers, client),
            "key_facts": [],
            "improvements": [],
            "score": None,
            "score_parts": {},
        }

    import hashlib
    from .workspace import content_type_for
    digest = hashlib.sha256(data).hexdigest()
    with db.get_connection() as conn:
        dup = db.find_duplicate_file(conn, organization_id, client_id, digest)
        if dup:
            raise HTTPException(status_code=409, detail=f"This file was already uploaded for this client "
                                                        f"as '{dup['filename']}'.")
    with db.get_connection() as conn:
        row = db.save_client_document(
            conn,
            client_id=client_id,
            organization_id=organization_id,
            requirement=requirement,
            filename=filename,
            file_type=extension_of(filename),
            size_bytes=len(data),
            chars=parsed.chars,
            extracted_text=parsed.text,
            verification=result,
        )
        ctx = auth.current()
        db.save_stored_file(conn, organization_id, filename, content_type_for(filename), digest, data,
                            ctx.user_id if ctx else None, client_document_id=str(row["id"]), client_id=client_id)
        auth.audit(conn, ctx, "document.uploaded", "client_document", str(row["id"]),
                   {"client_id": client_id, "filename": filename, "requirement": requirement, "size": len(data)})

    return _client_document_response(row)


def _client_document_response(row: dict) -> dict:
    return {
        "document_id": str(row["id"]),
        "requirement": row["requirement"],
        "filename": row["filename"],
        "file_type": row.get("file_type"),
        "size_bytes": row.get("size_bytes"),
        "chars": row.get("chars"),
        "verdict": row["verdict"],
        "doc_type": row.get("doc_type"),
        "reason": row.get("reason"),
        "identifiers": row.get("identifiers") or {},
        "notes": row.get("notes") or [],
        "key_facts": row.get("key_facts") or [],
        "score": row.get("score"),
        "score_parts": row.get("score_parts") or {},
        "improvements": row.get("improvements") or [],
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


@router.get("/clients/{client_id}/documents")
def list_documents(client_id: str, x_organization_id: str | None = Header(None)):
    """Every document attached for this client."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.list_client_documents(conn, client_id, organization_id)
    return {
        "client_id": client_id,
        "documents": [_client_document_response(r) for r in rows],
    }


@router.delete("/clients/{client_id}/documents/{document_id}")
def remove_client_document(client_id: str, document_id: str,
                           x_organization_id: str | None = Header(None)):
    client_id = _validate_uuid(client_id, "client id")
    document_id = _validate_uuid(document_id, "document id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if not any(str(d["id"]) == document_id
                   for d in db.list_client_documents(conn, client_id, organization_id)):
            raise HTTPException(status_code=404, detail="Document not found.")
        if not db.delete_client_document(conn, document_id, organization_id):
            raise HTTPException(status_code=404, detail="Document not found.")
        auth.audit(conn, auth.current(), "document.deleted", "client_document", document_id,
                   {"client_id": client_id})
    return {"deleted": document_id}
