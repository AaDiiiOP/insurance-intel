"""
Document Intelligence — vertical slice.

One end-to-end path, deliberately narrow, built to answer one question before
any of it is generalised: do the evidence + confidence + review shapes in
db.py actually hold up against a real document?

    upload -> quality check -> text/OCR -> extract ONE fact
    -> evidence + typed confidence -> stored as needs_review
    -> human verifies or corrects -> append-only event

The one fact extracted here is the coverage limit / sum insured on a policy
document — concrete, present on almost every commercial policy schedule, and
exactly the kind of number a broker actually needs verified before it goes
into a submission.

This module deliberately does NOT:
  - extract more than one fact type (breadth is not the goal of a slice)
  - build a fact taxonomy or ontology (the plan explicitly defers this)
  - call any new AI provider (uses the existing local Ollama model exactly
    the way every other module in this codebase already does)

Confidence, concretely, per the typed-confidence decision:
  - a value produced by a deterministic rule (e.g. regex match against a
    known currency-amount pattern with a labelled "sum insured" nearby) gets
    confidence_type='deterministic', value 1.0 or 0.0 — it either matched or
    it did not, there is no in-between to report.
  - a value the model reads out of clean, native text gets
    confidence_type='model_self_reported' — the model's own stated
    plausibility, never a calibrated probability, and labelled as such.
  - a value read from an OCR'd page carries confidence_type='ocr_engine',
    using Tesseract's own per-word confidence average for that page — a real
    engine measurement, not a guess.
"""
import logging
import re

from llm import LLMError, generate_json, load_prompt

logger = logging.getLogger(__name__)

FACT_TYPE_COVERAGE_LIMIT = "coverage_limit"

# Below this self-reported confidence, a fact starts life flagged for review
# even before a human looks at it — the UI can surface these first. This is
# advisory sorting, not a gate: every fact needs_review regardless, per the
# "no silent guessing" rule; nothing here is ever auto-verified.
LOW_MODEL_CONFIDENCE = 0.6

# A currency-amount pattern used only to try a deterministic read before
# falling back to the model. Matches "Rs 10,00,00,000", "₹1,27,00,000",
# "INR 50,00,000", "USD 500,000" — common Indian and international forms.
# Deliberately conservative: a pattern that fires wrongly here would report
# false certainty (confidence 1.0) for a wrong value, which is worse than
# falling through to the model.
#
# The label alternation was widened after the Phase 0 evaluation
# (Aurevia Synthetic Insurance World v1) found real policy wording reading
# "Property Damage Sum Insured", which the original pattern's plain "sum
# insured" alternation only matched by the label appearing as a substring —
# fragile, since a differently-worded carrier ("Building Sum Insured",
# "Insured Amount") would have missed the deterministic path entirely and
# fallen through to the model on documents that are actually unambiguous.
#
# That same evaluation's test documents render the ₹ symbol as a bare "n"
# (a font-encoding artifact in the dataset's PDF generation, confirmed by
# inspecting the raw extracted text) — e.g. "n600,000,000". Adding "n" as an
# accepted currency prefix was tried and reverted: "n" is an ordinary letter,
# and it matched things like "on n 2024 filings" as a false positive with
# confidence 1.0, which is a worse failure than missing the real figure and
# falling through to the model. Documents with this rendering artifact
# correctly fall through to the model path instead, which reads them as
# plain text with no such ambiguity. See Results.md for the run that found
# both issues.
_CURRENCY_AMOUNT = re.compile(
    r"(?:(?:property|building|contents|stock)?\s*damage\s+sum\s+insured"
    r"|total\s+sum\s+insured"
    r"|sum\s+insured"
    r"|insured\s+amount"
    r"|coverage\s+limit"
    r"|limit\s+of\s+liability)"
    r"[^\d₹]{0,40}"
    r"((?:₹|Rs\.?|INR|USD|\$)\s?[\d,]{4,})",
    re.IGNORECASE,
)


