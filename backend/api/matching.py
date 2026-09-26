"""Phase 2 — /clients/{client_id}/match."""
import logging
import os
import time

import matching
from fastapi import APIRouter, Header, HTTPException

import db
import llm
import tokenise
from llm import LLMError

from .carriers import _require_search_ready
from .deps import _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


@router.post("/clients/{client_id}/match")
def run_match(client_id: str, x_organization_id: str | None = Header(None)):
    """Rank carrier plans against one client's situation."""
    client_id = _validate_uuid(client_id, "client id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_client(conn, client_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        # Seeded so the notes redact to the SAME placeholders as the stored
        # profile. A fresh Redactor would number independently, and the model
        # would see {{PAN_1}} meaning one thing in the profile and another in
        # the notes — two entities where there is one.
        token_map = db.get_client_tokens(conn, client_id, organization_id)

    redacted_notes = tokenise.Redactor(token_map).redact(row.get("risk_notes") or "")

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )
    _require_search_ready()

    started = time.perf_counter()
    try:
        result = matching.match_client(
            profile=row.get("redacted_profile") or "",
            categories=row.get("categories") or [],
            risk_notes=redacted_notes,
            organization_id=organization_id,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Match failed")
        raise HTTPException(status_code=500, detail=f"Match failed: {exc}")

    stats = {
        **result["stats"],
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }

    with db.get_connection() as conn:
        saved = db.save_match(
            conn,
            client_id=client_id,
            organization_id=organization_id,
            results=result["matches"],
            sources=result["sources"],
            model=llm.OLLAMA_MODEL,
            stats=stats,
        )

    return {
        "match_id": str(saved["id"]),
        "client_id": client_id,
        "matches": result["matches"],
        "gaps": result["gaps"],
        "questions": result["questions"],
        "sources": result["sources"],
        "stats": stats,
        "model": llm.OLLAMA_MODEL,
        "created_at": saved["created_at"].isoformat(),
    }


@router.get("/clients/{client_id}/match")
def read_match(client_id: str, x_organization_id: str | None = Header(None)):
    """The most recent match run for this client."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_latest_match(conn, client_id, organization_id)
    if not row:
        raise HTTPException(status_code=404, detail="No match run yet.")
    return {
        "match_id": str(row["id"]),
        "client_id": str(row["client_id"]),
        "matches": row["results"],
        "gaps": [],
        "questions": [],
        "sources": row["sources"],
        "stats": row["stats"],
        "model": row["model"],
        "created_at": row["created_at"].isoformat(),
    }
