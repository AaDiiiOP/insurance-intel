"""
Property Policy Intelligence — the first vertical slice built on top of the
Knowledge Core (backend/knowledge/).

Proves the Knowledge Core's shapes (entities, facts, evidence, relationships,
reconciliation) hold up against a real multi-document submission, the same
way Document Intelligence's coverage-limit extraction proved out
extraction_facts before anything else was built on it.

    submission (Property Policy + Coverage Schedule + Endorsement +
    Risk Survey, as separate documents)
      -> for each document: extract property.* / policy.* facts
      -> get-or-create a Business entity and an Asset entity (the property)
         and a Policy entity, linked Business-[owns]->Asset,
         Business-[has_policy]->Policy
      -> every fact attaches to the Asset entity (property.*) or the Policy
         entity (policy.*), with kc_evidence pointing at the real
         document/page/snippet
      -> a fact that disagrees with the current fact already on record for
         the same entity+fact_type goes through the SAME
         reconcile_fact()/contradiction-flagging path as everything else in
         the Knowledge Core — reused, not reimplemented.

This module deliberately does NOT touch extraction_facts, doc_intelligence.py,
or the coverage-limit slice — additive only, per the project's Phase 1 scope.
"""
import difflib
import logging
import re

from llm import LLMError, generate_json, load_prompt

import db
import knowledge.service as kc
from knowledge.fact_types import ALL_FACT_TYPES, NUMERIC_FACT_TYPES, target_entity_kind

logger = logging.getLogger(__name__)

# A document's real text layer is split into chunks of at most this many
# characters before extraction runs, rather than flat-truncating at 12,000
# chars and silently dropping every fact past that point (item 6's fix).
# Chosen to comfortably fit qwen2.5:7b's context alongside the prompt
# instructions, matching the previous single-shot limit's intent, just
# applied per-chunk with every chunk actually processed instead of applied
# once to the whole document.
MAX_CHUNK_CHARS = 12000

# Below this ratio, a claimed snippet is treated as NOT verified against the
# source text (item 5's fix) — chosen loose enough to tolerate whitespace/
# OCR noise but tight enough that an unrelated sentence does not pass.
SNIPPET_SIMILARITY_THRESHOLD = 0.85

# NUMERIC_FACT_TYPES and target_entity_kind() now live in
# knowledge/fact_types.py's FACT_DEFINITIONS registry (imported above) —
# this used to be its own copy here (and a third copy of the allowed-types
# set below), which is exactly the kind of duplication that silently drifts
# when a new fact type is added. See that module's docstring.


def _numeric(value: str) -> float | None:
    """Same stripping rule as doc_intelligence._numeric_value — pulls a
    comparable number out of a free-text figure like "₹350,000,000"."""
    if not value:
        return None
    digits = re.sub(r"[^\d.]", "", value)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def split_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """
    Split a document's text into extraction-sized chunks, preferring to
    break on a "[Page N]" marker boundary so no chunk ever cuts a page in
    half — the markers the OCR/native-parser text already carries are a
    natural, low-risk split point, simpler than reusing chunking.py's
    section-aware chunker (built for retrieval chunking, not for feeding an
    extraction prompt page-by-page) and just as effective for this purpose.

    A document shorter than max_chars returns a single chunk — the common
    case, and the previous (buggy) behaviour for anything under the old
    12,000-char truncation limit is unchanged.

    Never drops text: every character of the input appears in exactly one
    output chunk (item 6's fix — the bug this replaces was `text[:12000]`
    silently discarding everything after that point).
    """
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    # Split right before each "[Page N]" marker, keeping the marker with the
    # page that follows it, so a chunk boundary never lands mid-page.
    page_starts = [m.start() for m in re.finditer(r"(?=\[Page \d+\])", text)]
    if not page_starts or page_starts[0] != 0:
        page_starts = [0] + page_starts

    pages = []
    for i, start in enumerate(page_starts):
        end = page_starts[i + 1] if i + 1 < len(page_starts) else len(text)
        pages.append(text[start:end])

    # No page markers at all (or only one "page") and still over max_chars —
    # fall back to a flat character split so the "never drop text" guarantee
    # holds even for text with no [Page N] markers.
    if len(pages) <= 1:
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    chunks: list[str] = []
    current = ""
    for page in pages:
        if current and len(current) + len(page) > max_chars:
            chunks.append(current)
            current = page
        else:
            current += page
        # A single page longer than max_chars on its own still needs
        # splitting — flat character split within that one page, since
        # there is no smaller natural boundary to prefer.
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        chunks.append(current)

    return chunks


