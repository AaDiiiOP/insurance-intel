"""
Knowledge Core API — entities, facts (with evidence and history), and the
Property Policy Intelligence trigger endpoint.

Follows api/doc_intelligence.py's exact pattern: every endpoint resolves the
caller's organization via _resolve_organization() first and threads it
through every db/service call, no exceptions — the whole point of this
package's schema (organization_id from the first line of DDL) is wasted if
an endpoint skips this.
"""
import datetime
import logging

import knowledge.service as kc
import policy_comparison
import property_policy_intelligence
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import auth
import db
import review_workflow

from .deps import _actor, _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateEntityBody(BaseModel):
    entity_type: str
    name: str
    attributes: dict | None = None


class UpdateFactBody(BaseModel):
    new_value: str
    normalized_value: float | None = None
    reviewed_by: str | None = None   # ignored: the signed-in user is the reviewer
    reason: str | None = None        # v0.16.0: required — why the value is being corrected


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
        "effective_from": row["effective_from"].isoformat() if row.get("effective_from") else None,
        "effective_to": row["effective_to"].isoformat() if row.get("effective_to") else None,
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


@router.get("/knowledge/entities")
def list_entities(entity_type: str | None = None, x_organization_id: str | None = Header(None)):
    """Entities in the caller's organization (optionally one type), newest first."""
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = kc.list_entities(conn, organization_id, entity_type)
    return {"entities": [_entity_response(r) for r in rows]}


@router.get("/knowledge/possible-duplicates")
def possible_duplicates(entity_type: str | None = None, x_organization_id: str | None = Header(None)):
    """Look-alike entity pairs for a human to review. Nothing is merged."""
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        pairs = kc.find_possible_duplicates(conn, organization_id=organization_id,
                                            entity_type=entity_type)
    return {"pairs": _json_safe(pairs)}


@router.get("/knowledge/entities/{entity_id}")
def get_entity(entity_id: str, x_organization_id: str | None = Header(None)):
    entity_id = _validate_uuid(entity_id, "entity id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = kc.get_entity(conn, entity_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Entity not found.")
    return _entity_response(row)


def _json_safe(value):
    """Dates/datetimes/UUIDs/Decimals -> JSON-friendly values (recursive)."""
    import datetime as _dt, decimal as _dec, uuid as _uuid
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, _dec.Decimal):
        return float(value)
    return value


