"""
Matching a client to carrier plans.

Retrieval, then judgement:

    client profile (redacted)
      -> several targeted searches across every carrier's chunks
      -> the retrieved extracts, numbered
      -> one model call that ranks fit and cites which extract it used

Searching more than once, with different queries, is deliberate. A single
query built from the whole profile returns whatever is nearest to that blend
and tends to miss the specific clause that matters — the basement loading,
the turnover band. Querying per requested category, plus once for the risk
notes, surfaces those separately and lets the model see them side by side.

Nothing here ever sees the client's real name, PAN, or contact details: the
profile arrives already redacted, and the citations come back keyed to
extract numbers rather than to anything about the client.
"""
import logging

from llm import LLMError, generate_json, load_prompt
from vectorstore import VectorStoreError, search

logger = logging.getLogger(__name__)

VALID_FIT = {"strong", "possible", "poor"}

VALID_CATEGORIES = {
    "fire", "marine", "liability", "burglary",
    "personal accident", "motor", "health", "life", "other",
}

# Chunks retrieved per query. Six across four or five queries lands around
# 20-30 extracts before dedupe, which is enough context for the model without
# burying the relevant clause.
PER_QUERY = 6

# Below this cosine similarity an extract is noise rather than weak evidence.
# Tuned to be forgiving: a policy wording and a broker's file note share
# little vocabulary, so genuinely relevant matches often score modestly.
MIN_SIMILARITY = 0.30

# Hard ceiling on extracts sent to the model, best-scoring first.
MAX_EXTRACTS = 24


def _queries_for(profile: str, categories: list[str], risk_notes: str) -> list[str]:
    """
    Build the searches to run.

    One per requested category, since those are the questions the broker
    actually asked. One for the risk notes, which is where the unusual
    details live. One for the profile as a whole, as a catch-all for
    eligibility clauses that mention turnover or entity type.
    """
    queries = []

    for category in categories:
        queries.append(
            f"{category} insurance cover, sum insured, premium rate, "
            f"eligibility and exclusions"
        )

    if risk_notes and risk_notes.strip():
        queries.append(risk_notes.strip()[:600])

    if profile.strip():
        queries.append(
            "eligibility criteria, required documents, turnover band, "
            "entity type accepted"
        )
        queries.append(profile.strip()[:600])

    return queries or ["commercial insurance cover for a small business"]


def gather_extracts(
    profile: str, categories: list[str], risk_notes: str
) -> list[dict]:
    """
    Run every search and pool the results, best first, without duplicates.

    A chunk retrieved by two different queries keeps its higher score rather
    than appearing twice — the model reading the same clause under two
    numbers would cite them inconsistently.
    """
    best: dict[str, dict] = {}

    for query in _queries_for(profile, categories, risk_notes):
        try:
            hits = search(question=query, limit=PER_QUERY)
        except VectorStoreError as exc:
            # One failed query should not lose the others.
            logger.warning("Retrieval failed for a query: %s", exc)
            continue

        for hit in hits:
            if hit["similarity"] < MIN_SIMILARITY:
                continue
            key = f"{hit['document_id']}:{hit['chunk_index']}"
            if key not in best or hit["similarity"] > best[key]["similarity"]:
                best[key] = hit

    pooled = sorted(best.values(), key=lambda h: h["similarity"], reverse=True)
    return pooled[:MAX_EXTRACTS]


def _format_extracts(extracts: list[dict]) -> str:
    """Number the extracts so the model can cite them."""
    lines = []
    for position, hit in enumerate(extracts, start=1):
        where = hit.get("section_title") or "section"
        page = f", p.{hit['page_start']}" if hit.get("page_start") else ""
        lines.append(
            f"[{position}] {hit['carrier_name']} — {where}{page}\n{hit['text']}"
        )
    return "\n\n".join(lines)


def _resolve_sources(raw, extracts: list[dict]) -> list[dict]:
    """Turn cited extract numbers into real, checkable citations."""
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
        }
        if source not in sources:
            sources.append(source)
    return sources


def _as_strings(value) -> list[str]:
    """Coerce a list-of-strings field, tolerating the shapes models return."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []

    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            for key in ("text", "note", "watch_out", "gap", "question", "value"):
                if isinstance(item.get(key), str) and item[key].strip():
                    out.append(item[key].strip())
                    break
    return out


def _normalise_matches(raw, extracts: list[dict]) -> list[dict]:
    """
    Clean the model's matches into a shape the UI can render.

    A match naming no carrier or no plan is dropped: it cannot be shown in a
    list, cannot be verified against a document, and is more likely to be the
    model padding than a real recommendation.
    """
    if not isinstance(raw, list):
        return []

    matches = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        carrier = str(item.get("carrier") or "").strip()
        plan = str(item.get("plan") or item.get("name") or "").strip()
        if not carrier and not plan:
            continue

        category = str(item.get("category") or "other").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "other"

        fit = str(item.get("fit") or "possible").strip().lower()
        if fit not in VALID_FIT:
            fit = "possible"

        matches.append(
            {
                "carrier": carrier or "(carrier not named)",
                "plan": plan or "(plan not named)",
                "category": category,
                "fit": fit,
                "why": str(item.get("why") or item.get("reason") or "").strip(),
                "watch_outs": _as_strings(item.get("watch_outs")),
                "sources": _resolve_sources(item.get("sources") or [], extracts),
            }
        )

    # Best fit first, preserving the model's order within each band.
    rank = {"strong": 0, "possible": 1, "poor": 2}
    matches.sort(key=lambda m: rank.get(m["fit"], 1))
    return matches


def match_client(*, profile: str, categories: list[str], risk_notes: str) -> dict:
    """
    Retrieve, then rank. Raises LLMError with a readable reason on failure.

    `profile` must already be redacted — this function does not redact, so
    that the one place redaction happens is the one place to audit.
    """
    extracts = gather_extracts(profile, categories, risk_notes)

    if not extracts:
        raise LLMError(
            "Nothing relevant was found in the carrier documents. Either no "
            "carrier has been briefed yet, or the search index is empty — "
            "rebuild it from Settings."
        )

    data = generate_json(
        load_prompt(
            "match_plans",
            client_profile=profile,
            categories=", ".join(categories) if categories else "not specified",
            extracts=_format_extracts(extracts),
        )
    )

    matches = _normalise_matches(data.get("matches"), extracts)

    if not matches:
        raise LLMError(
            "The model returned no usable recommendations. The extracts may "
            "not cover what this client asked for."
        )

    # Everything retrieved, so the UI can show what the recommendation was
    # based on — including extracts the model chose not to cite.
    sources = [
        {
            "carrier_name": hit["carrier_name"],
            "carrier_id": hit["carrier_id"],
            "filename": hit["filename"],
            "section_title": hit.get("section_title"),
            "page_start": hit.get("page_start"),
            "similarity": hit["similarity"],
            "cited": any(
                s["filename"] == hit["filename"]
                and s.get("section_title") == hit.get("section_title")
                for m in matches
                for s in m["sources"]
            ),
        }
        for hit in extracts
    ]

    return {
        "matches": matches,
        "gaps": _as_strings(data.get("gaps")),
        "questions": _as_strings(data.get("questions")),
        "sources": sources,
        "stats": {
            "extracts_retrieved": len(extracts),
            "carriers_seen": len({h["carrier_id"] for h in extracts}),
            "top_similarity": extracts[0]["similarity"] if extracts else 0,
        },
    }