def verify_snippet(claimed_snippet: str, source_text: str,
                    threshold: float = SNIPPET_SIMILARITY_THRESHOLD) -> bool:
    """
    Whether a model's claimed evidence snippet actually appears in the real
    source document text (item 5's fix — previously only the claimed page
    marker was checked, never the snippet itself, so a hallucinated quote
    that happened to name a real page number was accepted unverified).

    Tries an exact substring match first (the common case for a real quote
    copied verbatim). Falls back to a fuzzy match — difflib.SequenceMatcher
    over a sliding window of source_text sized to the snippet, sized to
    tolerate whitespace/OCR noise differences without accepting an
    unrelated sentence — using `threshold` as the minimum similarity ratio
    to count as verified.

    Returns False for an empty/missing snippet or source — nothing here
    treats "nothing to compare" as verified.
    """
    if not claimed_snippet or not source_text:
        return False

    snippet = " ".join(claimed_snippet.split())
    normalized_source = " ".join(source_text.split())

    if snippet in normalized_source:
        return True

    window = len(snippet)
    if window == 0:
        return False

    # Locate an approximate anchor point first (find_longest_match), then
    # score a window of source text centered on it — this is what makes the
    # fuzzy check robust to whitespace/OCR noise that shifts the snippet's
    # true position by a few characters: a blind fixed-stride slide can
    # miss the best-aligned window entirely (checked empirically: a coarse
    # stride skipped straight past the real match here), whereas anchoring
    # on the longest common substring first, then checking a small spread of
    # nearby offsets, reliably finds it.
    matcher = difflib.SequenceMatcher(a=normalized_source, b=snippet, autojunk=False)
    match = matcher.find_longest_match(0, len(normalized_source), 0, len(snippet))
    if match.size == 0:
        return False

    # The snippet position `match.b` maps to source position `match.a` —
    # so the window that should align the WHOLE snippet with source text
    # starts roughly `match.b` characters before `match.a`.
    anchor = max(0, match.a - match.b)

    best_ratio = 0.0
    spread = max(window, 20)
    lo = max(0, anchor - spread)
    hi = min(len(normalized_source), anchor + window + spread)
    scorer = difflib.SequenceMatcher(a=snippet, autojunk=False)
    for start in range(lo, max(hi - window, lo) + 1):
        candidate = normalized_source[start:start + window]
        scorer.set_seq2(candidate)
        ratio = scorer.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
        if best_ratio >= threshold:
            return True

    return best_ratio >= threshold


