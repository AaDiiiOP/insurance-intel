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
import collections
import difflib
import logging
import re
from datetime import date

from llm import LLMError, generate_json, load_prompt

import db
import knowledge.service as kc
from knowledge.fact_types import (
    ALL_FACT_TYPES, FACT_DEFINITIONS, NUMERIC_FACT_TYPES, target_entity_kind,
)

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

# Multi-valued fact types (several independently-true values per entity —
# every coverage, every exclusion, every endorsement, every risk factor).
# Derived from the registry, never re-listed by hand, so a new multi-valued
# type added to FACT_DEFINITIONS is picked up here automatically.
MULTI_VALUED_FACT_TYPES = frozenset(
    ft for ft, definition in FACT_DEFINITIONS.items()
    if not definition.is_singleton and ft in ALL_FACT_TYPES
)

# Cheap, deterministic "does this text even have a labelled section for that
# fact type?" cues, used ONLY to decide whether a recovery pass is worth an
# extra model call (see extract_with_recovery). They never create a fact —
# a cue matching only means "ask the model again, with a prompt focused on
# just this type"; the model must still quote real evidence or abstain.
_RECOVERY_CUES = {
    "policy.coverage": re.compile(r"perils?\s+covered|covered\s+perils?|\bcovers?\s*:|\bcoverage\s*:", re.I),
    "property.risk_factors": re.compile(r"risk\s+factors?|\bhazards?\b", re.I),
    "policy.endorsement": re.compile(r"endorsement|\bchange\s+\d+\b|\bamend(?:ed|ment)?\b", re.I),
    "policy.exclusion": re.compile(r"\bexclusions?\b|\bexcluded\b|\bnot\s+covered\b", re.I),
}

# Every extraction call's outcome is counted here so a run can be diagnosed
# after the fact. Before this existed, a fact type the model returned under
# a slightly different name (or in an unexpected JSON shape) was dropped with
# no trace — "coverage = 0/40" could not be told apart from "the model never
# mentioned coverage at all". eval_property_policy.py records these counters
# in its result file.
EXTRACTION_DIAGNOSTICS: collections.Counter = collections.Counter()
DROPPED_FACT_TYPE_NAMES: collections.Counter = collections.Counter()


# Validity rule for property.risk_factors (v0.6.1). A risk FACTOR is a hazard.
# An overall survey rating ("Overall risk rating: Moderate") is a grade, and
# a statement that protections exist ("Fire protection: hydrants and portable
# extinguishers reported") is a mitigation. The prompt says so explicitly, but
# the 7B model still returned both in the 3-company quick check (4 silently
# wrong facts), so the rule is also enforced here, deterministically. It is
# deliberately narrow: it only rejects a rating, or a protection statement
# with no negative wording. "Fire protection: no hydrants on site" is a real
# hazard and is NOT rejected. This filter does not touch the scoring.
_RATING_RE = re.compile(r"\brisk\s+(?:rating|grade|score|level)\b", re.I)
_PROTECTION_RE = re.compile(r"^\s*(?:fire\s+)?(?:protection|safety\s+systems?)\b", re.I)
_NEGATIVE_RE = re.compile(
    r"\b(?:no|not|none|without|lack(?:s|ing)?|inadequate|insufficient|absent|"
    r"missing|deficien\w*|defective|faulty|out\s+of\s+service|non-?functional|overdue)\b", re.I)


def is_non_hazard_risk_factor(value: str) -> bool:
    """True if a candidate property.risk_factors value is a rating or a
    positive protection statement rather than a hazard."""
    if _RATING_RE.search(value):
        return True
    if _PROTECTION_RE.search(value) and not _NEGATIVE_RE.search(value):
        return True
    return False


