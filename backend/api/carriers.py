"""
/carriers* — carrier document upload/parse, briefing (summarise), chunks,
and /reindex.
"""
import logging
import os
import time

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

import db
import llm
import vectorstore
from llm import LLMError
from parsers import ParseError, extension_of, parse_file
from summarise import summarise_carrier
from vectorstore import VectorStoreError

from .deps import _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = os.getenv("CHROMA_PORT", "8000")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class SummaryEdit(BaseModel):
    """Body of PUT /carriers/{id}/summary."""

    summary: str


def _derive_carrier_name(payloads) -> str:
    """Fall back to the first filename, minus its extension."""
    for filename, _ in payloads:
        stem = filename.rsplit(".", 1)[0].strip()
        if stem:
            return stem[:120]
    return "Untitled carrier"


def _document_response(row: dict) -> dict:
    return {
        "document_id": str(row["id"]),
        "name": row["filename"],
        "status": row["status"],
        "pages": row.get("pages") or 0,
        "chars": row.get("chars") or 0,
        "error": row.get("error_message"),
    }


# --------------------------------------------------------------------------
# Upload & parse
# --------------------------------------------------------------------------


@router.post("/carriers", status_code=201)
async def create_carrier(
    files: list[UploadFile] = File(...),
    name: str | None = Form(None),
    x_organization_id: str | None = Header(None),
):
    """
    Upload one carrier's documents, parse each to text, store the results.

    A file that fails to parse is stored with status='failed' and its error
    rather than aborting the whole upload — one unreadable brochure should
    not lose the policy wording uploaded alongside it.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    # Read every file before opening a transaction: an upload stream that
    # stalls should not hold a database connection open.
    payloads = []
    for upload in files:
        data = await upload.read()
        payloads.append((upload.filename or "unnamed", data))

    carrier_name = (name or "").strip() or _derive_carrier_name(payloads)

    stored = []
    try:
        with db.get_connection() as conn:
            organization_id = _resolve_organization(conn, x_organization_id)
            carrier_id = db.create_carrier(conn, carrier_name, organization_id)

            for filename, data in payloads:
                try:
                    parsed = parse_file(filename, data)
                except ParseError as exc:
                    row = db.create_document(
                        conn,
                        organization_id=organization_id,
                        carrier_id=carrier_id,
                        filename=filename,
                        file_type=extension_of(filename),
                        size_bytes=len(data),
                        pages=0,
                        chars=0,
                        status="failed",
                        error_message=str(exc),
                        extracted_text=None,
                    )
                    stored.append(_document_response(row))
                    continue

                row = db.create_document(
                    conn,
                    organization_id=organization_id,
                    carrier_id=carrier_id,
                    filename=filename,
                    file_type=extension_of(filename),
                    size_bytes=len(data),
                    pages=parsed.pages,
                    chars=parsed.chars,
                    status="parsed",
                    error_message=None,
                    extracted_text=parsed.text,
                )
                stored.append(_document_response(row))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Upload failed")
        raise HTTPException(status_code=500, detail=f"Could not store upload: {exc}")

    return {"carrier_id": carrier_id, "name": carrier_name, "files": stored}


@router.get("/carriers")
def list_carriers(x_organization_id: str | None = Header(None)):
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.list_carriers(conn, organization_id)
    return {
        "carriers": [
            {
                "carrier_id": str(r["id"]),
                "name": r["name"],
                "created_at": r["created_at"].isoformat(),
                "document_count": r["document_count"],
                "failed_count": r["failed_count"],
                "has_summary": r["has_summary"],
            }
            for r in rows
        ]
    }


@router.get("/carriers/{carrier_id}")
def get_carrier(carrier_id: str, x_organization_id: str | None = Header(None)):
    carrier_id = _validate_uuid(carrier_id, "carrier id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        carrier = db.get_carrier(conn, carrier_id, organization_id)
        if not carrier:
            raise HTTPException(status_code=404, detail="Carrier not found.")
        documents = db.list_documents_for_carrier(conn, carrier_id, organization_id)
        summary = db.get_summary(conn, carrier_id, organization_id)

    parsed = [d for d in documents if d["status"] == "parsed"]

    # Reported on every visit, not just after a briefing run: an indexing
    # failure is otherwise silent, and surfaces much later as a match that
    # inexplicably ignores this carrier.
    indexed_chunks = (
        vectorstore.count_for_carrier(carrier_id, organization_id)
        if summary and vectorstore.is_available()
        else 0
    )

    return {
        "carrier_id": str(carrier["id"]),
        "name": carrier["name"],
        "created_at": carrier["created_at"].isoformat(),
        "documents": [
            {
                **_document_response(d),
                "file_type": d["file_type"],
                "size_bytes": d["size_bytes"],
                "uploaded_at": d["uploaded_at"].isoformat(),
            }
            for d in documents
        ],
        "can_summarise": len(parsed) > 0,
        "has_summary": summary is not None,
        "indexed_chunks": indexed_chunks,
    }


@router.get("/documents/{document_id}/text")
def get_document_text(document_id: str, x_organization_id: str | None = Header(None)):
    """The full extracted text, for checking what the parser actually got."""
    document_id = _validate_uuid(document_id, "document id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_document_with_text(conn, document_id, organization_id)

    if not row:
        raise HTTPException(status_code=404, detail="Document not found.")
    if row["status"] != "parsed":
        raise HTTPException(
            status_code=409,
            detail=f"This document failed to parse: {row['error_message']}",
        )

    return {
        "document_id": str(row["id"]),
        "filename": row["filename"],
        "pages": row["pages"],
        "chars": row["chars"],
        "text": row["extracted_text"] or "",
    }


@router.delete("/carriers/{carrier_id}")
def delete_carrier(carrier_id: str, x_organization_id: str | None = Header(None)):
    carrier_id = _validate_uuid(carrier_id, "carrier id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if not db.delete_carrier(conn, carrier_id, organization_id):
            raise HTTPException(status_code=404, detail="Carrier not found.")

    # Postgres cascades to documents and chunks; Chroma has no foreign keys,
    # so its copies are dropped explicitly or they would linger and be cited
    # in answers about a carrier that no longer exists.
    vectorstore.remove_carrier(carrier_id)
    return {"deleted": carrier_id}


# --------------------------------------------------------------------------
# Summarise
# --------------------------------------------------------------------------

# In-memory progress for the currently-running summarise, keyed by carrier.
# Deliberately not in Postgres: it is worthless after a restart, and writing
# a row per chunk would be noise in a table that matters.
_progress: dict[str, dict] = {}


def _set_progress(carrier_id: str, **fields) -> None:
    _progress[carrier_id] = {**_progress.get(carrier_id, {}), **fields}


@router.get("/carriers/{carrier_id}/summary/progress")
def summary_progress(carrier_id: str):
    """
    How far a running summarise has got.

    The UI polls this: a map-reduce over a long policy wording can take
    several minutes on a local model, and a spinner with no numbers behind
    it is indistinguishable from a hang.
    """
    carrier_id = _validate_uuid(carrier_id, "carrier id")
    return _progress.get(carrier_id) or {"state": "idle"}


def _summary_response(row: dict) -> dict:
    return {
        "carrier_id": str(row["carrier_id"]),
        "summary": row["summary"],
        "company_overview": row["company_overview"],
        "requirements": row["requirements"],
        "plans": row["plans"],
        "exclusions": row["exclusions"],
        "key_points": row["key_points"],
        "stats": row["stats"],
        "model": row["model"],
        "generated_at": row["generated_at"].isoformat(),
        "edited_at": row["edited_at"].isoformat() if row["edited_at"] else None,
    }


@router.post("/carriers/{carrier_id}/summary")
def create_summary(carrier_id: str, x_organization_id: str | None = Header(None)):
    """
    Run the map-reduce over every parsed document for this carrier.

    Synchronous: this can take minutes, and the client polls
    /summary/progress alongside it. Kept simple deliberately — a task queue
    is worth adding when several people use this at once, not before.
    """
    carrier_id = _validate_uuid(carrier_id, "carrier id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        carrier = db.get_carrier(conn, carrier_id, organization_id)
        if not carrier:
            raise HTTPException(status_code=404, detail="Carrier not found.")
        documents = db.list_documents_for_carrier(conn, carrier_id, organization_id)

    parsed = [d for d in documents if d["status"] == "parsed"]
    if not parsed:
        raise HTTPException(
            status_code=409,
            detail="No parsed documents for this carrier. Upload a readable file first.",
        )

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or '{llm.OLLAMA_MODEL}' "
                "is not pulled. Ollama runs on the host, not in Docker."
            ),
        )

    # Load the full text only now — the list query above deliberately omits it.
    triples = []
    with db.get_connection() as conn:
        for document in parsed:
            row = db.get_document_with_text(conn, str(document["id"]), organization_id)
            if row and row["extracted_text"]:
                triples.append((str(row["id"]), row["filename"], row["extracted_text"]))

    if not triples:
        raise HTTPException(
            status_code=409, detail="Parsed documents contain no extractable text."
        )

    _set_progress(carrier_id, state="running", done=0, total=None, stage="chunking")

    def on_progress(done: int, total: int) -> None:
        _set_progress(
            carrier_id, state="running", done=done, total=total, stage="reading sections"
        )

    started = time.perf_counter()
    try:
        result = summarise_carrier(triples, on_progress=on_progress)
    except LLMError as exc:
        _set_progress(carrier_id, state="error", detail=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Summarise failed")
        _set_progress(carrier_id, state="error", detail=str(exc))
        raise HTTPException(status_code=500, detail=f"Summarise failed: {exc}")

    _set_progress(carrier_id, state="running", stage="saving")

    # Declared out here so the indexing step below can reuse the same
    # per-document grouping without rebuilding it.
    by_document: dict[str, list[dict]] = {}

    stats = {
        **result["stats"],
        "documents": len(triples),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }

    try:
        with db.get_connection() as conn:
            row = db.save_summary(
                conn,
                carrier_id=carrier_id,
                organization_id=organization_id,
                summary=result["summary"],
                company_overview=result["company_overview"],
                requirements=result["requirements"],
                plans=result["plans"],
                exclusions=result["exclusions"],
                key_points=result["key_points"],
                stats=stats,
                model=llm.OLLAMA_MODEL,
            )

            # Chunks are saved per document so a later phase can embed these
            # exact pieces. Grouped here because the map-reduce pooled them.
            by_document.clear()
            for chunk in result["chunks"]:
                if chunk.get("document_id"):
                    by_document.setdefault(chunk["document_id"], []).append(chunk)
            for document_id, chunks in by_document.items():
                # Re-index within the document: the pooled index is unique
                # across the carrier, but the chunks table is keyed
                # (document_id, chunk_index) and expects a per-document
                # sequence starting at 0.
                for position, chunk in enumerate(chunks):
                    chunk["index"] = position
                db.save_chunks(conn, document_id, chunks)
    except Exception as exc:
        logger.exception("Could not save summary")
        _set_progress(carrier_id, state="error", detail=str(exc))
        raise HTTPException(status_code=500, detail=f"Could not save summary: {exc}")

    # Index for retrieval. Best-effort on purpose: the summary took real model
    # time and is already saved, so a Chroma outage here must not fail the
    # request. The index is rebuildable from Postgres at any time, and /health
    # reports when it is empty.
    _set_progress(carrier_id, state="running", stage="indexing")
    indexed = 0
    index_error = None
    try:
        for document_id, chunks in by_document.items():
            indexed += vectorstore.index_document(
                document_id=document_id,
                carrier_id=carrier_id,
                carrier_name=carrier["name"],
                filename=next(
                    (c["filename"] for c in chunks if c.get("filename")), "document"
                ),
                organization_id=organization_id,
                chunks=chunks,
            )
    except (VectorStoreError, LLMError) as exc:
        # Deliberately not fatal — the briefing is already saved and cost real
        # model time. But it must be VISIBLE: an invisible failure here leaves
        # a carrier marked "Briefed" that matching and chat cannot see, which
        # is exactly the confusing state this comment exists to prevent.
        if not llm.embedding_available():
            index_error = (
                f"Briefing saved, but it could not be added to the search index: "
                f"the embedding model '{llm.EMBED_MODEL}' is not pulled. "
                f"Run: ollama pull {llm.EMBED_MODEL} — then press Rebuild search "
                f"index in Settings."
            )
        else:
            index_error = (
                f"Briefing saved, but it could not be added to the search index "
                f"({exc}). Press Rebuild search index in Settings to retry."
            )
        logger.warning("Indexing failed after summarise: %s", exc)

    _set_progress(carrier_id, state="done", done=stats.get("chunks_total"), stage="done")

    payload = _summary_response(row)
    payload["indexed_chunks"] = indexed
    payload["index_error"] = index_error
    payload["recognised"] = result.get("recognised", True)
    payload["confidence"] = result.get("confidence")
    payload["looks_like_policy"] = result["looks_like_policy"]
    # Sentence-level provenance is returned on generation but not stored: it
    # is only useful next to the prose the model just wrote, and a later hand
    # edit would leave it pointing at sentences that no longer exist.
    payload["summary_parts"] = result["summary_parts"]
    return payload


@router.get("/carriers/{carrier_id}/summary")
def read_summary(carrier_id: str, x_organization_id: str | None = Header(None)):
    carrier_id = _validate_uuid(carrier_id, "carrier id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_summary(conn, carrier_id, organization_id)
    if not row:
        raise HTTPException(status_code=404, detail="No summary yet for this carrier.")
    return _summary_response(row)


@router.put("/carriers/{carrier_id}/summary")
def edit_summary(carrier_id: str, body: SummaryEdit,
                  x_organization_id: str | None = Header(None)):
    """
    Save a human edit to the summary prose.

    The structured fields are left alone: they are what later phases read,
    and letting free-text editing silently change a requirements list would
    make the briefing and the machine-readable data disagree.
    """
    carrier_id = _validate_uuid(carrier_id, "carrier id")

    text = (body.summary or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Summary cannot be empty.")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.update_summary_text(conn, carrier_id, organization_id, text)
    if not row:
        raise HTTPException(status_code=404, detail="No summary yet for this carrier.")
    return _summary_response(row)


@router.get("/documents/{document_id}/chunks")
def read_chunks(document_id: str, x_organization_id: str | None = Header(None)):
    """The stored chunks for one document — useful for checking chunking."""
    document_id = _validate_uuid(document_id, "document id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.get_chunks_for_document(conn, document_id, organization_id)
    return {
        "document_id": document_id,
        "chunks": [
            {
                "index": r["chunk_index"],
                "section_title": r["section_title"],
                "page_start": r["page_start"],
                "chars": len(r["text"]),
                "summary": r["summary"],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# Phase 2 — reindex
# --------------------------------------------------------------------------


def _require_search_ready():
    """
    Check the search index can actually answer, and say precisely what is
    wrong when it cannot.

    The three failures look identical from `count() == 0` but need different
    actions from the user, so they are told apart by asking Postgres what
    *should* be indexed:

      * Chroma unreachable      -> a service is down, nothing to do in the app
      * no carrier briefed yet  -> brief one first
      * briefed but not indexed -> press Rebuild search index

    The previous single message covered all three with "brief a carrier, then
    rebuild", which read as nonsense to anyone who had already briefed one.
    """
    if not vectorstore.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"ChromaDB is not reachable at {CHROMA_HOST}:{CHROMA_PORT}. "
                "Search and matching need it — check the container is running."
            ),
        )

    if vectorstore.count() > 0:
        return

    with db.get_connection() as conn:
        briefed = db.all_indexable_documents(conn)

    if not briefed:
        raise HTTPException(
            status_code=409,
            detail=(
                "No carrier has been briefed yet. Upload a carrier's documents "
                "and press Generate briefing first."
            ),
        )

    if not llm.embedding_available():
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(briefed)} document(s) are briefed but none are indexed, "
                f"because the embedding model '{llm.EMBED_MODEL}' is not pulled. "
                f"Run: ollama pull {llm.EMBED_MODEL} — then press Rebuild search "
                "index in Settings."
            ),
        )

    raise HTTPException(
        status_code=409,
        detail=(
            f"{len(briefed)} document(s) are briefed but the search index is "
            "empty. Press Rebuild search index in Settings."
        ),
    )


@router.post("/reindex")
def reindex():
    """
    Rebuild the vector index from Postgres.

    This is what makes the Chroma volume disposable. No model reasoning is
    repeated — only re-embedding, which is cheap — because the chunks were
    saved when the briefing ran. Covers EVERY carrier, not just one: a
    reindex that quietly skipped some would leave search half-broken while
    looking fixed.
    """
    if not vectorstore.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"ChromaDB is not reachable at {CHROMA_HOST}:{CHROMA_PORT}.",
        )
    if not llm.embedding_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"The embedding model '{llm.EMBED_MODEL}' is not pulled. "
                f"Run: ollama pull {llm.EMBED_MODEL}"
            ),
        )

    with db.get_connection() as conn:
        documents = db.all_indexable_documents(conn)

    if not documents:
        return {"documents": 0, "chunks": 0, "failed": [], "detail":
                "No briefed documents to index yet."}

    total = 0
    failed = []
    for document in documents:
        document_id = str(document["document_id"])
        with db.get_connection() as conn:
            # Deliberately unscoped — see get_chunks_for_document_unscoped's
            # docstring: /reindex rebuilds the search index across every
            # organization at once, by design.
            rows = db.get_chunks_for_document_unscoped(conn, document_id)
        chunks = [
            {
                "index": r["chunk_index"],
                "section_title": r["section_title"],
                "page_start": r["page_start"],
                "text": r["text"],
            }
            for r in rows
        ]
        try:
            total += vectorstore.index_document(
                document_id=document_id,
                carrier_id=str(document["carrier_id"]),
                carrier_name=document["carrier_name"],
                filename=document["filename"],
                organization_id=str(document["organization_id"]),
                chunks=chunks,
            )
        except (VectorStoreError, LLMError) as exc:
            # Keep going: one bad document should not abort the rebuild.
            failed.append({"filename": document["filename"], "error": str(exc)})
            logger.warning("Reindex failed for %s: %s", document["filename"], exc)

    return {
        "documents": len(documents) - len(failed),
        "chunks": total,
        "failed": failed,
    }