def extract_property_policy_facts(*, text: str, ocr_confidence: float | None = None,
                                   page_confidences: dict | None = None,
                                   full_text: str | None = None) -> list[dict]:
    """
    Extract every property/policy fact ONE chunk of a document's text
    states, using the same LLM-calling convention as
    doc_intelligence.extract_coverage_limit (prompt file, generate_json,
    OCR-vs-model confidence typing).

    `text` should already be sized for one extraction call — callers
    processing a full document use split_into_chunks() and call this once
    per chunk (item 6's fix; this function itself no longer truncates).

    `full_text`, when given, is the WHOLE document's text (pre-chunking) —
    used only for snippet verification, not for extraction itself. A
    legitimate snippet can span a chunk boundary (e.g. a sentence split
    across two "[Page N]" sections), which a chunk-only check wrongly
    reports as "not found" even though it genuinely appears in the
    document. Verifying against `full_text` instead closes that gap. Falls
    back to `text` (the chunk) when `full_text` is not given, preserving
    the old per-chunk-only behaviour for any caller that does not pass it.

    `page_confidences`, when given, is a {"<page number>": confidence} map
    (ocr.quality_check_and_ocr's shape) — used to score each fact by the
    OCR confidence of its OWN evidence page, rather than the document-wide
    average (item 7's fix). `ocr_confidence` (the document average) is kept
    as the fallback when a fact's page cannot be determined, or when
    page_confidences is not given at all.

    Returns [] when nothing usable was found or the model call failed — no
    fact is invented, same "no silent guessing" rule as Phase 0. Each
    returned dict has: fact_type, value, normalized_value, unit, evidence
    ({page, section, snippet, source}), confidence ({value, type, method,
    note}), produced_by, status (normally 'needs_review', or explicitly
    'needs_review' with a note when the claimed snippet could not be
    verified against the real source text — item 5's fix).
    """
    verification_text = full_text if full_text is not None else text
    try:
        data = generate_json(
            load_prompt("extract_property_facts", document_text=text)
        )
    except LLMError as exc:
        logger.warning("Property/policy extraction failed: %s", exc)
        return []

    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        return []

    results = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        fact_type = str(item.get("fact_type") or "").strip()
        value = str(item.get("value") or "").strip()
        if fact_type not in ALL_FACT_TYPES or not value:
            continue

        self_confidence = item.get("self_confidence")
        try:
            self_confidence = max(0.0, min(1.0, float(self_confidence)))
        except (TypeError, ValueError):
            self_confidence = 0.5

        claimed_page = item.get("page")
        page = None
        if claimed_page:
            try:
                claimed_page = int(claimed_page)
                if f"[Page {claimed_page}]" in text:
                    page = claimed_page
            except (TypeError, ValueError):
                pass

        reasoning = str(item.get("reasoning") or "").strip()

        # Snippet anchoring (item 5): a claimed page marker being present is
        # not proof the SNIPPET itself is real — verify the snippet against
        # this chunk's actual text too, and fall back to needs_review with
        # an explanatory note rather than silently accepting an unverified
        # quote as if it were confirmed evidence.
        claimed_snippet = str(item.get("snippet") or "").strip()[:500] or None
        snippet_verified = (
            verify_snippet(claimed_snippet, verification_text) if claimed_snippet else False
        )
        status = "needs_review"
        status_note = None
        if claimed_snippet and not snippet_verified:
            status_note = "evidence snippet could not be verified against source text"

        # Page-specific OCR confidence (item 7): look up the fact's OWN
        # evidence page in page_confidences when possible; fall back to the
        # document-wide average when the page is unknown or has no entry.
        page_ocr_confidence = ocr_confidence
        if page_confidences and page is not None:
            page_value = page_confidences.get(str(page))
            if page_value is not None:
                page_ocr_confidence = page_value

        if ocr_confidence is not None:
            note_parts = [f"Page OCR confidence {page_ocr_confidence:.0f}%."]
            if reasoning:
                note_parts.append(f"Model reasoning: {reasoning}")
            if status_note:
                note_parts.append(status_note + ".")
            confidence = {
                "value": round(page_ocr_confidence / 100.0, 3),
                "type": "ocr_engine",
                "method": "tesseract",
                "note": " ".join(note_parts),
            }
        else:
            note_parts = [p for p in [reasoning, status_note] if p]
            confidence = {
                "value": self_confidence,
                "type": "model_self_reported",
                "method": "qwen2.5:7b_property_policy_extraction_v1",
                "note": ". ".join(note_parts) or None,
            }

        results.append({
            "fact_type": fact_type,
            "value": value[:300],
            "normalized_value": _numeric(value) if fact_type in NUMERIC_FACT_TYPES else None,
            "evidence": {
                "page": page,
                "section": (str(item.get("section")).strip()[:200] or None) if item.get("section") else None,
                "snippet": claimed_snippet,
                "snippet_verified": snippet_verified,
                "source": "ocr" if ocr_confidence is not None else "text_layer",
            },
            "confidence": confidence,
            "produced_by": "model:qwen2.5:7b_property_policy_extraction_v1",
            "status": status,
        })
    return results


