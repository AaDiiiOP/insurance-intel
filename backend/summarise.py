"""
Phase 1 summarisation: parsed carrier text in, structured briefing out.

Hierarchical map-reduce:

    chunk each document
        -> summarise each chunk independently        (map, N LLM calls)
        -> synthesise one briefing from those        (reduce, 1 LLM call)

Deliberately not summarising everything in one prompt: a policy wording plus
a rate card plus a brochure exceeds the context window, and truncating would
silently drop the exact requirements and exclusions this product exists to
capture.

The map step pulls five kinds of thing out of every chunk — summary,
requirements, plans, exclusions, facts — because the brief for this phase is
"everything important". The reduce step merges them into one briefing whose
fields later phases read directly: `plans` is what Phase 2 matches a client
against, and `requirements` is what Phase 3 diffs a client's submission
against. A requirement lost here is one that never gets checked later.
"""
import logging

from chunking import Chunk, ChunkedDocument, chunk_documents
from llm import LLMError, generate_json, load_prompt

logger = logging.getLogger(__name__)

# Guard against a pathological document producing hundreds of slow calls.
# Well past what a normal policy wording needs; if it trips, something is
# wrong with the document or the chunker.
MAX_CHUNKS = 60

# The categories the prompt is allowed to assign. Anything else becomes
# "other" rather than being trusted through to the UI and to Phase 2's
# category filter, where an invented category would silently match nothing.
VALID_CATEGORIES = {
    "fire",
    "marine",
    "liability",
    "burglary",
    "personal accident",
    "motor",
    "health",
    "life",
    "other",
}


