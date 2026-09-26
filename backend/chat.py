"""
Question answering over the carrier library.

Two layers keep this honest, and both matter:

  1. A similarity floor, checked BEFORE any model call. If the closest
     extract is not close enough, the answer is "not found" and no model
     runs. This costs nothing and catches questions the library simply has
     no material for.

  2. A strict instruction plus a `sufficient` flag the model sets itself.
     This catches the harder case: extracts that are topically near the
     question but do not actually answer it. Without it, a model handed six
     plausible-looking clauses will write a confident answer from general
     knowledge about insurance and cite them anyway.

The second layer is the one that matters for this product. A broker reading
an answer about a premium rate needs it to come from the document, not from
what the model knows about how premiums usually work.
"""
import logging

from llm import LLMError, generate_json, load_prompt
from vectorstore import VectorStoreError, search

logger = logging.getLogger(__name__)

TOP_K = 6

# Below this, don't bother the model. Deliberately low — a natural-language
# question and a clause of policy wording share little vocabulary even when
# they are about the same thing, so a strict floor rejects good matches.
MIN_SIMILARITY = 0.30


def _format_extracts(extracts: list[dict]) -> str:
    lines = []
    for position, hit in enumerate(extracts, start=1):
        where = hit.get("section_title") or "section"
        page = f", p.{hit['page_start']}" if hit.get("page_start") else ""
        lines.append(
            f"[{position}] {hit['carrier_name']} — {where}{page}\n{hit['text']}"
        )
    return "\n\n".join(lines)


def _resolve_sources(raw, extracts: list[dict]) -> list[dict]:
    if not isinstance(raw, list):
        raw = [raw]

    sources = []
    for value in raw:
        try:
            position = int(value)
        except (TypeError, ValueError):
            continue
        if not 1 <= position <= len(extracts):
            continue
        hit = extracts[position - 1]
        source = {
            "carrier_name": hit["carrier_name"],
            "carrier_id": hit["carrier_id"],
            "filename": hit["filename"],
            "section_title": hit.get("section_title"),
            "page_start": hit.get("page_start"),
            "similarity": hit["similarity"],
            "text": hit["text"][:400],
        }
        if source not in sources:
            sources.append(source)
    return sources


NOT_FOUND = (
    "Nothing in the carrier documents answers that. Either the point is not "
    "covered by the wordings that have been uploaded, or the carrier it "
    "concerns has not been added yet."
)


def ask(question: str, *, organization_id: str, carrier_id: str | None = None) -> dict:
    """
    Answer one question from the indexed carrier documents.

    `carrier_id` narrows to a single carrier; by default every carrier is
    searched at once, which is the useful case — "which of our carriers
    covers X" is a question a broker cannot answer from memory across a
    dozen wordings. `organization_id` is the tenant boundary and is always
    applied.
    """
    question = (question or "").strip()
    if not question:
        raise LLMError("Ask a question first.")

    try:
        hits = search(
            question=question,
            limit=TOP_K,
            carrier_id=carrier_id,
            organization_id=organization_id,
        )
    except VectorStoreError as exc:
        raise LLMError(str(exc)) from exc

    usable = [h for h in hits if h["similarity"] >= MIN_SIMILARITY]

    # Layer 1: nothing close enough. No model call.
    if not usable:
        return {
            "answer": NOT_FOUND,
            "sources": [],
            "sufficient": False,
            "considered": len(hits),
        }

    data = generate_json(
        load_prompt(
            "chat_answer",
            question=question,
            extracts=_format_extracts(usable),
        )
    )

    answer = str(data.get("answer") or "").strip()
    # Absent flag is treated as "yes" only when there is also an answer —
    # a model that omits the field has usually written a real answer, but an
    # empty answer is never sufficient regardless of what the flag says.
    sufficient = bool(data.get("sufficient", True)) and bool(answer)

    if not answer:
        answer = NOT_FOUND

    # Layer 2: the model itself judged the extracts inadequate. Citations are
    # dropped in that case — showing sources beside "I could not answer this"
    # invites reading them as support for an answer that was not given.
    sources = _resolve_sources(data.get("sources") or [], usable) if sufficient else []

    return {
        "answer": answer,
        "sources": sources,
        "sufficient": sufficient,
        "considered": len(usable),
    }
