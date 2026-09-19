"""
Insurance Intelligence — Backend (Phase 1)

Phase 1 does three things:
  1. Accept uploaded insurer / broker documents and parse them to text.
  2. Summarise them with a local model into a structured briefing —
     company overview, requirements, plans, exclusions, key facts.
  3. Store all of it in Postgres, and serve it to the UI.

Everything uploaded here is the CARRIER's own paperwork. Real customer data
does not enter this system until Phase 2, and will arrive through its own
tables and a tokenisation layer rather than through this upload path.

/health probes every dependency rather than just reporting the API is alive,
because the most common failure in this stack is Ollama not running on the
host — which is invisible until a summarise call fails minutes later.
"""
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import chat
import db
import drafting
import export_docx
import extraction
import feedback
import gaps
from psycopg.types.json import Jsonb as Jsonb_
import llm
import matching
import tokenise
import vectorstore
import verify
from llm import LLMError
from parsers import SUPPORTED_EXTENSIONS, ParseError, extension_of, parse_file
from summarise import summarise_carrier
from vectorstore import VectorStoreError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = os.getenv("CHROMA_PORT", "8000")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class SummaryEdit(BaseModel):
    """Body of PUT /carriers/{id}/summary."""

    summary: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Create tables on startup.

    Postgres may still be booting when the backend starts, so this retries
    rather than crashing the container on the first failed connection.
    """
    for attempt in range(1, 11):
        try:
            db.init_schema()
            logger.info("Database schema ready")
            break
        except Exception as exc:
            logger.warning("Schema init attempt %s/10 failed: %s", attempt, exc)
            time.sleep(2)
    else:
        logger.error("Could not initialise schema — /health will show the error")
    yield


app = FastAPI(title="Insurance Intelligence API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Read from the environment so a deployed instance can allow its own
    # public address without a code change. A fixed localhost-only list
    # works locally but silently blocks every request once the app is
    # reachable from a real IP or domain — which looks exactly like a
    # backend outage from the browser's side, even though /health is fine.
    #
    # Comma-separated, so a server can allow both an IP and a domain during
    # a DNS cutover.
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS", "http://localhost:5183,http://localhost:3000"
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def _timed(fn):
    """Run a probe, adding how long it took to the result."""
    start = time.perf_counter()
    result = fn()
    result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return result


def check_postgres():
    """Open a real connection and run a query. Anything less isn't proof."""

    def probe():
        with psycopg.connect(db.DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                version = cur.fetchone()[0]
        return {"status": "connected", "detail": " ".join(version.split()[:2])}

    try:
        return _timed(probe)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "latency_ms": None}


def check_chroma():
    """
    ChromaDB, probed through the same client the app uses rather than a bare
    HTTP ping — so a reachable server with an unusable collection still
    reports as broken.
    """

    def probe():
        if not vectorstore.is_available():
            raise RuntimeError(
                f"could not reach ChromaDB at {CHROMA_HOST}:{CHROMA_PORT}"
            )
        indexed = vectorstore.count()
        if indexed == 0:
            return {
                "status": "connected",
                "detail": (
                    "reachable, but nothing indexed. Matching and chat need "
                    "an index — rebuild it below."
                ),
            }
        return {"status": "connected", "detail": f"{indexed} chunks indexed"}

    try:
        return _timed(probe)
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "latency_ms": None}


def check_ollama():
    """
    List locally pulled models, and say plainly if the configured one is
    missing — "reachable" alone would be misleading when summarising would
    still fail for want of the model.
    """

    def probe():
        resp = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        if not models:
            return {"status": "error", "detail": "reachable, but no models pulled"}
        if not llm.is_available():
            return {
                "status": "error",
                "detail": (
                    f"'{llm.OLLAMA_MODEL}' is not pulled. Run: "
                    f"ollama pull {llm.OLLAMA_MODEL}"
                ),
            }
        return {"status": "connected", "detail": ", ".join(models)}

    try:
        return _timed(probe)
    except Exception as exc:
        return {
            "status": "error",
            "detail": (
                f"Could not reach Ollama at {OLLAMA_HOST}. It runs on the host, "
                f"not in Docker — check it is running. ({str(exc)[:120]})"
            ),
            "latency_ms": None,
        }


@app.get("/health")
def health():
    """Probe every dependency, so a failure names the specific service."""
    services = {
        "api": {"status": "connected", "detail": "FastAPI running", "latency_ms": 0.0},
        "postgres": check_postgres(),
        "chromadb": check_chroma(),
        "ollama": check_ollama(),
    }
    all_ok = all(s["status"] == "connected" for s in services.values())
    return {"status": "ok" if all_ok else "degraded", "services": services}