def _try_deterministic(text: str) -> dict | None:
    """
    A labelled currency figure right next to the words that mean "this is
    the limit" is unambiguous enough to treat as a rule match rather than
    something the model needs to judge.

    Returns None on no match — falling through to the model is the normal
    case; this only fires on the clean, obvious case.
    """
    match = _CURRENCY_AMOUNT.search(text)
    if not match:
        return None

    value = match.group(1).strip()
    start = match.start()

    # Locate which [Page N] marker precedes the match, and the line it sits
    # on, for evidence — same provenance shape every other module in this
    # codebase already produces.
    page = None
    for page_match in re.finditer(r"\[Page (\d+)\]", text[:start]):
        page = int(page_match.group(1))

    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line_end = line_end if line_end != -1 else len(text)

    # OCR output is frequently one long line per page (no real newlines
    # between visually-separate lines of the source image), so the newline
    # search above can return most or all of the page as "the snippet".
    # Detected by comparing the line's span against the whole text rather
    # than a fixed character count — a genuinely short document could have
    # a legitimate 200-character "line" that IS the whole page, and a fixed
    # threshold would miss that case the way it did here.
    if (line_end - line_start) > 120 and (line_end - line_start) >= len(text) * 0.5:
        window_start = max(line_start, start - 40)
        window_end = min(line_end, start + len(value) + 40)
        snippet = text[window_start:window_end].strip()
    else:
        snippet = text[line_start:line_end].strip()

    return {
        "value": value,
        "page": page,
        "section": None,
        "snippet": snippet[:300],
        "confidence": {
            "value": 1.0,
            "type": "deterministic",
            "method": "labelled_currency_amount_regex_v1",
            "note": "A labelled sum-insured figure matched a known pattern.",
        },
    }


def extract_coverage_limit(
    *, text: str, ocr_confidence: float | None = None
) -> dict:
    """
    Extract the coverage limit / sum insured from one document's text.

    `ocr_confidence` is the document's average OCR confidence, if this text
    came from OCR rather than a native text layer — passed through so a
    model-read value from a poorly-OCR'd page can be labelled accordingly
    rather than presented as equally trustworthy as one from clean text.

    Returns None if nothing usable was found — the caller stores no fact in
    that case rather than a fact with an empty value, per the "no silent
    guessing" rule: absence is reported by not creating a row, not by
    creating one that says nothing.
    """
    deterministic = _try_deterministic(text)
    if deterministic:
        return {
            "fact_type": FACT_TYPE_COVERAGE_LIMIT,
            "label": "Coverage limit (sum insured)",
            "value": deterministic["value"],
            "evidence": {
                "page": deterministic["page"],
                "section": deterministic["section"],
                "snippet": deterministic["snippet"],
                "source": "ocr" if ocr_confidence is not None else "text_layer",
            },
            "confidence": deterministic["confidence"],
            "produced_by": "rule:labelled_currency_amount_v1",
        }

    try:
        data = generate_json(
            load_prompt("extract_coverage_limit", document_text=text[:12000])
        )
    except LLMError as exc:
        logger.warning("Coverage-limit extraction failed: %s", exc)
        return None

    if not data.get("found") or not str(data.get("value") or "").strip():
        return None

    self_confidence = data.get("self_confidence")
    try:
        self_confidence = max(0.0, min(1.0, float(self_confidence)))
    except (TypeError, ValueError):
        self_confidence = 0.5  # model gave no usable number — treat as unsure

    # A page the model claims is only trustworthy if it can be cross-checked
    # against a real [Page N] marker in the text it was actually given.
    claimed_page = data.get("page")
    page = None
    if claimed_page:
        try:
            claimed_page = int(claimed_page)
            if f"[Page {claimed_page}]" in text:
                page = claimed_page
        except (TypeError, ValueError):
            pass

    reasoning = str(data.get("reasoning") or "").strip()

    if ocr_confidence is not None:
        # The value came from an OCR'd page. What matters to a reviewer is
        # the OCR engine's own measurement of that page, not the model's
        # opinion of text it was simply handed — the model has no way to
        # know the scan was blurry, only whatever the OCR happened to output.
        confidence = {
            "value": round(ocr_confidence / 100.0, 3),
            "type": "ocr_engine",
            "method": "tesseract",
            "note": (
                f"Page OCR confidence {ocr_confidence:.0f}%. Model reasoning: "
                f"{reasoning}" if reasoning else f"Page OCR confidence {ocr_confidence:.0f}%."
            ),
        }
    else:
        confidence = {
            "value": self_confidence,
            "type": "model_self_reported",
            "method": "qwen2.5:7b_extraction_v1",
            "note": reasoning or None,
        }

    return {
        "fact_type": FACT_TYPE_COVERAGE_LIMIT,
        "label": "Coverage limit (sum insured)",
        "value": str(data["value"]).strip()[:200],
        "evidence": {
            "page": page,
            "section": (str(data.get("section")).strip()[:200] or None)
            if data.get("section") else None,
            "snippet": str(data.get("snippet") or "").strip()[:500] or None,
            "source": "ocr" if ocr_confidence is not None else "text_layer",
        },
        "confidence": confidence,
        "produced_by": "model:qwen2.5:7b_extraction_v1",
    }


