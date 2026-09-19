"""
Reading a client's own document into intake fields.

A broker receives a submission, a proposal form, or a covering letter and
should not have to retype it. This turns one into a filled-in form they can
correct — a draft, never a saved record. Nothing here writes to the database.

Extraction is split in two, deliberately:

  * Identifiers — PAN, GSTIN, phone, email, IFSC — are pulled out by pattern,
    with no model involved. Two reasons. They have fixed formats, so a regex
    is exact where a 7B model reliably mangles alphanumeric strings, dropping
    a character or "correcting" a digit. And it means these never need to be
    in the text the model reads.

  * Everything else — what the business does, its turnover, what the premises
    are like, what worries them — has no fixed shape and genuinely needs a
    model. It reads the document with the identifiers already replaced by
    placeholders.

The honest limit: a business name and a contact name are not pattern-matchable,
so the model does read those. Redaction cannot come before extraction — some
component has to see a value to lift it out. What this split buys is that the
structured identifiers never reach the model, and that from the moment the
client is saved, everything downstream works from the redacted profile.
"""
import logging
import re

from llm import LLMError, generate_json, load_prompt
from tokenise import PATTERNS, Redactor

logger = logging.getLogger(__name__)

# Long submissions get truncated: the fields worth extracting are stated near
# the front in every real submission shape, and a whole document would push a
# 7B model past the point where it answers carefully.
MAX_CHARS = 14000

VALID_CATEGORIES = {
    "fire", "marine", "liability", "burglary",
    "personal accident", "motor", "health", "life",
}

# Field -> the pattern kind that fills it. Only formats a regex gets right.
IDENTIFIER_FIELDS = {
    "pan": "PAN",
    "gstin": "GSTIN",
    "contact_phone": "PHONE",
    "contact_email": "EMAIL",
}


def extract_identifiers(text: str) -> dict[str, str]:
    """
    Pull the fixed-format identifiers straight out of the text.

    First match wins per field. A submission mentions its own PAN before any
    other, and a director's personal PAN — if present at all — comes later,
    so first-match is the right default. The broker corrects it if not.
    """
    found: dict[str, str] = {}
    by_kind = dict(PATTERNS)

    for field, kind in IDENTIFIER_FIELDS.items():
        pattern = by_kind.get(kind)
        if not pattern:
            continue
        match = pattern.search(text)
        if match:
            found[field] = match.group(0).strip()

    # A GSTIN contains a PAN. If the PAN pattern landed inside the GSTIN
    # rather than on a standalone PAN, look for one outside it.
    gstin = found.get("gstin")
    pan = found.get("pan")
    if gstin and pan and pan in gstin:
        outside = by_kind["PAN"].findall(text.replace(gstin, " "))
        found["pan"] = outside[0] if outside else pan

    return found


def _clean(value) -> str:
    """One-line string, or empty. Models return None and numbers here."""
    if value is None:
        return ""
    text = str(value).strip()
    # A model that found nothing sometimes says so in the field itself.
    if text.lower() in {"n/a", "na", "none", "not stated", "unknown", "null", "-"}:
        return ""
    return re.sub(r"\s+", " ", text)[:300]