# Numeric grounding rule (v0.7.0). The 3-company quick check of v0.6.1 showed a
# policy.coverage_limit of "n1,650,000,000" for AUR-SYN-003. That number is in
# NONE of the company's documents (the endorsement states n150,000,000 and
# n500,000,000), yet it carried a real, verified snippet: verify_snippet()
# proves the QUOTE is real, not that the extracted VALUE is what the quote
# says. So a numeric value must also be grounded in the text it came from:
# either that number appears in it, or it is the sum of 2-3 figures that do
# (a policy listing "Property Damage Sum Insured n500,000,000" and "Business
# Interruption Sum Insured n100,000,000" legitimately yields a combined
# n600,000,000 — the benchmark accepts that reading). Anything else is an
# unsupported number and is dropped, never written.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _figures(text: str) -> list[float]:
    out = []
    for tok in _NUMBER_RE.findall(text or ""):
        tok = tok.replace(",", "").rstrip(".")
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def numeric_value_grounded(value: str, source_text: str) -> bool:
    """True if the number in `value` appears in `source_text`, or equals the
    sum of 2-3 of the larger (>=10,000) figures in it."""
    target = _numeric(value)
    if target is None:
        return False
    figs = _figures(source_text)
    if any(abs(f - target) < 0.5 for f in figs):
        return True
    big = sorted({f for f in figs if f >= 10_000})[:40]
    import itertools
    for r in (2, 3):
        for combo in itertools.combinations(big, r):
            if abs(sum(combo) - target) < 0.5:
                return True
    return False


# Policy period (v0.8.1) — read DETERMINISTICALLY from the document text, never
# by the model, so it cannot be guessed. A period is returned only when the
# text states "Period: <date> to <date>" (or "Policy Period" / "Period of
# Insurance") with dates written with a month NAME or as ISO yyyy-mm-dd.
# Numeric dd/mm/yyyy is deliberately NOT read: 03/04/2026 is 3 April in India
# and 4 March in the US, and guessing wrong would silently shift a policy year.
# Two different periods in one document, an unreadable date, or an end before
# the start all give None (period unknown).
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_DATE_PAT = (r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4})")
_PERIOD_RE = re.compile(
    rf"(?:policy\s+period|period\s+of\s+insurance|\bperiod)\s*[:\-]\s*({_DATE_PAT})\s*"
    rf"(?:to|until|through|[-\u2013\u2014])\s*({_DATE_PAT})", re.I)

# Fact types whose value holds for the policy period. Attributes of the
# insured property itself (location, occupancy, construction, risk factors)
# are not tied to a policy year and stay undated.
PERIOD_BOUND_FACT_TYPES = frozenset({
    "policy.coverage", "policy.coverage_limit", "policy.deductible",
    "policy.exclusion", "policy.endorsement", "property.sum_insured",
})


def _parse_date(token: str) -> date | None:
    token = token.strip().rstrip(".,")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            return date.fromisoformat(token)
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", token)
        if not m:
            return None
        name = m.group(2).lower()
        month = _MONTHS.get(name) or next(
            (i for full, i in _MONTHS.items() if len(name) >= 3 and full.startswith(name)), None)
        return date(int(m.group(3)), month, int(m.group(1))) if month else None
    except (ValueError, TypeError):
        return None


# v0.8.3 — a coverage must be NAMED AS COVER somewhere. The v0.8.1 3-company
# check had the model read "Plant and machinery values reviewed. Revised
# machinery declared value: ..." (an endorsement's valuation note, present in
# all 20 benchmark endorsements) as a policy.coverage — 2 silently wrong facts.
# Rule: if the value appears in the source text but NO line it appears on (or
# the line before, for wrapped lists) mentions cover/peril/section/sum insured/
# limit of indemnity, it is a mention, not a coverage, and is dropped. A value
# not found verbatim is left to the normal evidence checks (no guessing).
_COVER_CUE_RE = re.compile(
    r"cover|peril|insuring|\bsection\b|sum\s+insured|limit\s+of\s+(?:indemnity|liability)", re.I)


def coverage_named_as_cover(value: str, source_text: str) -> bool:
    needle = " ".join((value or "").lower().split())
    if not needle:
        return False
    lines = [" ".join(l.lower().split()) for l in (source_text or "").splitlines()]
    hits = [i for i, l in enumerate(lines) if needle in l]
    if not hits:
        return True
    return any(_COVER_CUE_RE.search(lines[i]) or (i > 0 and _COVER_CUE_RE.search(lines[i - 1]))
               for i in hits)


# v0.10.1 — two rules from the v0.10.0 real-Qwen 4-company run.
#
# (a) Endorsement values: the model prefixed a change with the document's
#     header label ("Endorsement SYN-END-0002-01: Additional named insured
#     ...") — a string that exists nowhere in the document (the label is on its
#     own line), so it matched no ground truth and was 1 silently wrong fact.
#     The reference number identifies the endorsement; it is not what the
#     endorsement does. Leading "Endorsement <ref>:" / "Change N:" labels are
#     stripped from the value (never from the middle of it).
_ENDORSEMENT_LABEL_RE = re.compile(
    r"^\s*(?:endorsement\s+(?:no\.?|number|ref(?:erence)?\.?)?\s*[#:]?\s*[A-Za-z0-9/._-]*\d[A-Za-z0-9/._-]*"
    r"|change\s+\d+)\s*[:\-\u2013\u2014]\s*", re.I)