@app.get("/supported-types")
def supported_types():
    return {"extensions": sorted(SUPPORTED_EXTENSIONS)}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _validate_uuid(value: str, label: str) -> str:
    """Reject a malformed id with 400 rather than letting Postgres raise."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {label}.")


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


@app.post("/carriers", status_code=201)
async def create_carrier(
    files: list[UploadFile] = File(...),
    name: str | None = Form(None),
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
            carrier_id = db.create_carrier(conn, carrier_name)

            for filename, data in payloads:
                try:
                    parsed = parse_file(filename, data)
                except ParseError as exc:
                    row = db.create_document(
                        conn,
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


@app.get("/carriers")
def list_carriers():
    with db.get_connection() as conn:
        rows = db.list_carriers(conn)
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


@app.get("/carriers/{carrier_id}")
def get_carrier(carrier_id: str):
    carrier_id = _validate_uuid(carrier_id, "carrier id")

    with db.get_connection() as conn:
        carrier = db.get_carrier(conn, carrier_id)
        if not carrier:
            raise HTTPException(status_code=404, detail="Carrier not found.")
        documents = db.list_documents_for_carrier(conn, carrier_id)
        summary = db.get_summary(conn, carrier_id)

    parsed = [d for d in documents if d["status"] == "parsed"]

    # Reported on every visit, not just after a briefing run: an indexing
    # failure is otherwise silent, and surfaces much later as a match that
    # inexplicably ignores this carrier.
    indexed_chunks = (
        vectorstore.count_for_carrier(carrier_id)
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


@app.get("/documents/{document_id}/text")
def get_document_text(document_id: str):
    """The full extracted text, for checking what the parser actually got."""
    document_id = _validate_uuid(document_id, "document id")

    with db.get_connection() as conn:
        row = db.get_document_with_text(conn, document_id)

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


@app.delete("/carriers/{carrier_id}")
def delete_carrier(carrier_id: str):
    carrier_id = _validate_uuid(carrier_id, "carrier id")
    with db.get_connection() as conn:
        if not db.delete_carrier(conn, carrier_id):
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


@app.get("/carriers/{carrier_id}/summary/progress")
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


@app.post("/carriers/{carrier_id}/summary")
def create_summary(carrier_id: str):
    """
    Run the map-reduce over every parsed document for this carrier.

    Synchronous: this can take minutes, and the client polls
    /summary/progress alongside it. Kept simple deliberately — a task queue
    is worth adding when several people use this at once, not before.
    """
    carrier_id = _validate_uuid(carrier_id, "carrier id")

    with db.get_connection() as conn:
        carrier = db.get_carrier(conn, carrier_id)
        if not carrier:
            raise HTTPException(status_code=404, detail="Carrier not found.")
        documents = db.list_documents_for_carrier(conn, carrier_id)

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
            row = db.get_document_with_text(conn, str(document["id"]))
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


@app.get("/carriers/{carrier_id}/summary")
def read_summary(carrier_id: str):
    carrier_id = _validate_uuid(carrier_id, "carrier id")
    with db.get_connection() as conn:
        row = db.get_summary(conn, carrier_id)
    if not row:
        raise HTTPException(status_code=404, detail="No summary yet for this carrier.")
    return _summary_response(row)


@app.put("/carriers/{carrier_id}/summary")
def edit_summary(carrier_id: str, body: SummaryEdit):
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
        row = db.update_summary_text(conn, carrier_id, text)
    if not row:
        raise HTTPException(status_code=404, detail="No summary yet for this carrier.")
    return _summary_response(row)


@app.get("/documents/{document_id}/chunks")
def read_chunks(document_id: str):
    """The stored chunks for one document — useful for checking chunking."""
    document_id = _validate_uuid(document_id, "document id")
    with db.get_connection() as conn:
        rows = db.get_chunks_for_document(conn, document_id)
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


@app.post("/reindex")
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
            rows = db.get_chunks_for_document(conn, document_id)
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


# --------------------------------------------------------------------------
# Phase 2 — clients
# --------------------------------------------------------------------------


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


def _client_response(row: dict) -> dict:
    out = {
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


@app.post("/clients", status_code=201)
def create_client(body: ClientIn):
    if not body.business_name.strip():
        raise HTTPException(status_code=400, detail="A business name is required.")

    profile, mapping = _build_redacted_profile(body, {})
    fields = {f: getattr(body, f, None) for f in db.CLIENT_FIELDS}

    with db.get_connection() as conn:
        row = db.create_client(
            conn,
            fields=fields,
            categories=body.categories,
            redacted_profile=profile,
        )
        db.save_client_tokens(conn, str(row["id"]), mapping)
        if body.documents_held or body.documents_missing:
            db.set_client_documents(
                conn,
                str(row["id"]),
                held=body.documents_held,
                missing=body.documents_missing,
            )
            row["documents_held"] = body.documents_held
            row["documents_missing"] = body.documents_missing

    return _client_response(row)


@app.get("/clients")
def list_clients():
    with db.get_connection() as conn:
        rows = db.list_clients(conn)
    return {
        "clients": [
            {
                "client_id": str(r["id"]),
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


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        row = db.get_client(conn, client_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        latest = db.get_latest_match(conn, client_id)

    payload = _client_response(row)
    payload["has_match"] = latest is not None
    return payload


@app.put("/clients/{client_id}")
def update_client(client_id: str, body: ClientIn):
    client_id = _validate_uuid(client_id, "client id")
    if not body.business_name.strip():
        raise HTTPException(status_code=400, detail="A business name is required.")

    with db.get_connection() as conn:
        if not db.get_client(conn, client_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        # Seed with the existing map so placeholders stay stable across edits:
        # a match run yesterday and one today should refer to the same
        # {{BUSINESS_1}}, not to two different tokens for one company.
        existing = db.get_client_tokens(conn, client_id)

    profile, mapping = _build_redacted_profile(body, existing)
    fields = {f: getattr(body, f, None) for f in db.CLIENT_FIELDS}

    with db.get_connection() as conn:
        row = db.update_client(
            conn,
            client_id,
            fields=fields,
            categories=body.categories,
            redacted_profile=profile,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        db.save_client_tokens(conn, client_id, mapping)
        db.set_client_documents(
            conn,
            client_id,
            held=body.documents_held,
            missing=body.documents_missing,
        )
        row["documents_held"] = body.documents_held
        row["documents_missing"] = body.documents_missing

    return _client_response(row)


@app.delete("/clients/{client_id}")
def delete_client(client_id: str):
    """
    Delete a client and everything derived from them.

    Postgres cascades to the token map and match history. Nothing about a
    client is ever written to Chroma, so there is nothing to clean up there —
    which is the point of never indexing client text.
    """
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        if not db.delete_client(conn, client_id):
            raise HTTPException(status_code=404, detail="Client not found.")
    return {"deleted": client_id}


@app.get("/clients/{client_id}/profile")
def get_redacted_profile(client_id: str):
    """
    Exactly what the model is shown for this client.

    Exposed so the team can see the redaction working rather than take it on
    trust — the placeholder map itself is deliberately NOT returned.
    """
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        row = db.get_client(conn, client_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        mapping = db.get_client_tokens(conn, client_id)

    return {
        "client_id": client_id,
        "redacted_profile": row.get("redacted_profile") or "",
        "placeholders": sorted(mapping.keys()),
    }


# --------------------------------------------------------------------------
# Phase 2 — matching
# --------------------------------------------------------------------------


@app.post("/clients/{client_id}/match")
def run_match(client_id: str):
    """Rank carrier plans against one client's situation."""
    client_id = _validate_uuid(client_id, "client id")

    with db.get_connection() as conn:
        row = db.get_client(conn, client_id)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found.")
        # Seeded so the notes redact to the SAME placeholders as the stored
        # profile. A fresh Redactor would number independently, and the model
        # would see {{PAN_1}} meaning one thing in the profile and another in
        # the notes — two entities where there is one.
        token_map = db.get_client_tokens(conn, client_id)

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


