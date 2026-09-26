"""
Knowledge Core API — entities, facts (with evidence and history), and the
Property Policy Intelligence trigger endpoint.

Follows api/doc_intelligence.py's exact pattern: every endpoint resolves the
caller's organization via _resolve_organization() first and threads it
through every db/service call, no exceptions — the whole point of this
package's schema (organization_id from the first line of DDL) is wasted if
an endpoint skips this.
"""
import logging

import knowledge.service as kc
import property_policy_intelligence
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import db

from .deps import _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateEntityBody(BaseModel):
    entity_type: str
    name: str
    attributes: dict | None = None


class UpdateFactBody(BaseModel):
    new_value: str
    normalized_value: float | None = None
    reviewed_by: str


def _entity_response(row: dict) -> dict:
    return {
        "entity_id": str(row["id"]),
        "entity_type": row["entity_type"],
        "name": row["name"],
        "attributes": row.get("attributes") or {},
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _fact_response(row: dict, *, evidence: list[dict] | None = None) -> dict:
    payload = {
        "fact_id": str(row["id"]),
        "entity_id": str(row["entity_id"]),
        "fact_type": row["fact_type"],
        "value": row["value"],
        "normalized_value": (float(row["normalized_value"])
                              if row.get("normalized_value") is not None else None),
        "unit": row.get("unit"),
        "status": row["status"],
        "confidence": {
            "value": float(row["confidence_value"]),
            "type": row["confidence_type"],
            "method": row.get("confidence_method"),
            "note": row.get("confidence_note"),
        },
        "produced_by": row["produced_by"],
        "valid_from": row["valid_from"].isoformat() if row.get("valid_from") else None,
        "valid_to": row["valid_to"].isoformat() if row.get("valid_to") else None,
        "created_at": row["created_at"].isoformat(),
        "reviewed_at": row["reviewed_at"].isoformat() if row.get("reviewed_at") else None,
        "reviewed_by": row.get("reviewed_by"),
    }
    if evidence is not None:
        payload["evidence"] = [
            {
                "evidence_id": str(e["id"]),
                "document_id": str(e["document_id"]),
                "document_filename": e.get("document_filename"),
                "page": e.get("page"),
                "section": e.get("section"),
                "snippet": e.get("snippet"),
                "source": e.get("source"),
            }
            for e in evidence
        ]
    return payload


@router.post("/knowledge/entities")
def create_entity(body: CreateEntityBody, x_organization_id: str | None = Header(None)):
    if body.entity_type not in kc.ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity_type. Must be one of: {sorted(kc.ENTITY_TYPES)}",
        )
    name = " ".join((body.name or "").split())
    if not name:
        raise HTTPException(status_code=400, detail="Give the entity a name.")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = kc.create_entity(
            conn, organization_id=organization_id, entity_type=body.entity_type,
            name=name, attributes=body.attributes,
        )
    return _entity_response(row)


@router.get("/knowledge/entities/{entity_id}")
def get_entity(entity_id: str, x_organization_id: str | None = Header(None)):
    entity_id = _validate_uuid(entity_id, "entity id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = kc.get_entity(conn, entity_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Entity not found.")
    return _entity_response(row)


@router.get("/knowledge/entities/{entity_id}/facts")
def get_entity_facts(entity_id: str, x_organization_id: str | None = Header(None)):
    """Every CURRENT fact for one entity, scoped to the caller's
    organization. Ownership is checked by first fetching the entity itself,
    exactly the pattern api/doc_intelligence.py's earlier fix (the
    documents/{id}/facts gap) established: never list a child collection by
    a bare id without confirming the parent belongs to the caller first."""
    entity_id = _validate_uuid(entity_id, "entity id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        entity = kc.get_entity(conn, entity_id, organization_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found.")
        rows = kc.find_facts(conn, entity_id, organization_id, current_only=True)
        facts = []
        for r in rows:
            evidence = kc.get_evidence_for_fact(conn, str(r["id"]), organization_id)
            facts.append(_fact_response(r, evidence=evidence))
    return {"entity": _entity_response(entity), "facts": facts}