def clean_endorsement_value(value: str) -> str:
    out = value or ""
    for _ in range(3):
        new = _ENDORSEMENT_LABEL_RE.sub("", out, count=1)
        if new == out:
            break
        out = new
    return out.strip() or (value or "").strip()


# (b) Limits and sums insured must be stated AS a limit / sum insured. In all 4
#     companies the model read "Revised machinery declared value: 90,000,000"
#     (an endorsement's valuation note) or a schedule's per-category declared
#     value as policy.coverage_limit / property.sum_insured. These were flagged
#     as contradictions (not silent), but they made every coverage-limit answer
#     abstain. Rule: if the number appears in the source, at least one line it
#     appears on (or the previous non-empty line) must mention sum insured /
#     limit / indemnity / cover / insured value. A number not found verbatim
#     (e.g. a sum of two stated figures) is left to numeric grounding.
_LIMIT_CUE_RE = re.compile(r"sum\s+insured|limit|indemnity|cover|insured\s+value|total\s+insured", re.I)
LIMIT_CUE_FACT_TYPES = frozenset({"policy.coverage_limit", "property.sum_insured"})
_SUBLIMIT_RE = re.compile(r"sub[\s-]?limit", re.I)


def numeric_on_limit_line(value: str, source_text: str) -> bool:
    target = _numeric(value)
    if target is None:
        return True
    lines = [l for l in (source_text or "").splitlines() if l.strip()]
    hits = [i for i, l in enumerate(lines) if any(abs(f - target) < 0.5 for f in _figures(l))]
    if not hits:
        return True
    # v0.11.0: a sub-limit is not the coverage limit or the sum insured. Lines
    # that state a sub-limit never qualify as limit lines.
    hits = [i for i in hits if not _SUBLIMIT_RE.search(lines[i])]
    if not hits:
        return False
    return any(_LIMIT_CUE_RE.search(lines[i]) or (i > 0 and _LIMIT_CUE_RE.search(lines[i - 1])
                                                  and not _SUBLIMIT_RE.search(lines[i - 1]))
               for i in hits)


def extract_policy_period(text: str) -> tuple[date, date] | None:
    """(start, end) of the policy period stated in `text`, or None."""
    found = set()
    for a, b in _PERIOD_RE.findall(text or ""):
        start, end = _parse_date(a), _parse_date(b)
        if start is None or end is None or end < start:
            return None            # a stated-but-unreadable period is unknown, not guessed
        found.add((start, end))
    return found.pop() if len(found) == 1 else None


def reset_extraction_diagnostics() -> None:
    EXTRACTION_DIAGNOSTICS.clear()
    DROPPED_FACT_TYPE_NAMES.clear()