@app.get("/clients/{client_id}/match")
def read_match(client_id: str):
    """The most recent match run for this client."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        row = db.get_latest_match(conn, client_id)
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


# --------------------------------------------------------------------------
# Phase 2 — chat
# --------------------------------------------------------------------------


class ChatIn(BaseModel):
    question: str
    carrier_id: str | None = None


@app.post("/chat")
def chat_ask(body: ChatIn):
    """Ask a question across every indexed carrier document, or just one."""
    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )
    _require_search_ready()

    carrier_id = (
        _validate_uuid(body.carrier_id, "carrier id") if body.carrier_id else None
    )

    try:
        return chat.ask(body.question, carrier_id=carrier_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/clients/extract")
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
# Phase 3 — gap analysis
# --------------------------------------------------------------------------


class DocumentLists(BaseModel):
    """The paperwork a client holds, and what they have said they lack."""

    held: list[str] = []
    missing: list[str] = []


@app.put("/clients/{client_id}/documents")
def set_documents(client_id: str, body: DocumentLists):
    """
    Record what paperwork the client has.

    Separate from the intake form because it changes on its own schedule —
    a broker chases a document, it arrives, and the checklist moves without
    anything else about the client changing.
    """
    client_id = _validate_uuid(client_id, "client id")

    def clean(values: list[str]) -> list[str]:
        out = []
        for value in values:
            name = " ".join(str(value).split()).strip(" .-")
            if name and len(name) <= 120 and name.casefold() not in {
                o.casefold() for o in out
            }:
                out.append(name)
        return out[:40]

    held, missing = clean(body.held), clean(body.missing)

    # A document cannot be both held and missing. The held list wins: it is
    # the one a broker edits after chasing something down, so it is the more
    # recent statement of fact.
    held_keys = {h.casefold() for h in held}
    missing = [m for m in missing if m.casefold() not in held_keys]

    with db.get_connection() as conn:
        if not db.get_client(conn, client_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        db.set_client_documents(conn, client_id, held=held, missing=missing)

    return {"client_id": client_id, "documents_held": held, "documents_missing": missing}


def _carriers_from_match(conn, latest: dict | None) -> list[str]:
    """
    Which carriers a match points at, in best-fit order.

    Tried in order, because each fallback is less precise than the last:

      1. carrier_id on a cited source. Exact, but only present when the model
         returned usable citation numbers — with a small model it often does
         not, and deriving the carrier from citations alone was why gap
         analysis used to fail on a perfectly good match.
      2. The carrier NAME the match itself names, resolved against the
         carriers on record. Every surviving match has one, so this works
         whenever the match does.

    Returns an empty list only when neither yields anything, leaving the
    caller to widen the net rather than refuse.
    """
    if not latest:
        return []

    targets: list[str] = []
    for match in latest.get("results") or []:
        for source in match.get("sources") or []:
            identifier = source.get("carrier_id")
            if identifier and identifier not in targets:
                targets.append(identifier)

    if targets:
        return targets

    # Fall back to matching on name.
    by_name = {
        " ".join(c["name"].casefold().split()): str(c["id"])
        for c in db.list_carriers(conn)
        if c.get("has_summary")
    }
    for match in latest.get("results") or []:
        name = " ".join(str(match.get("carrier") or "").casefold().split())
        identifier = by_name.get(name)
        if not identifier and name:
            # Loose match: the model sometimes writes "Suvidha Trust Bank Ltd"
            # for a carrier recorded as "Suvidha Trust Bank".
            for known, known_id in by_name.items():
                if known and (known in name or name in known):
                    identifier = known_id
                    break
        if identifier and identifier not in targets:
            targets.append(identifier)

    return targets


@app.post("/clients/{client_id}/gaps")
def run_gap_analysis(client_id: str, carrier_id: str | None = None):
    """
    Check a client's file against what each carrier requires.

    Runs against the carriers the latest match recommended, since those are
    the ones actually in play. Pass carrier_id to check one specific carrier
    instead — useful before a match has been run, or to re-check after
    chasing a document.
    """
    client_id = _validate_uuid(client_id, "client id")

    with db.get_connection() as conn:
        client = db.get_client(conn, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        # Attached documents are the strongest evidence a requirement is met,
        # so they travel with the client record into the analysis. Newest per
        # requirement wins: re-uploading after a rejection should replace the
        # earlier verdict, not sit behind it.
        attachments = []
        seen_requirements = set()
        for row in db.list_client_documents(conn, client_id):
            key = " ".join(row["requirement"].casefold().split())
            if key in seen_requirements:
                continue
            seen_requirements.add(key)
            attachments.append(
                {
                    "requirement": row["requirement"],
                    "filename": row["filename"],
                    "verdict": row["verdict"],
                }
            )
        client["_attachments"] = attachments

        if carrier_id:
            targets = [_validate_uuid(carrier_id, "carrier id")]
        else:
            latest = db.get_latest_match(conn, client_id)
            targets = _carriers_from_match(conn, latest)

        if not targets:
            # Nothing could be resolved from the match — usually because the
            # model returned recommendations without usable citations. Rather
            # than refuse, check against every briefed carrier: a broker who
            # asked what paperwork is needed is better served by a slightly
            # wider answer than by an error.
            with_summaries = [
                str(c["id"])
                for c in db.list_carriers(conn)
                if c.get("has_summary")
            ]
            targets = with_summaries[:5]

        if not targets:
            raise HTTPException(
                status_code=409,
                detail=(
                    "No carrier has a briefing yet, so there are no "
                    "requirements to check against. Add a carrier and "
                    "generate its briefing first."
                ),
            )

        carriers = []
        for identifier in targets:
            carrier = db.get_carrier(conn, identifier)
            summary = db.get_summary(conn, identifier)
            if carrier and summary:
                carriers.append((carrier, summary))

    if not carriers:
        raise HTTPException(
            status_code=409,
            detail=(
                "None of those carriers has a briefing yet. Generate one so "
                "its requirements are on record."
            ),
        )

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )

    reports = []
    failures = []
    for carrier, summary in carriers:
        name = carrier["name"]
        try:
            result = gaps.analyse(
                client=client,
                carrier_name=name,
                requirements=summary.get("requirements") or [],
            )
        except LLMError as exc:
            # One carrier failing should not lose the others.
            failures.append({"carrier_name": name, "error": str(exc)})
            logger.warning("Gap analysis failed for %s: %s", name, exc)
            continue
        except Exception as exc:
            failures.append({"carrier_name": name, "error": str(exc)})
            logger.exception("Gap analysis failed for %s", name)
            continue

        with db.get_connection() as conn:
            saved = db.save_gap_report(
                conn,
                client_id=client_id,
                carrier_id=str(carrier["id"]),
                items=result["items"],
                summary=result["summary"],
                stats=result["stats"],
                model=llm.OLLAMA_MODEL,
            )

        reports.append(
            {
                "report_id": str(saved["id"]),
                "carrier_id": str(carrier["id"]),
                "carrier_name": name,
                "items": result["items"],
                "summary": result["summary"],
                "stats": result["stats"],
                "created_at": saved["created_at"].isoformat(),
            }
        )

    if not reports:
        # Every carrier failed. Report all of them rather than the first: with
        # several carriers in play, one message leaves the user fixing one
        # problem at a time without knowing how many remain.
        if failures:
            detail = " · ".join(f"{f['carrier_name']}: {f['error']}" for f in failures)
        else:
            detail = "Gap analysis produced nothing."
        raise HTTPException(status_code=502, detail=detail)

    return {"client_id": client_id, "reports": reports, "failures": failures}


@app.get("/clients/{client_id}/gaps")
def read_gap_analysis(client_id: str):
    """The most recent report per carrier for this client."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        rows = db.get_latest_gap_reports(conn, client_id)

    return {
        "client_id": client_id,
        "reports": [
            {
                "report_id": str(r["id"]),
                "carrier_id": str(r["carrier_id"]),
                "carrier_name": r["carrier_name"],
                "items": r["items"],
                "summary": r["summary"],
                "stats": r["stats"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# Phase 3.5 — documents the client actually sent
# --------------------------------------------------------------------------


@app.post("/clients/{client_id}/documents/upload")
async def upload_client_document(
    client_id: str,
    requirement: str = Form(...),
    file: UploadFile = File(...),
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
        client = db.get_client(conn, client_id)
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

    with db.get_connection() as conn:
        row = db.save_client_document(
            conn,
            client_id=client_id,
            requirement=requirement,
            filename=filename,
            file_type=extension_of(filename),
            size_bytes=len(data),
            chars=parsed.chars,
            extracted_text=parsed.text,
            verification=result,
        )

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


@app.get("/clients/{client_id}/documents")
def list_documents(client_id: str):
    """Every document attached for this client."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        rows = db.list_client_documents(conn, client_id)
    return {
        "client_id": client_id,
        "documents": [_client_document_response(r) for r in rows],
    }


@app.delete("/clients/{client_id}/documents/{document_id}")
def remove_client_document(client_id: str, document_id: str):
    _validate_uuid(client_id, "client id")
    document_id = _validate_uuid(document_id, "document id")
    with db.get_connection() as conn:
        if not db.delete_client_document(conn, document_id):
            raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted": document_id}


# --------------------------------------------------------------------------
# Phase 4, part 1 — drafted paperwork
# --------------------------------------------------------------------------


class DraftEdit(BaseModel):
    title: str
    body: str


def _draft_response(row: dict) -> dict:
    return {
        "draft_id": str(row["id"]),
        "client_id": str(row["client_id"]),
        "carrier_id": str(row["carrier_id"]) if row.get("carrier_id") else None,
        "carrier_name": row.get("carrier_name"),
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "warnings": row.get("warnings") or [],
        "stats": row.get("stats") or {},
        "model": row.get("model"),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@app.get("/draft-kinds")
def draft_kinds():
    """The document kinds that can be drafted, for the UI to offer."""
    return {"kinds": [{"id": k, "label": v} for k, v in drafting.KINDS.items()]}


@app.post("/clients/{client_id}/drafts")
def create_draft(client_id: str, carrier_id: str | None = None,
                 kind: str = "submission"):
    """
    Write a document for this client.

    Produces a DRAFT and nothing else — there is no send path here, by
    design. A licensed human is accountable for what reaches an insurer.

    Without carrier_id, the best-fitting carrier from the latest match is
    used, since that is the one a broker would submit to first.
    """
    client_id = _validate_uuid(client_id, "client id")

    kind = (kind or "submission").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown document kind. Choose one of: {', '.join(drafting.KINDS)}.",
        )

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )

    with db.get_connection() as conn:
        client = db.get_client(conn, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        token_map = db.get_client_tokens(conn, client_id)
        match = db.get_latest_match(conn, client_id)

        # Guidance learned from how this team edits its own drafts, if any.
        style_row = db.get_house_style(conn, kind)
        house_style = style_row["guidance"] if style_row else None

        target = _validate_uuid(carrier_id, "carrier id") if carrier_id else None
        if not target and match:
            # First carrier cited by the best-fitting match.
            for result in match["results"]:
                for source in result.get("sources") or []:
                    if source.get("carrier_id"):
                        target = source["carrier_id"]
                        break
                if target:
                    break

        carrier = db.get_carrier(conn, target) if target else None
        carrier_name = carrier["name"] if carrier else None

        attachments = []
        seen = set()
        for row in db.list_client_documents(conn, client_id):
            key = " ".join(row["requirement"].casefold().split())
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                {
                    "requirement": row["requirement"],
                    "filename": row["filename"],
                    "verdict": row["verdict"],
                    "doc_type": row.get("doc_type"),
                    "key_facts": row.get("key_facts") or [],
                }
            )

        gap_items = []
        for report in db.get_latest_gap_reports(conn, client_id):
            # A chase email goes to the client about everything outstanding,
            # so it takes every carrier's report. The carrier-facing kinds
            # take only the carrier they are addressed to.
            if kind != "chase" and target and str(report["carrier_id"]) != str(target):
                continue
            gap_items.extend(report["items"] or [])

        if kind == "chase":
            # Deduplicate: two carriers asking for the same document should
            # produce one line in the email, not two.
            seen_requirements = set()
            deduped = []
            for item in gap_items:
                key = " ".join(str(item.get("requirement", "")).casefold().split())
                if not key or key in seen_requirements:
                    continue
                seen_requirements.add(key)
                deduped.append(item)
            gap_items = deduped

    try:
        if kind == "chase":
            # Addressed to the client, so it needs the gap report rather than
            # the carrier dossier. Every outstanding item across carriers is
            # chased at once — one email, not one per insurer.
            result = drafting.draft_chase(
                client=client, gap_items=gap_items, token_map=token_map,
                house_style=house_style,
            )
        elif kind == "proposal":
            result = drafting.draft_proposal(
                client=client,
                carrier_name=carrier_name,
                match=match,
                attachments=attachments,
                gap_items=gap_items,
                token_map=token_map,
                house_style=house_style,
            )
        else:
            result = drafting.draft_submission(
                client=client,
                carrier_name=carrier_name,
                match=match,
                attachments=attachments,
                gap_items=gap_items,
                token_map=token_map,
                house_style=house_style,
            )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Drafting failed")
        raise HTTPException(status_code=500, detail=f"Drafting failed: {exc}")

    with db.get_connection() as conn:
        row = db.save_draft(
            conn,
            client_id=client_id,
            carrier_id=None if kind == "chase" else target,
            kind=kind,
            title=result["title"],
            body=result["body"],
            body_redacted=result["body_redacted"],
            warnings=result["warnings"],
            stats=result["stats"],
            model=llm.OLLAMA_MODEL,
        )
        row["carrier_name"] = carrier_name
        db.log_draft_event(
            conn,
            draft_id=str(row["id"]),
            event="created",
            actor=llm.OLLAMA_MODEL,
            detail=(
                f"{drafting.KINDS[kind]} drafted"
                + (f" for {carrier_name}." if carrier_name and kind != "chase" else ".")
            ),
        )

    return _draft_response(row)


@app.get("/clients/{client_id}/drafts")
def list_client_drafts(client_id: str):
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        rows = db.list_drafts(conn, client_id)
    return {"client_id": client_id, "drafts": [_draft_response(r) for r in rows]}


@app.put("/drafts/{draft_id}")
def edit_draft(draft_id: str, body: DraftEdit):
    """
    Save a human edit.

    Warnings are recomputed on the edited text rather than carried over: an
    edit that fixes a stray placeholder should clear its warning, and one
    that introduces a bracket placeholder should raise a new one.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    title = " ".join((body.title or "").split())
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="A draft cannot be empty.")

    warnings = drafting._warnings_for(text)

    with db.get_connection() as conn:
        before = db.get_draft(conn, draft_id)
        if not before:
            raise HTTPException(status_code=404, detail="Draft not found.")

        row = db.update_draft_body(
            conn, draft_id, title=title[:200] or "Untitled draft",
            body=text, warnings=warnings,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")

        # An edit after sign-off is worth calling out in the trail: the
        # approval that came before it no longer covers this text.
        detail = "Edited by hand."
        if before["status"] == "approved":
            detail = "Edited after approval — the earlier sign-off no longer applies."
        db.log_draft_event(conn, draft_id=draft_id, event="edited", detail=detail)

        # The edit itself is the feedback signal — what was drafted against
        # what a person actually wanted. Stored whole so guidance can be
        # re-derived later from better examples than we have today.
        stats = feedback.summarise_edit(before["body"], text)
        db.save_correction(
            conn,
            kind="draft_edit",
            client_id=str(before["client_id"]),
            carrier_id=str(before["carrier_id"]) if before.get("carrier_id") else None,
            subject=before["kind"],
            before_text=before["body"],
            after_text=text,
            meta=stats,
        )

        full = db.get_draft(conn, draft_id)

    return _draft_response(full or row)


@app.delete("/drafts/{draft_id}")
def remove_draft(draft_id: str):
    draft_id = _validate_uuid(draft_id, "draft id")
    with db.get_connection() as conn:
        if not db.delete_draft(conn, draft_id):
            raise HTTPException(status_code=404, detail="Draft not found.")
    return {"deleted": draft_id}


# --------------------------------------------------------------------------
# Phase 4, part 2 — review and approval
# --------------------------------------------------------------------------


class Approval(BaseModel):
    """Who is signing this off. Self-declared — there is no user system."""

    approved_by: str
    note: str | None = None


class Revocation(BaseModel):
    reason: str | None = None


def _blocking(warnings: list[dict]) -> list[dict]:
    """Warnings severe enough to stop a sign-off."""
    return [w for w in warnings or [] if w.get("level") == "error"]


@app.get("/drafts/{draft_id}")
def read_draft(draft_id: str):
    """One draft, with its full history."""
    draft_id = _validate_uuid(draft_id, "draft id")
    with db.get_connection() as conn:
        row = db.get_draft(conn, draft_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")
        events = db.get_draft_events(conn, draft_id)

    payload = _draft_response(row)
    payload["history"] = [
        {
            "event": e["event"],
            "actor": e["actor"],
            "detail": e["detail"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


@app.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: str, body: Approval):
    """
    Sign a draft off.

    Approval is a human act and is recorded as one. Two guards:

      * A name is required. An approval with nobody attached to it is not an
        approval, and months later "who agreed this went out" is the question
        that actually gets asked.
      * Error-level warnings block it. An unreplaced placeholder means the
        text is wrong, and signing off wrong text is exactly what a review
        step exists to prevent. Lesser warnings are shown but do not block —
        the reviewer can see them and judge.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    approver = " ".join((body.approved_by or "").split())
    if not approver:
        raise HTTPException(
            status_code=400,
            detail="Enter your name to sign this off.",
        )

    with db.get_connection() as conn:
        row = db.get_draft(conn, draft_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")

        blocking = _blocking(row.get("warnings"))
        if blocking:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This draft cannot be approved yet: "
                    + " ".join(w["text"] for w in blocking)
                ),
            )

        if row["status"] == "approved":
            raise HTTPException(
                status_code=409, detail="This draft is already approved."
            )

        db.set_draft_status(conn, draft_id, "approved")
        db.log_draft_event(
            conn,
            draft_id=draft_id,
            event="approved",
            actor=approver[:120],
            detail=" ".join((body.note or "").split())[:300] or None,
        )
        updated = db.get_draft(conn, draft_id)
        events = db.get_draft_events(conn, draft_id)

    payload = _draft_response(updated)
    payload["history"] = [
        {
            "event": e["event"],
            "actor": e["actor"],
            "detail": e["detail"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


@app.post("/drafts/{draft_id}/revoke")
def revoke_draft(draft_id: str, body: Revocation):
    """
    Withdraw a sign-off.

    The approval event stays in the trail — revoking records a second event
    rather than erasing the first. A history that can be rewritten is not a
    history.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    with db.get_connection() as conn:
        row = db.get_draft(conn, draft_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")
        if row["status"] != "approved":
            raise HTTPException(
                status_code=409, detail="This draft is not approved."
            )

        db.set_draft_status(conn, draft_id, "edited")
        db.log_draft_event(
            conn,
            draft_id=draft_id,
            event="revoked",
            detail=" ".join((body.reason or "").split())[:300] or None,
        )
        updated = db.get_draft(conn, draft_id)
        events = db.get_draft_events(conn, draft_id)

    payload = _draft_response(updated)
    payload["history"] = [
        {
            "event": e["event"],
            "actor": e["actor"],
            "detail": e["detail"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


# --------------------------------------------------------------------------
# Phase 4, part 3 — export
# --------------------------------------------------------------------------


@app.get("/drafts/{draft_id}/export")
def export_draft(draft_id: str):
    """
    Download a draft as a .docx.

    Unapproved drafts export too — a broker often wants to read one in Word,
    or send it to a colleague, before signing it off. They are stamped
    "DRAFT — NOT YET APPROVED FOR SENDING" inside the document, because the
    file travels on its own and the status badge in the UI does not go with
    it.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    with db.get_connection() as conn:
        row = db.get_draft(conn, draft_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")
        client = db.get_client(conn, str(row["client_id"]))
        events = db.get_draft_events(conn, draft_id)

    # The trail is the record of who signed off, not a column on the draft.
    approved_by = approved_at = None
    for event in events:
        if event["event"] == "approved":
            approved_by = event["actor"]
            approved_at = event["created_at"].isoformat()

    try:
        data = export_docx.build(
            title=row["title"],
            body=row["body"],
            status=row["status"],
            client_name=(client or {}).get("business_name"),
            carrier_name=row.get("carrier_name"),
            approved_by=approved_by,
            approved_at=approved_at,
            kind=row["kind"],
        )
    except Exception as exc:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Could not build the file: {exc}")

    filename = export_docx.filename_for(
        client_name=(client or {}).get("business_name"),
        carrier_name=row.get("carrier_name"),
        status=row["status"],
        kind=row["kind"],
    )

    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Without this the browser cannot read the filename off a fetch()
            # response, and the download lands as "export" with no extension.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


# --------------------------------------------------------------------------
# Phase 5 — the feedback loop
# --------------------------------------------------------------------------


class GapOverride(BaseModel):
    """A person correcting one line of a gap report."""

    requirement: str
    status: str
    note: str | None = None


class StyleEdit(BaseModel):
    guidance: str


@app.put("/gap-reports/{report_id}/item")
def override_gap_item(report_id: str, body: GapOverride):
    """
    Correct one requirement's status by hand.

    The stored report is updated AND the correction is recorded. The record
    is the point: a requirement that keeps being overridden is one the tool
    reads wrong, and that only becomes visible if the corrections are kept.
    """
    report_id = _validate_uuid(report_id, "report id")

    status = (body.status or "").strip().lower()
    if status not in {"held", "missing", "unclear"}:
        raise HTTPException(
            status_code=400, detail="Status must be held, missing or unclear."
        )

    requirement = " ".join((body.requirement or "").split())
    if not requirement:
        raise HTTPException(status_code=400, detail="Which requirement?")

    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, client_id, carrier_id, items FROM gap_reports WHERE id = %s",
                (report_id,),
            )
            report = cur.fetchone()
        if not report:
            raise HTTPException(status_code=404, detail="Gap report not found.")

        items = report["items"] or []
        previous = None
        found = False
        for item in items:
            if " ".join(str(item.get("requirement", "")).split()) == requirement:
                previous = item.get("status")
                item["status"] = status
                item["reason"] = (
                    " ".join((body.note or "").split())
                    or f"Set to {status} by hand."
                )
                item["overridden"] = True
                if status == "held":
                    item["ask"] = ""
                found = True
                break

        if not found:
            raise HTTPException(
                status_code=404, detail="That requirement is not in this report."
            )

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE gap_reports SET items = %s WHERE id = %s",
                (Jsonb_(items), report_id),
            )

        db.save_correction(
            conn,
            kind="gap_override",
            client_id=str(report["client_id"]),
            carrier_id=str(report["carrier_id"]),
            subject=requirement,
            before_text=previous,
            after_text=status,
            meta={"note": " ".join((body.note or "").split())[:300]},
        )

    return {"report_id": report_id, "requirement": requirement, "status": status}


@app.get("/insights")
def insights():
    """Everything on the Review page, counted from real rows."""
    with db.get_connection() as conn:
        return db.insight_counts(conn)


@app.get("/house-style/{kind}")
def read_house_style(kind: str):
    kind = (kind or "").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(status_code=400, detail="Unknown document kind.")
    with db.get_connection() as conn:
        row = db.get_house_style(conn, kind)
    if not row:
        return {"kind": kind, "guidance": "", "derived_from": 0,
                "edited_by_hand": False, "updated_at": None}
    return {
        "kind": kind,
        "guidance": row["guidance"],
        "derived_from": row["derived_from"],
        "edited_by_hand": row["edited_by_hand"],
        "updated_at": row["updated_at"].isoformat(),
    }


@app.post("/house-style/{kind}/learn")
def learn_house_style(kind: str):
    """
    Re-derive guidance from recent edits to this kind of document.

    Deliberately on request rather than continuously: a rule that changes
    under you between one draft and the next is impossible to reason about,
    and the broker should choose when the tool updates its habits.
    """
    kind = (kind or "").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(status_code=400, detail="Unknown document kind.")

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"Ollama is not reachable at {OLLAMA_HOST}.",
        )

    with db.get_connection() as conn:
        edits = [
            e for e in db.recent_corrections(conn, "draft_edit", limit=40)
            if e["subject"] == kind
        ]

    try:
        result = feedback.distil(edits)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with db.get_connection() as conn:
        db.save_house_style(
            conn,
            kind=kind,
            guidance=result["guidance"],
            derived_from=result["derived_from"],
            edited_by_hand=False,
        )

    return {
        "kind": kind,
        "guidance": result["guidance"],
        "observations": result["observations"],
        "derived_from": result["derived_from"],
        "note": result["note"],
    }


@app.put("/house-style/{kind}")
def edit_house_style(kind: str, body: StyleEdit):
    """
    Edit the guidance by hand.

    A broker who disagrees with what the tool concluded should be able to
    just fix it, rather than wait for enough new edits to outvote it.
    """
    kind = (kind or "").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(status_code=400, detail="Unknown document kind.")

    guidance = (body.guidance or "").strip()

    # The same guardrail as the distiller. A rule typed by hand is no safer
    # than one a model suggested, and this one would be applied to every
    # future document without anybody seeing it again.
    kept, dropped = [], []
    for line in guidance.splitlines():
        line = line.strip()
        if not line:
            continue
        (dropped if feedback._is_unsafe(line) else kept).append(line)

    with db.get_connection() as conn:
        row = db.save_house_style(
            conn, kind=kind, guidance="\n".join(kept[:6]),
            derived_from=0, edited_by_hand=True,
        )

    return {
        "kind": kind,
        "guidance": row["guidance"],
        "derived_from": row["derived_from"],
        "edited_by_hand": True,
        "updated_at": row["updated_at"].isoformat(),
        "dropped": dropped,
    }


@app.post("/clients/{client_id}/drafts/fill-form")
async def fill_proposal_form(
    client_id: str,
    file: UploadFile = File(...),
    carrier_id: str | None = Form(None),
):
    """
    Fill in a blank proposal form from the client's file.

    Takes the form itself as an upload, because a proposal form is the
    insurer's document, not ours — every carrier's is different, and reading
    theirs is the only way to answer the fields they actually ask for.

    Fields the file cannot answer come back blank and listed, never guessed.
    """
    client_id = _validate_uuid(client_id, "client id")

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"Ollama is not reachable at {OLLAMA_HOST}.",
        )

    data = await file.read()
    filename = file.filename or "form"
    try:
        parsed = parse_file(filename, data)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with db.get_connection() as conn:
        client = db.get_client(conn, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")
        token_map = db.get_client_tokens(conn, client_id)
        match = db.get_latest_match(conn, client_id)

        target = _validate_uuid(carrier_id, "carrier id") if carrier_id else None
        if not target and match:
            target = (_carriers_from_match(conn, match) or [None])[0]
        carrier = db.get_carrier(conn, target) if target else None

        attachments = []
        seen = set()
        for row in db.list_client_documents(conn, client_id):
            key = " ".join(row["requirement"].casefold().split())
            if key in seen:
                continue
            seen.add(key)
            attachments.append({
                "requirement": row["requirement"], "filename": row["filename"],
                "verdict": row["verdict"], "doc_type": row.get("doc_type"),
                "key_facts": row.get("key_facts") or [],
            })

        gap_items = []
        for report in db.get_latest_gap_reports(conn, client_id):
            if target and str(report["carrier_id"]) != str(target):
                continue
            gap_items.extend(report["items"] or [])

    try:
        result = drafting.fill_form(
            form_text=parsed.text,
            client=client,
            carrier_name=carrier["name"] if carrier else None,
            match=match,
            attachments=attachments,
            gap_items=gap_items,
            token_map=token_map,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Form filling failed")
        raise HTTPException(status_code=500, detail=f"Could not fill that form: {exc}")

    with db.get_connection() as conn:
        row = db.save_draft(
            conn, client_id=client_id, carrier_id=target, kind="form",
            title=result["title"], body=result["body"],
            body_redacted=result["body_redacted"], warnings=result["warnings"],
            stats=result["stats"], model=llm.OLLAMA_MODEL,
        )
        row["carrier_name"] = carrier["name"] if carrier else None
        db.log_draft_event(
            conn, draft_id=str(row["id"]), event="created", actor=llm.OLLAMA_MODEL,
            detail=(
                f"Filled {result['stats']['answered']} of "
                f"{result['stats']['fields']} fields from {filename}."
            ),
        )

    payload = _draft_response(row)
    payload["fields"] = result["fields"]
    return payload