def needs_priority_review(confidence: dict) -> bool:
    """
    Whether a fact should surface near the top of a reviewer's queue.

    Advisory only — every fact starts 'needs_review' regardless of this. A
    deterministic match is never flagged (it either matched cleanly or does
    not exist as a fact at all); a low model or OCR confidence is.
    """
    if confidence["type"] == "deterministic":
        return False
    return confidence["value"] < LOW_MODEL_CONFIDENCE


# --------------------------------------------------------------------------
# Cross-document contradiction detection
#
# Everything above this line extracts a fact from ONE document. This part
# runs after every document in a submission has been extracted, and answers
# a different question: do two documents that were both supposed to state
# the same real-world figure actually agree?
#
# The gap this closes: before it existed, a policy stating one sum insured
# and its own coverage schedule stating a different one were never compared
# at all — each was scored as an independent, individually-correct fact, and
# nothing in the system would ever have noticed the two disagreed. That is
# exactly the "silent guessing" failure mode the whole slice exists to rule
# out, just at the cross-document level rather than within one document.
# --------------------------------------------------------------------------

# Which fact types are considered "the same real-world figure" for the
# purpose of comparison, even though they came from asking about different
# document kinds. Coverage limit is the one fact type this slice extracts,
# so there is only one group today — this is a dict rather than a flat
# constant so a second fact type can be added as its own group later without
# the comparison logic needing to change, only this table.
COMPARABLE_FACT_TYPES = {
    FACT_TYPE_COVERAGE_LIMIT: {FACT_TYPE_COVERAGE_LIMIT},
}

# Two figures are treated as agreeing if they are within this fraction of
# each other, not just byte-identical. Real documents round differently —
# "₹35,00,00,000" vs "Rs 3,50,00,000" written with one extra digit dropped is
# a formatting difference, not a contradiction, but a genuinely different
# figure (350000000 vs 370000000, roughly 5.7% apart) should not slip through
# under too generous a tolerance. 1% is deliberately tight: it exists to
# absorb rounding, not real disagreement.
CONTRADICTION_TOLERANCE = 0.01


def _numeric_value(value: str) -> float | None:
    """
    Pull a comparable number out of an extracted value string.

    Extracted values are free text ("₹350,000,000", "INR 370,000,000") kept
    exactly as written, on purpose — comparison needs a number, so this
    strips everything else. Returns None when no digits are present at all,
    which the caller treats as "cannot compare" rather than "equal" or
    "different": a fact with no recoverable number should never silently
    count as agreeing with another one.
    """
    if not value:
        return None
    digits = re.sub(r"[^\d.]", "", value)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def find_contradiction(new_fact_type: str, new_value: str,
                       existing_facts: list[dict]) -> dict | None:
    """
    Check one newly-extracted fact against every fact already on record for
    the same submission, and return the first one it disagrees with.

    `existing_facts` is every fact already stored for the submission (see
    db.list_submission_facts) — checked regardless of which document they
    came from, since the whole point is comparing across documents. A fact
    from the SAME document as the new one is skipped: two mentions of a
    figure within one document restating each other is not the scenario
    this exists to catch, and would just be noise.

    Returns None when nothing comparable disagrees — either no other fact
    of a comparable type exists yet, or every comparable one agrees within
    tolerance.
    """
    comparable_types = COMPARABLE_FACT_TYPES.get(new_fact_type, {new_fact_type})
    new_number = _numeric_value(new_value)
    if new_number is None:
        return None

    for existing in existing_facts:
        if existing["fact_type"] not in comparable_types:
            continue

        existing_number = _numeric_value(existing["value"])
        if existing_number is None:
            continue

        if existing_number == 0:
            continue  # avoid a division by zero on a degenerate value

        relative_difference = abs(new_number - existing_number) / existing_number
        if relative_difference > CONTRADICTION_TOLERANCE:
            return {
                "fact_id": existing["id"],
                "document_filename": existing.get("document_filename"),
                "value": existing["value"],
                "relative_difference": round(relative_difference, 4),
            }

    return None