def snapshot_extraction_diagnostics() -> dict:
    return {"counters": dict(EXTRACTION_DIAGNOSTICS),
            "dropped_fact_type_names": dict(DROPPED_FACT_TYPE_NAMES)}

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
                                   full_text: str | None = None,
                                   prompt_name: str = "extract_property_facts",
                                   only_types: set | frozenset | None = None) -> list[dict]:
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
    fields = {"document_text": text}
    if only_types:
        fields["target_types"] = "\n".join(f"  {t}" for t in sorted(only_types))
    try:
        data = generate_json(load_prompt(prompt_name, **fields))
    except LLMError as exc:
        logger.warning("Property/policy extraction failed: %s", exc)
        EXTRACTION_DIAGNOSTICS["model_call_failed"] += 1
        return []

    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        EXTRACTION_DIAGNOSTICS["response_missing_facts_list"] += 1
        return []

    results = []
    for item in raw_facts:
        if not isinstance(item, dict):
            EXTRACTION_DIAGNOSTICS["dropped_non_object_item"] += 1
            continue
        EXTRACTION_DIAGNOSTICS["model_facts_returned"] += 1
        fact_type = str(item.get("fact_type") or "").strip()
        value = str(item.get("value") or "").strip()
        if fact_type not in ALL_FACT_TYPES:
            # Never "corrected" or guessed into a valid type — dropped, but
            # counted and named so the next run shows what the model said.
            EXTRACTION_DIAGNOSTICS["dropped_unknown_fact_type"] += 1
            DROPPED_FACT_TYPE_NAMES[fact_type or "<empty>"] += 1
            continue
        if not value:
            EXTRACTION_DIAGNOSTICS["dropped_empty_value"] += 1
            continue
        if only_types and fact_type not in only_types:
            EXTRACTION_DIAGNOSTICS["dropped_out_of_scope_type"] += 1
            continue
        if fact_type == "policy.endorsement":
            cleaned = clean_endorsement_value(value)
            if cleaned != value:
                EXTRACTION_DIAGNOSTICS["endorsement_label_stripped"] += 1
                value = cleaned
        if fact_type in LIMIT_CUE_FACT_TYPES and not numeric_on_limit_line(value, verification_text):
            EXTRACTION_DIAGNOSTICS["dropped_number_not_stated_as_limit"] += 1
            continue
        if fact_type in NUMERIC_FACT_TYPES and not numeric_value_grounded(value, verification_text):
            EXTRACTION_DIAGNOSTICS["dropped_numeric_not_in_source"] += 1
            continue
        if fact_type == "policy.endorsement" and not _RECOVERY_CUES["policy.endorsement"].search(verification_text):
            # v0.7.1: an endorsement is a change attached to a policy. A
            # document that never mentions an endorsement, change or amendment
            # cannot be where one is stated — the full 20-company run had the
            # model label two policy CONDITIONS ("Fire protection systems must
            # be maintained...") as endorsements (2 silently wrong facts).
            EXTRACTION_DIAGNOSTICS["dropped_endorsement_without_endorsement_text"] += 1
            continue
        if fact_type == "policy.coverage" and not coverage_named_as_cover(value, verification_text):
            EXTRACTION_DIAGNOSTICS["dropped_coverage_not_named_as_cover"] += 1
            continue
        if fact_type == "property.risk_factors" and is_non_hazard_risk_factor(value):
            EXTRACTION_DIAGNOSTICS["dropped_non_hazard_risk_factor"] += 1
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
            # v0.11.0: "5% of the loss" is not the number 5 — a percentage is
            # kept as text only (no normalized amount).
            "normalized_value": (_numeric(value) if fact_type in NUMERIC_FACT_TYPES and "%" not in value
                                 else None),
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


def _dedupe_key(fact: dict) -> tuple:
    return (fact["fact_type"], " ".join(fact["value"].split()).lower())


def extract_with_recovery(*, text: str, ocr_confidence: float | None = None,
                           page_confidences: dict | None = None,
                           full_text: str | None = None) -> list[dict]:
    """
    Stage A: the normal all-fact-types extraction (unchanged behaviour —
    singleton types are already extracted reliably this way).

    Stage B (recovery, only when needed): for each MULTI-VALUED fact type
    that Stage A returned nothing for on this chunk, but whose section is
    visibly labelled in the text (see _RECOVERY_CUES), ask the model again
    with a prompt focused on just those types. The first real-Qwen benchmark
    showed policy.coverage at 0/40 and property.risk_factors at 0/20 even
    though both are stated verbatim in the source PDFs: a 7B model given ten
    fact types at once tends to stop after the figures and names it finds
    first. A short, single-purpose second pass is the standard remedy.

    What this does NOT do, deliberately:
      - It never runs when Stage A already found that type (no extra cost
        once the main prompt does its job).
      - It never invents anything: recovered items go through the exact same
        validation as Stage A (registered type, real snippet check, page
        marker check, needs_review status).
      - It never loosens the abstention rule — the recovery prompt repeats
        it, an empty answer is a normal outcome, and (stricter than Stage A)
        a recovered item whose snippet does not verify against the source
        text is dropped outright.
      - Every recovered fact still reaches the Knowledge Core only through
        reconcile_fact() (the caller does that), so evidence linking and
        versioning are identical.
    Recovered items identical to something Stage A already returned are
    dropped, since multi-valued facts are inserted as new rows and would
    otherwise be duplicated.
    """
    kwargs = dict(ocr_confidence=ocr_confidence, page_confidences=page_confidences,
                  full_text=full_text)
    facts = extract_property_policy_facts(text=text, **kwargs)

    found_types = {f["fact_type"] for f in facts}
    wanted = {
        ft for ft in MULTI_VALUED_FACT_TYPES
        if ft not in found_types and ft in _RECOVERY_CUES and _RECOVERY_CUES[ft].search(text)
    }
    if not wanted:
        return facts

    for ft in wanted:
        EXTRACTION_DIAGNOSTICS[f"recovery_pass_for:{ft}"] += 1
    recovered = extract_property_policy_facts(
        text=text, prompt_name="extract_multivalued_facts", only_types=wanted, **kwargs,
    )
    seen = {_dedupe_key(f) for f in facts}
    for fact in recovered:
        # Stricter than Stage A on purpose: the recovery pass is the path
        # where a model is being asked to look harder for something, which is
        # exactly when fabrication is most tempting. A recovered item whose
        # quoted snippet cannot be found in the source text is abstained
        # from (dropped), not written as "needs_review".
        if not fact["evidence"].get("snippet_verified"):
            EXTRACTION_DIAGNOSTICS["recovery_unverified_dropped"] += 1
            continue
        key = _dedupe_key(fact)
        if key in seen:
            EXTRACTION_DIAGNOSTICS["recovery_duplicate_dropped"] += 1
            continue
        seen.add(key)
        EXTRACTION_DIAGNOSTICS[f"recovered_fact:{fact['fact_type']}"] += 1
        facts.append(fact)
    return facts


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
            extracted.extend(extract_with_recovery(
                text=chunk, ocr_confidence=ocr_confidence,
                page_confidences=page_confidences,
                # Full document text (item 2's fix), not just this chunk —
                # a snippet that legitimately spans a chunk boundary must
                # not be reported "not found" for having been checked
                # against only the smaller chunk it happened to be
                # extracted from.
                full_text=text,
            ))

        period = extract_policy_period(text)
        EXTRACTION_DIAGNOSTICS["policy_period_found" if period else "policy_period_not_found"] += 1

        documents_processed.append({
            "document_id": document_id, "filename": document["filename"],
            "facts_found": len(extracted),
            "chunks_processed": len(chunks),
            "policy_period": [period[0].isoformat(), period[1].isoformat()] if period else None,
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
                # The period stated IN THIS DOCUMENT applies to the
                # period-bound facts read from this document; anything else,
                # or a document with no readable period, stays undated.
                effective_from=period[0] if period and item["fact_type"] in PERIOD_BOUND_FACT_TYPES else None,
                effective_to=period[1] if period and item["fact_type"] in PERIOD_BOUND_FACT_TYPES else None,
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

    structure = _apply_policy_structure(conn, organization_id, submission, documents,
                                        business, asset, policy)

    return {
        "submission_id": submission_id,
        "business": business,
        "asset": asset,
        "policy": policy,
        "documents_processed": documents_processed,
        "facts": facts_written,
        "contradictions": contradictions,
        # Phase 1C: deterministic policy structure, kept OUT of "facts" so the
        # Phase 1A benchmark scores exactly what it scored before.
        "policy_structure": structure,
    }


def _apply_policy_structure(conn, organization_id, submission, documents, business, asset, policy) -> dict:
    """Phase 1C (v0.11.0): read policy structure deterministically from every
    document of the submission and write it into the Knowledge Core.

    - documents become `document` entities with their role, linked
      document -[supports]-> policy only when the document's policy number /
      reference matches the policy's (or neither states one). A document that
      names a DIFFERENT policy is never attached to this one.
    - policy.number / policy.insurer / conditions / definitions / sub-limits
      from policy + schedule documents (dated with the document's stated period).
    - each coverage named on the policy becomes a `coverage` entity
      (policy -[provides]-> coverage) carrying its own coverage.limit.
    - each endorsement gets a policy.endorsement_ref fact (its id) effective from
      its stated effective date — only if it references this policy.
    """
    import policy_structure as ps

    label = submission["label"]
    per_doc = []
    for document in documents:
        text = document.get("extracted_text") or ""
        st = ps.extract_policy_structure(text) if text.strip() else {"role": "unknown", "items": [],
                                                                       "endorsements": [], "coverages": []}
        per_doc.append((document, st, extract_policy_period(text) if text.strip() else None))

    def numbers(roles):
        return {i["value"] for d, st, _ in per_doc if st["role"] in roles
                for i in st["items"] if i["fact_type"] == "policy.number"}

    policy_numbers = numbers({"policy"}) or numbers({"schedule", "unknown"})
    policy_period = next((p for d, st, p in per_doc if st["role"] == "policy" and p), None)

    written, doc_out, coverage_entities = [], [], {}
    for document, st, period in per_doc:
        document_id = str(document["id"])
        own_numbers = {i["value"] for i in st["items"] if i["fact_type"] == "policy.number"}
        refs = {e["policy_ref"] for e in st["endorsements"] if e.get("policy_ref")}
        stated = own_numbers | refs
        matches = not stated or not policy_numbers or bool(stated & policy_numbers)
        doc_entity = kc.get_or_create_entity(
            conn, organization_id=organization_id, entity_type="document",
            name=f"{label} — {document['filename']}",
            attributes={"document_id": document_id, "role": st["role"]})
        target = {"risk_survey": asset, "loss_run": business}.get(st["role"], policy)
        linked = matches or st["role"] in ("risk_survey", "loss_run")
        if linked:
            kc.create_relationship(conn, organization_id=organization_id,
                                   from_entity_id=str(doc_entity["id"]), to_entity_id=str(target["id"]),
                                   relationship_type="supports")
        doc_out.append({"document_id": document_id, "filename": document["filename"], "role": st["role"],
                        "policy_numbers_stated": sorted(stated), "linked_to_policy": linked and target is policy,
                        "note": None if matches else "names a different policy number; not attached to this policy"})
        if not matches:
            EXTRACTION_DIAGNOSTICS["structure_document_other_policy"] += 1
            continue

        dated = period if st["role"] == "policy" else None
        for item in st["items"]:
            ft = item["fact_type"]
            if ft == "coverage.limit":
                cov = item["coverage"]
                if cov not in coverage_entities:
                    ce = kc.get_or_create_entity(conn, organization_id=organization_id, entity_type="coverage",
                                                 name=f"{label} — {cov}", attributes={"coverage": cov})
                    kc.create_relationship(conn, organization_id=organization_id,
                                           from_entity_id=str(policy["id"]), to_entity_id=str(ce["id"]),
                                           relationship_type="provides")
                    coverage_entities[cov] = ce
                entity_id = str(coverage_entities[cov]["id"])
            else:
                entity_id = str(policy["id"])
            fact = kc.reconcile_fact(
                conn, organization_id=organization_id, entity_id=entity_id, fact_type=ft,
                value=item["value"], normalized_value=item.get("normalized_value"),
                confidence={"value": 1.0, "type": "deterministic", "method": "labelled_line",
                            "note": "read from a labelled line of the document"},
                produced_by="policy_structure", document_id=document_id, evidence_page=item["page"],
                evidence_section=None, evidence_snippet=item["snippet"], evidence_source="text_layer",
                effective_from=dated[0] if dated else None, effective_to=dated[1] if dated else None)
            written.append({"fact_id": str(fact["id"]), "fact_type": ft, "value": fact["value"],
                            "status": fact["status"], "coverage": item.get("coverage")})
            EXTRACTION_DIAGNOSTICS[f"structure_fact:{ft}"] += 1

        for e in st["endorsements"]:
            start = e.get("effective_date")
            end = policy_period[1].isoformat() if policy_period and start and \
                policy_period[0].isoformat() <= start <= policy_period[1].isoformat() else None
            fact = kc.reconcile_fact(
                conn, organization_id=organization_id, entity_id=str(policy["id"]),
                fact_type="policy.endorsement_ref", value=e["endorsement_id"], normalized_value=None,
                confidence={"value": 1.0, "type": "deterministic", "method": "labelled_line",
                            "note": f"policy reference {e.get('policy_ref')}"},
                produced_by="policy_structure", document_id=document_id, evidence_page=e["page"],
                evidence_section=None, evidence_snippet=e["snippet"], evidence_source="text_layer",
                effective_from=start if end else None, effective_to=end)
            written.append({"fact_id": str(fact["id"]), "fact_type": "policy.endorsement_ref",
                            "value": fact["value"], "status": fact["status"], "effective_from": start})
            EXTRACTION_DIAGNOSTICS["structure_fact:policy.endorsement_ref"] += 1

    return {"documents": doc_out, "facts": written,
            "coverages": {k: str(v["id"]) for k, v in coverage_entities.items()},
            "policy_numbers": sorted(policy_numbers),
            "policy_period": [policy_period[0].isoformat(), policy_period[1].isoformat()] if policy_period else None}
