"""Phase 2 — /chat (Ask across indexed carrier documents)."""
import os

import chat
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import llm
from llm import LLMError

from .carriers import _require_search_ready
from .deps import _validate_uuid

router = APIRouter()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class ChatIn(BaseModel):
    question: str
    carrier_id: str | None = None


@router.post("/chat")
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