def analyze_submission(conn, submission_id: str, organization_id: str) -> dict:
    """
    Run Property Policy Intelligence over every document in one submission.

    Creates (or reuses) a Business entity and an Asset entity ("the
    property") and a Policy entity, all named from the submission's label,
    linked Business-[owns]->Asset and Business-[has_policy]->Policy. Every
    fact extracted from every document in the submission is written through
    knowledge.service.reconcile_fact(), which links kc_evidence back to the
    real document/page/snippet and flags BOTH sides of any cross-document
    contradiction rather than picking one silently.

    Raises ValueError if the submission does not exist for this
    organization — caller (api/knowledge.py) turns that into a 404, same
    convention as every other org-scoped lookup in this codebase.
    """
    submission = db.get_submission(conn, submission_id, organization_id)
    if not submission:
        raise ValueError("Submission not found.")

    documents = db.list_submission_documents(conn, submission_id, organization_id)

    business = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="business",
        name=submission["label"],
    )
    asset = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="asset",
        name=f"{submission['label']} — insured property",
    )
    policy = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="policy",
        name=f"{submission['label']} — policy",
    )

    kc.create_relationship(
        conn, organization_id=organization_id, from_entity_id=str(business["id"]),
        to_entity_id=str(asset["id"]), relationship_type="owns",
    )
    kc.create_relationship(
        conn, organization_id=organization_id, from_entity_id=str(business["id"]),
        to_entity_id=str(policy["id"]), relationship_type="has_policy",
    )

    entity_by_kind = {"asset": asset, "policy": policy}

    facts_written = []
    contradictions = []
    documents_processed = []

    for document in documents:
        document_id = str(document["id"])
        text = document.get("extracted_text") or ""
        if not text.strip():
            documents_processed.append({
                "document_id": document_id, "filename": document["filename"],
                "facts_found": 0, "note": "No extracted text on this document.",
            })
            continue

        quality = db.get_document_quality(conn, document_id, organization_id)
        ocr_confidence = quality.get("avg_ocr_confidence") if quality else None
        page_confidences = quality.get("page_confidences") if quality else None

        # Chunk rather than flat-truncate (item 6's fix) — every chunk of
        # this document is run through extraction, so a fact stated past
        # the old 12,000-char cutoff is no longer silently dropped. Each
        # chunk still carries its own real "[Page N]" markers, so page
        # numbers and per-page OCR confidence (item 7) resolve correctly
        # per chunk exactly as they did for a whole, shorter document.
        chunks = split_into_chunks(text)
        extracted: list[dict] = []
        for chunk in chunks:
            extracted.extend(extract_property_policy_facts(
                text=chunk, ocr_confidence=ocr_confidence,
                page_confidences=page_confidences,
                # Full document text (item 2's fix), not just this chunk —
                # a snippet that legitimately spans a chunk boundary must
                # not be reported "not found" for having been checked
                # against only the smaller chunk it happened to be
                # extracted from.
                full_text=text,
            ))

        documents_processed.append({
            "document_id": document_id, "filename": document["filename"],
            "facts_found": len(extracted),
            "chunks_processed": len(chunks),
        })

        for item in extracted:
            entity = entity_by_kind[target_entity_kind(item["fact_type"])]
            fact = kc.reconcile_fact(
                conn,
                organization_id=organization_id,
                entity_id=str(entity["id"]),
                fact_type=item["fact_type"],
                value=item["value"],
                normalized_value=item["normalized_value"],
                confidence=item["confidence"],
                produced_by=item["produced_by"],
                document_id=document_id,
                evidence_page=item["evidence"]["page"],
                evidence_section=item["evidence"]["section"],
                evidence_snippet=item["evidence"]["snippet"],
                evidence_source=item["evidence"]["source"],
            )
            facts_written.append(fact)
            if fact.get("_contradiction"):
                contradictions.append({
                    "fact_id": str(fact["id"]),
                    "fact_type": fact["fact_type"],
                    "value": fact["value"],
                    "document_id": document_id,
                    "document_filename": document["filename"],
                    **fact["_contradiction"],
                })

    return {
        "submission_id": submission_id,
        "business": business,
        "asset": asset,
        "policy": policy,
        "documents_processed": documents_processed,
        "facts": facts_written,
        "contradictions": contradictions,
    }