@router.get("/knowledge/entities/{entity_id}/knowledge")
def get_entity_knowledge(entity_id: str, as_of: str | None = None,
                          x_organization_id: str | None = Header(None)):
    """Everything known about one entity: current + historical facts (with
    evidence), unresolved conflicts, relationships both ways, and — with
    ?as_of=YYYY-MM-DD — what was in force on that date, kept separate from
    "current". Organization-scoped; another tenant's id is a 404."""
    entity_id = _validate_uuid(entity_id, "entity id")
    if as_of is not None:
        try:
            datetime.date.fromisoformat(as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD.")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        result = kc.get_entity_knowledge(conn, entity_id, organization_id, as_of=as_of)
    if result is None:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return _json_safe(result)


@router.get("/knowledge/entities/{entity_id}/relationships")
def get_entity_relationships(entity_id: str, x_organization_id: str | None = Header(None)):
    entity_id = _validate_uuid(entity_id, "entity id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        result = kc.get_entity_knowledge(conn, entity_id, organization_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return _json_safe(result["relationships"])


class ReviewFactBody(BaseModel):
    action: str
    reviewed_by: str | None = None   # ignored: the signed-in user is the reviewer
    reason: str | None = None        # v0.16.0: required for 'reject'


@router.post("/knowledge/facts/{fact_id}/review")
def review_fact(fact_id: str, body: ReviewFactBody, x_organization_id: str | None = Header(None)):
    """Human review: action 'verify' (needs evidence, not in a conflict) or
    'reject' (kept for audit as superseded). Conflicts are settled by the
    correction endpoint, not here."""
    fact_id = _validate_uuid(fact_id, "fact id")
    reason = " ".join((body.reason or "").split())[:1000] or None
    if body.action == "reject" and not reason:
        raise HTTPException(status_code=400, detail="A reason is required to reject a fact.")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        try:
            row = kc.review_fact(conn, fact_id, organization_id, action=body.action,
                                 reviewed_by=_actor())
        except ValueError as exc:
            status = 404 if "not found" in str(exc).lower() else 400
            raise HTTPException(status_code=status, detail=str(exc))
        review_workflow.audit_fact(conn, auth.current(),
                                   "fact.verified" if body.action == "verify" else "fact.rejected",
                                   fact_id, reason)
        evidence = kc.get_evidence_for_fact(conn, fact_id, organization_id)
    return _fact_response(row, evidence=evidence)


@router.get("/knowledge/entities/{entity_id}/facts")
def get_entity_facts(entity_id: str, x_organization_id: str | None = Header(None)):
    """Every CURRENT fact for one entity, scoped to the caller's
    organization. Ownership is checked by first fetching the entity itself,
    exactly the pattern api/doc_intelligence.py's earlier fix (the
    documents/{id}/facts gap) established: never list a child collection by
    a bare id without confirming the parent belongs to the caller first.

    "Current" here still means the plain valid_to IS NULL rows — a
    fact_type with an unresolved conflict lists BOTH contradicting rows
    (each still individually valid_to IS NULL), rather than hiding either
    one; see GET /knowledge/entities/{id}/facts/{fact_type}/current for the
    conflict-aware single-value read."""
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


@router.get("/knowledge/entities/{entity_id}/facts/{fact_type}/current")
def get_entity_current_fact(entity_id: str, fact_type: str,
                             x_organization_id: str | None = Header(None)):
    """
    The conflict-aware "what is the current value" read for one
    entity+fact_type (item 1's fix). Unlike get_entity_facts above, this
    picks a single answer when there is one — or explicitly says there
    isn't, rather than an ORDER BY ... LIMIT 1 silently picking a side of an
    unresolved disagreement.

    Response shape: {"current_value": <fact or null>, "unresolved_conflict":
    bool, "conflict_id": str|null, "conflicting_facts": [<fact>, ...]}.
    """
    entity_id = _validate_uuid(entity_id, "entity id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        entity = kc.get_entity(conn, entity_id, organization_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found.")
        result = kc.get_current_value_or_conflict(conn, entity_id, organization_id, fact_type)
        current_value = (
            _fact_response(result["current_value"]) if result["current_value"] else None
        )
        conflicting_facts = [_fact_response(f) for f in result["conflicting_facts"]]
    return {
        "current_value": current_value,
        "unresolved_conflict": result["unresolved_conflict"],
        "conflict_id": result["conflict_id"],
        "conflicting_facts": conflicting_facts,
    }


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
    """
    Human review correction — VERSIONS the fact: the original row's
    valid_to is closed and a NEW row is created carrying the corrected
    value, status='corrected', reviewed_by/reviewed_at recorded on the new
    row. The original value is never overwritten — GET
    /knowledge/facts/{id}/history (using either the old or new fact's id)
    shows the full chain. See knowledge/service.py's correct_fact, which
    goes through facts.supersede_fact() rather than any in-place UPDATE.

    If this correction resolves an unresolved contradiction for the fact's
    entity+fact_type, the matching kc_conflicts row is marked resolved with
    resolution_fact_id pointing at the new fact — reflected in this
    response's `resolved_conflict_id`.
    """
    fact_id = _validate_uuid(fact_id, "fact id")
    reviewed_by = _actor()
    new_value = (body.new_value or "").strip()
    if not new_value:
        raise HTTPException(status_code=400, detail="Provide the corrected value.")
    reason = " ".join((body.reason or "").split())[:1000]
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required for a correction.")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        old_fact = kc.get_fact(conn, fact_id, organization_id)
        if not old_fact:
            raise HTTPException(status_code=404, detail="Fact not found.")

        # Recorded BEFORE the correction: whether this entity+fact_type has
        # an open conflict right now, so we can tell afterward whether this
        # specific call is the one that resolved it (see below).
        conflict_before = db.get_unresolved_conflict(
            conn, str(old_fact["entity_id"]), organization_id, old_fact["fact_type"]
        )

        # update_fact() versions the fact — the row it returns is a NEW
        # fact row (new id), not fact_id itself, so its evidence must be
        # looked up under ITS id, not the original fact_id.
        new_fact = kc.update_fact(
            conn, fact_id, organization_id, new_value=new_value,
            normalized_value=body.normalized_value, reviewed_by=reviewed_by,
        )
        if not new_fact:
            raise HTTPException(status_code=404, detail="Fact not found.")
        evidence = kc.get_evidence_for_fact(conn, str(new_fact["id"]), organization_id)

        resolved_conflict_id = None
        if conflict_before is not None:
            after = db.get_conflict(conn, str(conflict_before["id"]), organization_id)
            if after and after["status"] == "resolved":
                resolved_conflict_id = str(after["id"])
        review_workflow.audit_fact(conn, auth.current(), "fact.corrected", fact_id, reason,
                                   new_fact_id=str(new_fact["id"]), resolved_conflict_id=resolved_conflict_id)

    payload = _fact_response(new_fact, evidence=evidence)
    payload["superseded_fact_id"] = fact_id
    payload["resolved_conflict_id"] = resolved_conflict_id
    return payload


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


class KnowledgeQueryBody(BaseModel):
    question: str
    entity_id: str | None = None
    as_of: str | None = None
    use_llm: bool = True


@router.post("/knowledge/query")
def knowledge_query(body: KnowledgeQueryBody, x_organization_id: str | None = Header(None)):
    """Answer a question from the Knowledge Core first (facts + evidence +
    periods + relationships), falling back to keyword retrieval and an
    evidence-validated local-model answer only when no fact type applies.
    Never returns an un-evidenced answer: unknown, ambiguous or contradictory
    cases come back with answer=null and needs_review=true."""
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="question is required.")
    if len(body.question) > 1000:
        raise HTTPException(status_code=400, detail="question must be at most 1000 characters.")
    entity_id = _validate_uuid(body.entity_id, "entity id") if body.entity_id else None
    if body.as_of is not None:
        try:
            datetime.date.fromisoformat(body.as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of must be YYYY-MM-DD.")
    from knowledge import query as kq
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        result = kq.query(conn, organization_id=organization_id, question=body.question.strip(),
                          entity_id=entity_id, as_of=body.as_of, use_llm=body.use_llm)
    return _json_safe(result)


@router.get("/knowledge/compare-submissions")
def compare_submissions(left_submission_id: str, right_submission_id: str,
                         x_organization_id: str | None = Header(None)):
    """
    Phase 1B — coverage comparison: diff two submissions' current Policy and
    Asset facts (e.g. this year's renewal against last year's expiring
    policy, or a new submission against the client's incumbent policy).

    Both submissions must already have been run through
    POST /knowledge/submissions/{id}/analyze-property-policy — this endpoint
    only reads existing Knowledge Core facts, it never extracts or writes
    anything. left_ is the baseline; right_ is compared against it, so
    "increased"/"added"/"removed" are stated relative to left (see
    policy_comparison.compare_entities's docstring).

    404s when either submission does not exist for the caller's
    organization; 409 (not 404) when a submission exists but has not been
    analyzed yet — a different, more specific condition than "not found"
    that the caller should surface differently (run analysis first, not
    check the submission id).
    """
    left_submission_id = _validate_uuid(left_submission_id, "left submission id")
    right_submission_id = _validate_uuid(right_submission_id, "right submission id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        try:
            result = policy_comparison.compare_submissions(
                conn, organization_id=organization_id,
                left_submission_id=left_submission_id,
                right_submission_id=right_submission_id,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "not found" in message else 409
            raise HTTPException(status_code=status_code, detail=message)
    return result


@router.get("/knowledge/compare-entities")
def compare_entities(left_entity_id: str, right_entity_id: str,
                      x_organization_id: str | None = Header(None)):
    """
    Lower-level form of the comparison above: diff two entities directly by
    id (any two entities of the same kind — typically two Policy entities,
    or two Asset entities), rather than resolving them from submission
    labels. Useful when the caller already has the entity_id (e.g. from a
    prior GET /knowledge/entities/{id}/facts) and does not want a second
    submission-label lookup.
    """
    left_entity_id = _validate_uuid(left_entity_id, "left entity id")
    right_entity_id = _validate_uuid(right_entity_id, "right entity id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        try:
            result = policy_comparison.compare_entities(
                conn, organization_id=organization_id,
                left_entity_id=left_entity_id, right_entity_id=right_entity_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return result
