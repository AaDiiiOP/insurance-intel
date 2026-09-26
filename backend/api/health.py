"""
/health and /supported-types.

/health probes every dependency rather than just reporting the API is
alive, because the most common failure in this stack is Ollama not
running on the host — which is invisible until a summarise call fails
minutes later.
"""
import logging
import os
import time

import httpx
import psycopg
from fastapi import APIRouter

import db
import llm
import vectorstore
from parsers import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

router = APIRouter()

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = os.getenv("CHROMA_PORT", "8000")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


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


@router.get("/health")
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


@router.get("/supported-types")
def supported_types():
    return {"extensions": sorted(SUPPORTED_EXTENSIONS)}