def _clean_categories(value) -> list[str]:
    """Keep only the recognised covers, deduplicated, in a stable order."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    seen = []
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip().lower()
        # "personal accident cover" -> "personal accident"
        for valid in VALID_CATEGORIES:
            if valid in name and valid not in seen:
                seen.append(valid)
                break
    return seen


def _clean_doc_list(value) -> list[str]:
    """
    Normalise a list of document names.

    Models return these as plain strings, as objects with a name key, or as
    one comma-joined string. All three are common enough to handle rather
    than lose the list over.
    """
    if isinstance(value, str):
        value = [part for part in re.split(r"[;\n]|,(?=\s*[A-Z])", value)]
    if not isinstance(value, list):
        return []

    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("document") or item.get("text") or ""
        name = re.sub(r"\s+", " ", str(item)).strip(" .-•")
        # Long entries are sentences about a document, not its name.
        if name and len(name) <= 120 and not any(
            n.casefold() == name.casefold() for n in out
        ):
            out.append(name)
    return out[:25]


def _clean_notes(value) -> str:
    """
    Risk notes, flattened to prose.

    Models ignore "not bullet points" often enough that a list comes back
    anyway; joining it is better than showing the broker a JSON array. Each
    item is stripped of its marker BEFORE joining — stripping after would
    only catch the first, since joining leaves no line starts behind.
    """
    if isinstance(value, list):
        parts = []
        for item in value:
            part = re.sub(r"^\s*[-•*\u2022]\s*", "", str(item).strip())
            if part:
                parts.append(part)
        value = " ".join(parts)

    text = str(value or "").strip()
    # Same again for a single string that came back with markers inline.
    text = re.sub(r"^\s*[-•*\u2022]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+[-•*\u2022]\s+", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()[:4000]


def extract_client(text: str) -> dict:
    """
    Read a client document into proposed intake fields.

    Returns the fields plus `redacted_preview`, the text the model was
    actually given, so the broker can see what was read rather than trust it.
    """
    if not text or not text.strip():
        raise LLMError("That document has no readable text in it.")

    identifiers = extract_identifiers(text)

    # Redact before the model reads it. Seeded with what was just found, so
    # the same PAN is one placeholder throughout rather than several.
    redactor = Redactor()
    for field, kind in IDENTIFIER_FIELDS.items():
        if identifiers.get(field):
            redactor.register(kind, identifiers[field])

    body = text[:MAX_CHARS]
    truncated = len(text) > MAX_CHARS
    redacted = redactor.redact(body)

    data = generate_json(load_prompt("extract_client", document_text=redacted))

    fields = {
        "business_name": _clean(data.get("business_name")),
        "entity_type": _clean(data.get("entity_type")),
        "industry": _clean(data.get("industry")),
        "city": _clean(data.get("city")),
        "state": _clean(data.get("state")),
        "turnover": _clean(data.get("turnover")),
        "years_trading": _clean(data.get("years_trading")),
        "employee_count": _clean(data.get("employee_count")),
        "sum_insured": _clean(data.get("sum_insured")),
        "contact_name": _clean(data.get("contact_name")),
        **identifiers,
    }

    # A model handed redacted text sometimes copies a placeholder into a
    # field despite being told not to. Those are not real values.
    for key, value in list(fields.items()):
        if "{{" in value:
            fields[key] = "" if key not in identifiers else identifiers[key]

    categories = _clean_categories(data.get("categories"))
    risk_notes = _clean_notes(data.get("risk_notes"))
    documents_held = _clean_doc_list(data.get("documents_held"))
    documents_missing = _clean_doc_list(data.get("documents_missing"))

    # Did this look like a client submission at all?
    #
    # The model is told to return empty values rather than guess, so a
    # document that is not a business insurance submission comes back almost
    # entirely blank. Rather than hand the broker an empty form with no
    # explanation — which reads as a broken feature — say plainly that the
    # file did not look like the right kind of document.
    #
    # Judged on the identifying business facts, not on the identifiers: a
    # letter can carry a PAN without being a submission, and a real submission
    # can omit its GSTIN.
    signals = [
        bool(fields["business_name"]),
        bool(fields["industry"]),
        bool(fields["turnover"]),
        bool(fields["entity_type"]),
        bool(categories),
        len(risk_notes) > 80,
        bool(documents_held or documents_missing),
    ]
    strength = sum(signals)

    return {
        "fields": fields,
        "categories": categories,
        "risk_notes": risk_notes,
        "documents_held": documents_held,
        "documents_missing": documents_missing,
        "found_identifiers": sorted(identifiers.keys()),
        "redacted_preview": redacted,
        "truncated": truncated,
        # 0-2 of seven signals is not a submission; 3 is thin but plausible.
        "recognised": strength >= 3,
        "confidence": strength,
    }
