"""/doc-intelligence/submissions* — grouping documents into one case/company."""
from fastapi import APIRouter, Form, Header, HTTPException

import db

from .deps import _resolve_organization, _validate_uuid
from .doc_intelligence import _fact_response

router = APIRouter()


@router.post("/doc-intelligence/submissions")
def create_doc_intelligence_submission(
    label: str = Form(...), x_organization_id: str | None = Header(None)
):
    """
    Start a new submission — a named group of documents believed to belong
    to the same case or company. Created explicitly, up front, rather than
    inferred from upload order: the upload endpoint has no reliable way to
    guess that two files are related, and asking for a submission first is
    the only honest way to record that grouping.
    """
    label = " ".join((label or "").split())
    if not label:
        raise HTTPException(status_code=400, detail="Give this submission a name.")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.create_submission(conn, organization_id=organization_id, label=label)

    return {
        "submission_id": str(row["id"]),
        "label": row["label"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/doc-intelligence/submissions")
def list_doc_intelligence_submissions(x_organization_id: str | None = Header(None)):
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.list_submissions(conn, organization_id)
    return {
        "submissions": [
            {
                "submission_id": str(r["id"]),
                "label": r["label"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/doc-intelligence/submissions/{submission_id}")
def get_doc_intelligence_submission(
    submission_id: str, x_organization_id: str | None = Header(None)
):
    submission_id = _validate_uuid(submission_id, "submission id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        # get_submission is scoped to organization_id in the query itself
        # (see db.py) — a submission belonging to a different organization
        # returns None here, identically to one that does not exist, so a
        # caller cannot even tell the two apart.
        submission = db.get_submission(conn, submission_id, organization_id)
        if not submission:
            raise HTTPException(status_code=404, detail="Submission not found.")
        documents = db.list_submission_documents(conn, submission_id, organization_id)
        facts = db.list_submission_facts(conn, submission_id, organization_id)

    return {
        "submission_id": submission_id,
        "label": submission["label"],
        "documents": [
            {
                "document_id": str(d["id"]),
                "filename": d["filename"],
                "status": d["status"],
            }
            for d in documents
        ],
        "facts": [_fact_response(f) for f in facts],
    }