@router.get("/knowledge/facts/{fact_id}")
def get_fact(fact_id: str, x_organization_id: str | None = Header(None)):
    fact_id = _validate_uuid(fact_id, "fact id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = kc.get_fact(conn, fact_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Fact not found.")
        evidence = kc.get_evidence_for_fact(conn, fact_id, organization_id)
    return _fact_response(row, evidence=evidence)


@router.put("/knowledge/facts/{fact_id}")
def correct_fact(fact_id: str, body: UpdateFactBody, x_organization_id: str | None = Header(None)):
    """Human review correction — updates the CURRENT fact's value in place
    (status becomes 'corrected', reviewed_by/reviewed_at recorded), as
    opposed to reconcile_fact()'s automatic write path, which is what
    actually creates new valid_from/valid_to versions when new evidence
    arrives. See knowledge/facts.py's correct_fact vs. supersede_fact."""
    fact_id = _validate_uuid(fact_id, "fact id")
    reviewed_by = " ".join((body.reviewed_by or "").split())
    if not reviewed_by:
        raise HTTPException(status_code=400, detail="Enter your name to record this correction.")
    new_value = (body.new_value or "").strip()
    if not new_value:
        raise HTTPException(status_code=400, detail="Provide the corrected value.")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = kc.update_fact(
            conn, fact_id, organization_id, new_value=new_value,
            normalized_value=body.normalized_value, reviewed_by=reviewed_by,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Fact not found.")
        evidence = kc.get_evidence_for_fact(conn, fact_id, organization_id)
    return _fact_response(row, evidence=evidence)


@router.get("/knowledge/facts/{fact_id}/history")
def get_fact_history(fact_id: str, x_organization_id: str | None = Header(None)):
    """
    Every version of this fact's entity+fact_type, oldest first — the read
    side of valid_from/valid_to versioning (knowledge/versions.py).
    """
    fact_id = _validate_uuid(fact_id, "fact id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = kc.get_fact(conn, fact_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Fact not found.")
        history_rows = kc.get_historical_values(
            conn, str(row["entity_id"]), organization_id, row["fact_type"]
        )
        history = []
        for h in history_rows:
            evidence = kc.get_evidence_for_fact(conn, str(h["id"]), organization_id)
            history.append(_fact_response(h, evidence=evidence))
    return {"fact_id": fact_id, "entity_id": str(row["entity_id"]),
            "fact_type": row["fact_type"], "versions": history}


@router.post("/knowledge/submissions/{submission_id}/analyze-property-policy")
def analyze_property_policy(submission_id: str, x_organization_id: str | None = Header(None)):
    """
    Run the Property Policy Intelligence slice over every document in one
    submission: extracts property.*/policy.* facts from each document,
    creates/reuses a Business + Asset(property) + Policy entity, links every
    fact to its evidence (document/page/snippet), and reconciles facts
    across documents — flagging both sides of any contradiction rather than
    silently picking one.

    404s exactly like every other submission-scoped endpoint when the
    submission does not exist for the caller's organization.
    """
    submission_id = _validate_uuid(submission_id, "submission id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        try:
            result = property_policy_intelligence.analyze_submission(
                conn, submission_id, organization_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        # Re-fetch each fact's evidence for the response — reconcile_fact
        # returns the bare fact row, not its evidence rows.
        facts_payload = []
        for fact in result["facts"]:
            evidence = kc.get_evidence_for_fact(conn, str(fact["id"]), organization_id)
            facts_payload.append(_fact_response(fact, evidence=evidence))

    return {
        "submission_id": submission_id,
        "business": _entity_response(result["business"]),
        "asset": _entity_response(result["asset"]),
        "policy": _entity_response(result["policy"]),
        "documents_processed": result["documents_processed"],
        "facts": facts_payload,
        "contradictions": result["contradictions"],
    }
