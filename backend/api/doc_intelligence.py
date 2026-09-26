"""
Document Intelligence vertical slice: upload -> quality check -> text/OCR
-> one extracted fact -> evidence + typed confidence -> human review ->
append-only audit event. Deliberately does not touch the existing carrier
upload/briefing flow — that flow keeps working exactly as it did. This
exists to validate the schema (db.py's extraction_facts / extraction_events
/ organizations) against one real path before any other extraction code is
asked to use the same shapes.
"""
import logging
import os

import doc_intelligence
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

import db
import llm
from llm import LLMError
from parsers import ParseError, extension_of, parse_file_with_quality

from .deps import _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class ReviewAction(BaseModel):
    """A human verifying, correcting, or flagging one extracted fact."""

    reviewer: str
    action: str  # 'verified' | 'corrected' | 'flagged'
    new_value: str | None = None
    reason: str | None = None


def _fact_response(row: dict) -> dict:
    return {
        "fact_id": str(row["id"]),
        "document_id": str(row["document_id"]),
        "fact_type": row["fact_type"],
        "label": row["label"],
        "value": row["value"],
        "original_value": row.get("original_value"),
        "status": row["status"],
        "evidence": {
            "page": row.get("evidence_page"),
            "section": row.get("evidence_section"),
            "snippet": row.get("evidence_snippet"),
            "source": row.get("evidence_source"),
        },
        "confidence": {
            "value": row["confidence_value"],
            "type": row["confidence_type"],
            "method": row["confidence_method"],
            "note": row.get("confidence_note"),
        },
        "produced_by": row["produced_by"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.post("/doc-intelligence/upload")
async def doc_intelligence_upload(
    file: UploadFile = File(...),
    submission_id: str | None = Form(None),
    x_organization_id: str | None = Header(None),
):
    """
    Upload one policy document for the vertical slice: quality check, OCR
    fallback if needed, extraction of the coverage-limit fact, all stored
    with evidence and typed confidence, awaiting human review.

    If `submission_id` is given, the document is attached to that submission
    and, once its fact is extracted, checked against every other fact
    already on record for the submission — this is what lets a policy and
    its own coverage schedule be compared to each other at all. Without a
    submission_id, the document is scored in isolation exactly as before;
    contradiction detection has nothing to compare against for a document
    with no declared siblings.

    Pilot-stage tenant scoping: the caller acts as whichever organization
    the X-Organization-Id header names (or the default organization if no
    header is sent). Every document and fact created here is scoped to
    that organization, and reading a submission or fact belonging to a
    DIFFERENT organization is rejected — see _resolve_organization and the
    organization_id filters in db.py's get_submission / get_extraction_fact.
    This is application-level scoping, not database-enforced isolation
    (no Postgres row-level security yet) — documented explicitly as the
    pilot-stage boundary, not a claim of full multi-tenant isolation.
    """
    data = await file.read()
    filename = file.filename or "unnamed"

    try:
        parsed, quality = parse_file_with_quality(filename, data)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    submission_id = (
        _validate_uuid(submission_id, "submission id") if submission_id else None
    )

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)

        if submission_id and not db.get_submission(conn, submission_id, organization_id):
            # Same message whether the submission belongs to a different
            # organization or does not exist at all — a caller cannot use
            # the error to distinguish "not yours" from "never existed".
            raise HTTPException(status_code=404, detail="Submission not found.")

        # Reuses the existing documents table with carrier_id left NULL — a
        # doc-intelligence upload is not filed against any carrier-briefing
        # carrier at all. This used to manufacture a fake carrier row
        # ("(unfiled) <filename>") purely to satisfy a NOT NULL constraint;
        # that constraint is gone (see the organization_id/carrier_id
        # migration in db.py), so there is nothing left to work around.
        document_row = db.create_document(
            conn,
            organization_id=organization_id,
            carrier_id=None,
            filename=filename,
            file_type=extension_of(filename),
            size_bytes=len(data),
            pages=quality["pages"],
            chars=parsed.chars,
            status="parsed",
            error_message=None,
            extracted_text=parsed.text,
        )
        document_id = str(document_row["id"])

        if submission_id:
            db.set_document_submission(conn, document_id, submission_id)

        db.save_document_quality(
            conn,
            document_id=document_id,
            used_ocr=quality["used_ocr"],
            pages_total=quality["pages"],
            pages_ocr_low_confidence=len(quality.get("low_confidence_pages", [])),
            avg_ocr_confidence=quality.get("avg_ocr_confidence"),
            notes=quality.get("notes", []),
        )

    # Extraction happens outside the connection block: it may call the LLM,
    # which should never hold a database connection open while it runs.
    if not llm.is_available():
        # The document and its quality record are already saved — extraction
        # can be retried later without re-uploading. Nothing here invents a
        # fact rather than admit the model is unreachable.
        return {
            "document_id": document_id,
            "submission_id": submission_id,
            "quality": quality,
            "fact": None,
            "extraction_error": (
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled. The document is saved; "
                "re-run extraction once it is available."
            ),
        }

    ocr_confidence = quality.get("avg_ocr_confidence")
    extracted = doc_intelligence.extract_coverage_limit(
        text=parsed.text, ocr_confidence=ocr_confidence
    )

    if extracted is None:
        # No fact created — per the "no silent guessing" rule, absence is
        # represented by there being no row, not by a row with an empty or
        # invented value.
        return {
            "document_id": document_id,
            "submission_id": submission_id,
            "quality": quality,
            "fact": None,
            "extraction_error": (
                "No coverage limit could be found in this document with "
                "enough confidence to record."
            ),
        }

    with db.get_connection() as conn:
        fact_row = db.save_extraction_fact(
            conn,
            organization_id=organization_id,
            document_id=document_id,
            fact_type=extracted["fact_type"],
            label=extracted["label"],
            value=extracted["value"],
            evidence=extracted["evidence"],
            confidence=extracted["confidence"],
            status="needs_review",
            produced_by=extracted["produced_by"],
        )

        contradiction = None
        if submission_id:
            # Every fact already on record for this submission, from every
            # document in it — including ones extracted before this one.
            # The just-saved fact is excluded by document_id, not by fact
            # id, so a policy's own restated figure on a later page of the
            # SAME document is never compared against itself.
            existing_facts = [
                f
                for f in db.list_submission_facts(conn, submission_id, organization_id)
                if str(f["document_id"]) != document_id
            ]
            contradiction = doc_intelligence.find_contradiction(
                extracted["fact_type"], extracted["value"], existing_facts
            )

            if contradiction:
                note = (
                    f"Disagrees with {contradiction['document_filename']}, "
                    f"which states {contradiction['value']} "
                    f"({contradiction['relative_difference'] * 100:.1f}% different)."
                )
                fact_row = db.flag_contradiction(
                    conn,
                    fact_id=str(fact_row["id"]),
                    contradicts_fact_id=contradiction["fact_id"],
                    note=note,
                ) or fact_row

                # Symmetric: the OTHER fact is flagged too, with a note
                # pointing back at this document. A reviewer opening either
                # document's fact should see the dispute, not just whichever
                # one happened to be uploaded second.
                other_note = (
                    f"Disagrees with {filename}, which states "
                    f"{extracted['value']} "
                    f"({contradiction['relative_difference'] * 100:.1f}% different)."
                )
                db.flag_contradiction(
                    conn,
                    fact_id=contradiction["fact_id"],
                    contradicts_fact_id=str(fact_row["id"]),
                    note=other_note,
                )

    payload = {
        "document_id": document_id,
        "submission_id": submission_id,
        "quality": quality,
        "fact": _fact_response(fact_row),
    }
    if contradiction:
        payload["contradiction_detected"] = True
    return payload


@router.get("/doc-intelligence/documents/{document_id}/facts")
def list_document_facts(document_id: str, x_organization_id: str | None = Header(None)):
    """
    Every fact extracted from one document, scoped to the caller's
    organization.

    Previously did not apply the same ownership check that
    GET /doc-intelligence/facts/{fact_id} already does — a caller could
    list every fact for a document_id belonging to a different
    organization even though fetching one of those same facts by fact_id
    directly was correctly rejected. Fixed the same way: resolve the
    organization first and pass it through the query itself.
    """
    document_id = _validate_uuid(document_id, "document id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.list_extraction_facts(conn, document_id, organization_id)
        quality = db.get_document_quality(conn, document_id, organization_id)
    return {
        "document_id": document_id,
        "quality": quality,
        "facts": [_fact_response(r) for r in rows],
    }


@router.get("/doc-intelligence/facts/{fact_id}")
def get_fact(fact_id: str, x_organization_id: str | None = Header(None)):
    fact_id = _validate_uuid(fact_id, "fact id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_extraction_fact(conn, fact_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Fact not found.")
        events = db.get_extraction_events(conn, fact_id)
    payload = _fact_response(row)
    payload["history"] = [
        {
            "event": e["event"],
            "reviewer": e["reviewer"],
            "before_value": e["before_value"],
            "after_value": e["after_value"],
            "reason": e["reason"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


@router.post("/doc-intelligence/facts/{fact_id}/review")
def review_fact(
    fact_id: str, body: ReviewAction, x_organization_id: str | None = Header(None)
):
    """
    Record a human review action on one extracted fact.

    'corrected' requires new_value — a correction with nothing to correct to
    is not a correction. The event is appended to extraction_events
    regardless of outcome; nothing here is ever deleted or overwritten,
    matching the same append-only principle as draft_events. Scoped to the
    caller's organization — reviewing a fact that belongs to a different
    organization is rejected with the same 404 as a fact that does not
    exist, so this endpoint cannot be used to even confirm another
    organization's fact_id is valid, let alone alter it.
    """
    fact_id = _validate_uuid(fact_id, "fact id")

    reviewer = " ".join((body.reviewer or "").split())
    if not reviewer:
        raise HTTPException(status_code=400, detail="Enter your name to review this.")

    action = (body.action or "").strip().lower()
    if action not in {"verified", "corrected", "flagged"}:
        raise HTTPException(
            status_code=400, detail="Action must be verified, corrected or flagged."
        )

    new_value = None
    if action == "corrected":
        new_value = (body.new_value or "").strip()
        if not new_value:
            raise HTTPException(
                status_code=400, detail="Provide the corrected value."
            )

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.review_extraction_fact(
            conn,
            fact_id,
            organization_id=organization_id,
            event=action,
            reviewer=reviewer[:120],
            new_value=new_value,
            reason=" ".join((body.reason or "").split())[:300] or None,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Fact not found.")
        events = db.get_extraction_events(conn, fact_id)

    payload = _fact_response(row)
    payload["history"] = [
        {
            "event": e["event"],
            "reviewer": e["reviewer"],
            "before_value": e["before_value"],
            "after_value": e["after_value"],
            "reason": e["reason"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload
