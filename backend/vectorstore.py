"""
Vector storage and retrieval.

Carrier chunks already live in Postgres with their section title and page
number. This module keeps a *searchable* copy in ChromaDB: each chunk's text
turned into a vector, so a client's situation can be matched against meaning
rather than keywords. A client who says "we store stock in a basement" should
surface the clause that says "basement or semi-basement storage carries a 20%
loading", which no keyword search would find.

Embeddings come from Ollama (nomic-embed-text), not from Chroma's own
default, so nothing leaves the machine.

Postgres stays the source of truth. Chroma is an index and is rebuilt from
Postgres whenever it is empty or stale — see `reindex_all`. That is what
makes it safe to wipe the Chroma volume without losing anything.

Only CARRIER text is indexed here. Client text is never embedded: it is
short, always read in full, and keeping it out of the index means one less
place a client's details can end up.
"""
import logging
import os

import chromadb
from chromadb.config import Settings

from llm import LLMError, embed

logger = logging.getLogger(__name__)

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))

# One collection for every chunk of every carrier document. Filtering by
# carrier_id and category at query time keeps things separate; a collection
# per carrier would multiply bookkeeping for no benefit at this scale, and
# would make "search across all carriers" — the core Phase 2 question —
# require fanning out over N collections instead of one query.
COLLECTION_NAME = "carrier_chunks"

_client = None
_collection = None


class VectorStoreError(Exception):
    """Raised when ChromaDB cannot be reached or a query fails."""


def _connect():
    """
    Open the Chroma connection lazily and reuse it.

    Deliberately not connected at import time: the backend must still start
    and serve /health when Chroma is down, so the failure shows up in the UI
    rather than crashing the container on boot.
    """
    global _client, _collection

    if _collection is not None:
        return _collection

    try:
        _client = chromadb.HttpClient(
            host=CHROMA_HOST,
            port=CHROMA_PORT,
            settings=Settings(anonymized_telemetry=False),
        )
        # embedding_function=None because vectors are supplied explicitly —
        # Chroma must never try to embed anything itself.
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )
    except Exception as exc:
        raise VectorStoreError(
            f"Could not reach ChromaDB at {CHROMA_HOST}:{CHROMA_PORT} — {exc}"
        ) from exc

    return _collection


def is_available() -> bool:
    """Whether ChromaDB is reachable. Used by /health."""
    try:
        _connect()
        return True
    except VectorStoreError:
        return False


def count() -> int:
    """How many chunks are indexed. 0 means a reindex is needed."""
    try:
        return _connect().count()
    except Exception:
        return 0


def index_document(
    *,
    document_id: str,
    carrier_id: str,
    carrier_name: str,
    filename: str,
    organization_id: str,
    chunks: list[dict],
) -> int:
    """
    Embed one document's chunks and store them for retrieval.

    Each entry carries what a citation needs — carrier, filename, section,
    page — so an answer can point at its source without a second lookup.
    `organization_id` is stored on every chunk too, so `search()` can enforce
    the tenant boundary that Chroma has no other way to know about.
    """
    if not chunks:
        return 0

    collection = _connect()

    # Rebuild rather than append, so re-running never leaves stale duplicates
    # from a previous version of the same document.
    remove_document(document_id)

    texts = [c["text"] for c in chunks]

    try:
        vectors = embed(texts)
    except LLMError as exc:
        raise VectorStoreError(f"Could not embed chunks: {exc}") from exc

    ids = [f"{document_id}:{c['index']}" for c in chunks]
    metadatas = [
        {
            "document_id": document_id,
            "carrier_id": carrier_id,
            "carrier_name": carrier_name,
            "filename": filename,
            "organization_id": organization_id,
            "chunk_index": c["index"],
            # Chroma metadata values must be primitives, so None becomes "".
            "section_title": c.get("section_title") or "",
            "page_start": c.get("page_start") or 0,
        }
        for c in chunks
    ]

    try:
        collection.add(
            ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas
        )
    except Exception as exc:
        raise VectorStoreError(f"Could not write to ChromaDB: {exc}") from exc

    logger.info("Indexed %s chunks for document %s", len(chunks), document_id)
    return len(chunks)


def remove_document(document_id: str) -> None:
    """Drop every entry for one document. Safe if nothing is stored."""
    try:
        _connect().delete(where={"document_id": document_id})
    except Exception as exc:
        logger.warning("Could not clear old chunks for %s: %s", document_id, exc)


def remove_carrier(carrier_id: str) -> None:
    """Drop every entry for one carrier, when it is deleted."""
    try:
        _connect().delete(where={"carrier_id": carrier_id})
    except Exception as exc:
        logger.warning("Could not clear chunks for carrier %s: %s", carrier_id, exc)


def search(
    *,
    question: str,
    organization_id: str,
    limit: int = 6,
    carrier_id: str | None = None,
) -> list[dict]:
    """
    Find the chunks most relevant to a question.

    Unscoped across carriers by default: the point of Phase 2 is to compare
    a client's situation against EVERY carrier at once, which is the thing a
    human broker with a few carrier relationships cannot do. Pass carrier_id
    to narrow to one.

    `organization_id` is always applied as a hard filter — this is the
    tenant boundary. Without it, one organization's search could return
    another organization's carrier document chunks, since Chroma has no
    concept of tenancy on its own.

    Returns dicts with the chunk text and enough metadata to cite it.
    """
    collection = _connect()

    try:
        vector = embed([question])[0]
    except LLMError as exc:
        raise VectorStoreError(f"Could not embed the question: {exc}") from exc

    if carrier_id:
        where = {"$and": [{"organization_id": organization_id}, {"carrier_id": carrier_id}]}
    else:
        where = {"organization_id": organization_id}

    try:
        result = collection.query(
            query_embeddings=[vector],
            n_results=limit,
            where=where,
        )
    except Exception as exc:
        raise VectorStoreError(f"ChromaDB query failed: {exc}") from exc

    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    hits = []
    for text, meta, distance in zip(documents, metadatas, distances):
        hits.append(
            {
                "text": text,
                "carrier_id": meta.get("carrier_id"),
                "carrier_name": meta.get("carrier_name", "Carrier"),
                "filename": meta.get("filename", "document"),
                "section_title": meta.get("section_title") or None,
                "page_start": meta.get("page_start") or None,
                "chunk_index": meta.get("chunk_index"),
                "document_id": meta.get("document_id"),
                # Cosine distance: 0 is identical, 2 is opposite. Converted to
                # a 0-1 similarity so callers read a higher number as better.
                "similarity": round(1 - (distance / 2), 3),
            }
        )
    return hits


def count_for_carrier(carrier_id: str, organization_id: str) -> int:
    """
    How many of one carrier's chunks are indexed.

    Lets a carrier's page say "briefed but not searchable" on every visit,
    not just in the moments after a briefing run — the failure is silent
    otherwise, and only shows up later as an unexplained empty match.
    """
    try:
        result = _connect().get(
            where={"$and": [{"organization_id": organization_id}, {"carrier_id": carrier_id}]},
            include=[],
        )
        return len(result.get("ids") or [])
    except Exception:
        return 0