def _as_list_of_strings(value) -> list[str]:
    """
    Coerce whatever the model returned into a clean list of strings.

    Models sometimes return a single string instead of an array, or an array
    of objects instead of plain strings. Rather than fail the whole run over
    formatting, normalise what came back.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("requirement", "exclusion", "text", "value", "description"):
                    if isinstance(item.get(key), str) and item[key].strip():
                        out.append(item[key].strip())
                        break
        return out
    return []


def _as_facts(value) -> list[dict]:
    """Normalise facts / key_points into [{label, value}]."""
    if not isinstance(value, list):
        return []

    points = []
    for item in value:
        if isinstance(item, dict):
            label = item.get("label") or item.get("name") or item.get("key")
            val = item.get("value") or item.get("detail")
            if isinstance(label, str) and val is not None and str(val).strip():
                points.append({"label": label.strip(), "value": str(val).strip()})
        elif isinstance(item, str) and ":" in item:
            # "Tenure: 1 year" — salvage rather than discard.
            label, _, val = item.partition(":")
            if label.strip() and val.strip():
                points.append({"label": label.strip(), "value": val.strip()})
    return points


def _as_plans(value) -> list[dict]:
    """
    Normalise plans into [{name, category, detail, exclusions}].

    A plan without a name is dropped: it cannot be shown in a list, matched
    against in Phase 2, or told apart from another nameless plan.
    """
    if not isinstance(value, list):
        return []

    plans = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("plan") or "").strip()
        if not name:
            continue

        category = (item.get("category") or "other").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "other"

        plans.append(
            {
                "name": name,
                "category": category,
                "detail": (item.get("detail") or item.get("description") or "").strip(),
                "exclusions": _as_list_of_strings(item.get("exclusions")),
            }
        )
    return plans


def _as_summary_parts(value, citable: list[dict]) -> list[dict]:
    """
    Turn the model's summary_parts into sentences with resolved provenance.

    The model cites sections by the number it was shown. That number is only
    meaningful against `citable`, so it is resolved to a real section title,
    page, and filename here — the frontend never sees a bare index it would
    have to interpret.

    Anything malformed is dropped rather than raised on: a summary that
    displays without hover provenance is a far better outcome than no summary
    at all after several minutes of work.
    """
    if not isinstance(value, list):
        return []

    parts: list[dict] = []
    for entry in value:
        if isinstance(entry, str):
            text, raw_sources = entry.strip(), []
        elif isinstance(entry, dict):
            text = str(entry.get("text") or "").strip()
            raw_sources = entry.get("sources") or []
            if not isinstance(raw_sources, list):
                raw_sources = [raw_sources]
        else:
            continue

        if not text:
            continue

        sources = []
        for raw in raw_sources:
            try:
                position = int(raw)
            except (TypeError, ValueError):
                continue
            # 1-based in the prompt, and only trust numbers actually shown.
            if not 1 <= position <= len(citable):
                continue
            result = citable[position - 1]
            source = {
                "section_title": result.get("section_title"),
                "page_start": result.get("page_start"),
                "filename": result.get("filename"),
                "chunk_index": result.get("index"),
            }
            if source not in sources:
                sources.append(source)

        parts.append({"text": text, "sources": sources})

    return parts


def summarise_chunk(chunk: Chunk) -> dict:
    """
    Map step: pull everything of interest out of one chunk.

    A chunk that fails is recorded and skipped by the caller rather than
    aborting the run — losing one section's detail is better than losing the
    whole briefing after several minutes of work.
    """
    prompt = load_prompt(
        "chunk_summary",
        section_title=chunk.section_title or "(untitled section)",
        chunk_text=chunk.text,
    )

    data = generate_json(prompt)
    return {
        "index": chunk.index,
        "section_title": chunk.section_title,
        "page_start": chunk.page_start,
        "document_id": chunk.document_id,
        "filename": chunk.filename,
        "summary": (data.get("summary") or "").strip(),
        "requirements": _as_list_of_strings(data.get("requirements")),
        "plans": _as_plans(data.get("plans")),
        "exclusions": _as_list_of_strings(data.get("exclusions")),
        "facts": _as_facts(data.get("facts")),
    }


def summarise_carrier(
    documents: list[tuple[str, str, str]], *, on_progress=None
) -> dict:
    """
    Run the full map-reduce over every document belonging to one carrier.

    `documents` is a list of (document_id, filename, text) triples. Each is
    chunked on its own — never concatenated first, because page numbers and
    section headings are only meaningful within their own document — and the
    resulting chunks are pooled before the map-reduce runs over the combined
    set.

    A carrier with a single uploaded file goes through this same path, so
    there is one code path for "one document" and "several", not two to keep
    in sync.

    on_progress, if given, is called as on_progress(done, total) after each
    chunk, so a long run can report progress rather than waiting in silence.
    """
    document = chunk_documents(documents)
    if document.count == 0:
        raise LLMError("There is no text to summarise across these documents.")
    return _summarise_chunked(document, on_progress=on_progress)


def _summarise_chunked(document: ChunkedDocument, *, on_progress=None) -> dict:
    """The actual map-reduce. Operates on pooled chunks alone."""
    chunks = document.chunks[:MAX_CHUNKS]
    if document.count > MAX_CHUNKS:
        logger.warning(
            "Documents produced %s chunks; summarising the first %s",
            document.count,
            MAX_CHUNKS,
        )

    # ---- Map ----
    chunk_results = []
    failed_sections = []
    for position, chunk in enumerate(chunks, start=1):
        try:
            chunk_results.append(summarise_chunk(chunk))
        except LLMError as exc:
            failed_sections.append(chunk.section_title or f"Section {chunk.index + 1}")
            logger.warning("Chunk %s failed to summarise: %s", chunk.index, exc)
        if on_progress:
            on_progress(position, len(chunks))

    if not chunk_results:
        raise LLMError(
            "Every section failed to summarise. Check that Ollama is running "
            "and the model is responding."
        )

    # ---- Reduce ----
    #
    # Numbered, because the reduce prompt asks the model to cite which section
    # each sentence came from, by number. This numbering is the only thing
    # those citations refer to, so it must stay 1-based and in the same order
    # as `citable`.
    citable = [r for r in chunk_results if r["summary"]]
    section_summaries = "\n".join(
        f"[{position}] {r['section_title'] or 'Section'}: {r['summary']}"
        for position, r in enumerate(citable, start=1)
    )

    def _bullets(key: str) -> str:
        lines = [f"- {item}" for r in chunk_results for item in r[key]]
        return "\n".join(lines) or "(none extracted)"

    all_plans = (
        "\n".join(
            f"- {p['name']} [{p['category']}]: {p['detail']}"
            + (f" | Excludes: {'; '.join(p['exclusions'])}" if p["exclusions"] else "")
            for r in chunk_results
            for p in r["plans"]
        )
        or "(none extracted)"
    )
    all_facts = (
        "\n".join(
            f"- {f['label']}: {f['value']}" for r in chunk_results for f in r["facts"]
        )
        or "(none extracted)"
    )

    final = generate_json(
        load_prompt(
            "final_summary",
            section_summaries=section_summaries or "(none)",
            all_requirements=_bullets("requirements"),
            all_plans=all_plans,
            all_exclusions=_bullets("exclusions"),
            all_facts=all_facts,
        )
    )

    summary_parts = _as_summary_parts(final.get("summary_parts"), citable)

    # Prefer text assembled from the parts, so the prose shown and the
    # provenance attached to it can never disagree.
    if summary_parts:
        summary = " ".join(p["text"] for p in summary_parts).strip()
    else:
        summary = _salvage_summary(final)

    # Last resort: stitch the chunk summaries together. By this point every
    # section has already been read successfully — the reduce call is the only
    # thing that failed to come back in a usable shape. Failing here would
    # throw away all of that work and show the user nothing, so a plainer
    # summary built from what we already have is strictly better than an
    # error. A smaller model returning an unexpected shape is common enough
    # that this path is expected to fire, not exceptional.
    if not summary and citable:
        summary = " ".join(r["summary"] for r in citable).strip()
        summary_parts = [
            {
                "text": r["summary"],
                "sources": [
                    {
                        "section_title": r["section_title"],
                        "page_start": r["page_start"],
                        "filename": r.get("filename"),
                        "chunk_index": r["index"],
                    }
                ],
            }
            for r in citable
        ]
        logger.warning(
            "Reduce step returned no usable summary; assembled one from %s "
            "section summaries instead.",
            len(citable),
        )

    if not summary:
        raise LLMError("The model returned an empty summary.")

    # ---- Requirements, with citations ----
    #
    # The reduce step's own requirements are a clean deduped list, but the
    # model has no reliable way to say which chunk each one came from once
    # they have been merged. Rather than guess, the requirements shown to the
    # user are sourced from the chunk-level results, which already carry
    # section title, page, and filename — every requirement is citable
    # because it was never separated from its source.
    #
    # This can show near-duplicate requirements if two chunks state the same
    # thing slightly differently. Traceability was chosen over a perfectly
    # clean list: a broker needs to check a requirement against the actual
    # policy page, and a citation they can verify matters more than tidiness.
    requirements = _dedupe_with_provenance(chunk_results, "requirements")

    # Plans are taken from the reduce step, which merges fragments about the
    # same plan found in different sections — the one place where merging
    # beats provenance, since a plan described across three sections should
    # appear once, not three times. Falls back to the chunk-level plans if
    # the model returned none.
    plans = _as_plans(final.get("plans"))
    if not plans:
        plans = _merge_chunk_plans(chunk_results)

    company_overview = final.get("company_overview")
    if not isinstance(company_overview, dict):
        company_overview = {}

    # Whether this reads as an insurance product document at all.
    #
    # Judged on what was EXTRACTED rather than by asking the model "is this a
    # policy?" — a wording with no plans, no requirements, no exclusions and
    # no figures has nothing a broker could act on, whatever it claims to be.
    # That covers both the wrong-file case (someone uploads an invoice) and
    # the useless-file case (a covering letter with no substance), without a
    # second model call to distinguish them.
    signal = (
        len(plans) + len(requirements)
        + len(_dedupe_with_provenance(chunk_results, "exclusions"))
        + len(_as_facts(final.get("key_points")))
    )
    looks_like_policy = signal >= 2

    # Did these documents look like insurance paperwork at all?
    #
    # The chunk prompt is told to say so plainly and return empty arrays when
    # a section is not part of an insurance document, so a CV or an invoice
    # produces a briefing with no plans, no requirements and no exclusions.
    # That is indistinguishable from a failed run unless it is named, and a
    # broker who uploaded the wrong file deserves to be told rather than left
    # with an empty-looking briefing.
    signals = [
        bool(plans),
        len(requirements) >= 2,
        len(_dedupe_with_provenance(chunk_results, "exclusions")) >= 2,
        bool(company_overview.get("product") or company_overview.get("insurer")),
        len(_as_facts(final.get("key_points"))) >= 2,
    ]

    return {
        "summary": summary,
        "summary_parts": summary_parts,
        # 0-1 of five signals means this almost certainly is not a policy
        # document. 2 is thin but plausible for a short brochure.
        "recognised": sum(signals) >= 2,
        "confidence": sum(signals),
        "looks_like_policy": looks_like_policy,
        "company_overview": {
            k: str(v).strip()
            for k, v in company_overview.items()
            if isinstance(v, (str, int, float)) and str(v).strip()
        },
        "key_points": _as_facts(final.get("key_points")),
        "requirements": requirements,
        "plans": plans,
        "exclusions": _dedupe_with_provenance(chunk_results, "exclusions"),
        # Kept so a later phase can embed these exact chunks and cite them,
        # rather than chunking the documents a second time and getting
        # different boundaries.
        "chunks": [
            {
                "index": c.index,
                "section_title": c.section_title,
                "page_start": c.page_start,
                "text": c.text,
                "document_id": c.document_id,
                "filename": c.filename,
                "summary": next(
                    (r["summary"] for r in chunk_results if r["index"] == c.index),
                    "",
                ),
            }
            for c in chunks
        ],
        "stats": {
            "chunks_total": document.count,
            "chunks_summarised": len(chunk_results),
            "chunks_failed": len(failed_sections),
            "failed_sections": failed_sections,
            "sectioned": document.sectioned,
        },
    }


def _salvage_summary(final: dict) -> str:
    """
    Pull prose out of whatever the reduce step actually returned.

    The prompt asks for `summary_parts` — a list of objects, each with text
    and a list of source numbers. That is a lot of structure to ask of a 7B
    model, and it frequently answers with something simpler but perfectly
    usable: a plain `summary` string, a differently-named key, or
    summary_parts as bare strings with no sources.

    Rather than treat any of that as a failure, take the prose and lose only
    the sentence-level provenance. The alternative — discarding a summary the
    model did write because it arrived under the wrong key — is worse for the
    user in every case.
    """
    # A plain string under any of the names models reach for.
    for key in ("summary", "overview", "text", "briefing", "description"):
        value = final.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # summary_parts present, but as bare strings or objects _as_summary_parts
    # rejected (e.g. every "sources" entry was unusable).
    parts = final.get("summary_parts")
    if isinstance(parts, list):
        sentences = []
        for entry in parts:
            if isinstance(entry, str) and entry.strip():
                sentences.append(entry.strip())
            elif isinstance(entry, dict):
                text = entry.get("text") or entry.get("summary") or entry.get("content")
                if isinstance(text, str) and text.strip():
                    sentences.append(text.strip())
        if sentences:
            return " ".join(sentences).strip()

    # A list of strings under one of the prose keys.
    for key in ("summary", "overview"):
        value = final.get(key)
        if isinstance(value, list):
            sentences = [v.strip() for v in value if isinstance(v, str) and v.strip()]
            if sentences:
                return " ".join(sentences).strip()

    return ""


def _dedupe_with_provenance(chunk_results: list[dict], key: str) -> list[dict]:
    """
    Flatten one list-of-strings field across chunks, keeping the first
    occurrence of each and the source it came from.
    """
    seen = set()
    out = []
    for result in chunk_results:
        for text in result[key]:
            fingerprint = text.strip().lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(
                {
                    "text": text,
                    "section_title": result["section_title"],
                    "page_start": result["page_start"],
                    "chunk_index": result["index"],
                    "document_id": result.get("document_id"),
                    "filename": result.get("filename"),
                }
            )
    return out


def _merge_chunk_plans(chunk_results: list[dict]) -> list[dict]:
    """
    Fallback when the reduce step returned no plans: merge the chunk-level
    ones by name, combining their exclusions.
    """
    merged: dict[str, dict] = {}
    for result in chunk_results:
        for plan in result["plans"]:
            fingerprint = plan["name"].strip().lower()
            if fingerprint in merged:
                existing = merged[fingerprint]
                for exclusion in plan["exclusions"]:
                    if exclusion not in existing["exclusions"]:
                        existing["exclusions"].append(exclusion)
                # Keep the fuller description of the two.
                if len(plan["detail"]) > len(existing["detail"]):
                    existing["detail"] = plan["detail"]
            else:
                merged[fingerprint] = dict(plan)
    return list(merged.values())
