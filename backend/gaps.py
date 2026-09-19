"""
Gap analysis: what is still missing before a carrier will quote.

Phase 1 extracted each carrier's requirements from its own wording. Phase 2
worked out which carriers suit a client. This checks one against the other
and produces a checklist — held, missing, or unclear — with the specific
thing to ask the client for.

Two halves, and the split matters:

  * Field presence is DETERMINISTIC. Whether a PAN is on record is a fact
    about the database, not a judgement, so it is answered by looking. The
    model is never asked, and its answer is overridden if it disagrees.

  * Mapping a requirement to what satisfies it is not deterministic. "Latest
    audited or CA-certified P&L and Balance Sheet for turnover verification"
    has to be matched against a document list written in the client's own
    words. That needs a model.

The backstop below is the important part. A model told that a file contains a
PAN still sometimes marks the PAN requirement missing; when the database says
otherwise, the database wins. That keeps the common cases exact and leaves
the model to do only the part that genuinely needs reading.
"""
import logging
import re

from llm import LLMError, generate_json, load_prompt

logger = logging.getLogger(__name__)

VALID_STATUS = {"held", "missing", "unclear"}

# Cap per carrier. A wording with more requirements than this is summarising
# badly, and sending all of them would push a 7B model past careful reading.
MAX_REQUIREMENTS = 30

# Words that mean a requirement is asking for a DOCUMENT, not for a fact.
#
# This guard matters more than the rules below it. "CA-certified P&L and
# Balance Sheet for turnover verification" mentions turnover, but holding a
# turnover figure is not holding audited accounts — without this, a populated
# field would silently satisfy a requirement for the paperwork evidencing it,
# which is exactly the gap the broker needed to see.
DOCUMENT_WORDS = re.compile(
    r"\b(certificate|statement|deed|proof|copy|copies|document|letter|licen[cs]e|"
    r"NOC|report|invoice|receipt|bill|accounts?|balance sheet|P&L|audited|"
    r"CA-certified|census|list|schedule|photograph|form|challan|declaration)\b",
    re.I,
)

# Requirement text -> the client field that settles it. Matched on the
# requirement wording, so a requirement mentioning PAN is resolved by whether
# a PAN is actually on file rather than by the model's reading of it.
#
# These answer "is this FACT on record", never "does the client hold this
# PAPER" — DOCUMENT_WORDS above stops them firing on the latter.
#
# Only unambiguous ones belong here. "Address proof" is deliberately absent:
# holding a city and state is not the same as holding a utility bill, and
# forcing it to "held" would hide a real gap.
FIELD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bPAN\b", re.I), "pan"),
    (re.compile(r"\bGST(IN)?\b|goods and services tax", re.I), "gstin"),
    (re.compile(r"\bturnover\b", re.I), "turnover"),
    (re.compile(r"number of employees|\bemployee count\b", re.I), "employee_count"),
    (re.compile(r"entity type|constitution of the (business|entity)|legal form", re.I), "entity_type"),
    (re.compile(r"sum insured (sought|required|proposed)", re.I), "sum_insured"),
]

# Ignored when comparing a requirement to a document name — they appear in
# almost every requirement and would make everything look similar.
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "any", "all", "not",
    "where", "which", "per", "its", "their", "must", "be", "of", "to", "in",
    "on", "or", "a", "an", "is", "are", "as", "at", "by", "latest", "copy",
    "copies", "document", "documents", "required", "provide", "provided",
}


def _significant(text: str) -> set[str]:
    """Distinctive words of a phrase, for loose comparison."""
    words = re.findall(r"[A-Za-z&]{3,}", text.casefold())
    return {w for w in words if w not in STOPWORDS}


def _same_requirement(a: str, b: str) -> bool:
    """
    Whether two requirement strings refer to the same requirement.

    Not compared exactly: attachments are keyed by the requirement text as it
    read when the file was uploaded, and re-running a briefing can reword a
    requirement slightly without changing what it asks for. Compared on
    distinctive words so an attachment survives a re-brief.
    """
    first, second = _significant(a), _significant(b)
    if not first or not second:
        return False
    overlap = first & second
    return len(overlap) / min(len(first), len(second)) >= 0.75


def _matches_document(requirement: str, document: str) -> bool:
    """
    Whether a requirement is asking for this named document.

    Compared on the DOCUMENT's distinctive words, not the requirement's: a
    requirement is usually the longer, wordier side ("Latest audited or
    CA-certified P&L and Balance Sheet for turnover verification" against
    "Audited / CA-certified P&L and Balance Sheet"), so asking how much of
    the document name appears in the requirement is the right direction.
    """
    document_words = _significant(document)
    if len(document_words) < 2:
        return False
    overlap = document_words & _significant(requirement)
    return len(overlap) / len(document_words) >= 0.6


def _as_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_client_file(client: dict) -> str:
    """
    Describe what is on record, for the model to check requirements against.

    Field NAMES and whether each is populated, never raw values — the model
    does not need a turnover figure to judge that a turnover is on record,
    and the redacted profile already carries the business context it does
    need for anything judgement-based.
    """
    lines = []

    labels = {
        "business_name": "Business name",
        "entity_type": "Entity type / legal form",
        "industry": "Industry",
        "city": "City",
        "state": "State",
        "turnover": "Annual turnover",
        "years_trading": "Years trading",
        "employee_count": "Employee count",
        "sum_insured": "Sum insured sought",
        "pan": "PAN",
        "gstin": "GSTIN",
        "contact_name": "Contact name",
        "contact_phone": "Contact phone",
        "contact_email": "Contact email",
    }

    for field, label in labels.items():
        present = bool(_as_text(client.get(field)))
        lines.append(f"- {label}: {'ON RECORD' if present else 'NOT ON RECORD'}")

    notes = _as_text(client.get("risk_notes"))
    if notes:
        lines.append("")
        lines.append("Broker notes on file:")
        lines.append(notes[:2500])

    return "\n".join(lines)


# Attachment verdicts that settle a requirement, and what they settle it to.
# "check" is deliberately absent: a document with a concern attached needs a
# human to look, so it is left for the model to describe rather than being
# forced either way.
ATTACHMENT_VERDICTS = {
    "verified": ("held", "Verified document attached: {filename}."),
    "mismatch": (
        "missing",
        "A document is attached ({filename}) but it does not match this client's "
        "record — see the attachment for details.",
    ),
    "wrong_document": (
        "missing",
        "The attached document ({filename}) is not what this requirement asks for.",
    ),
    "unreadable": (
        "missing",
        "The attached file ({filename}) could not be read.",
    ),
}


def _deterministic_status(requirement: str, client: dict) -> tuple[str | None, str]:
    """
    Settle a requirement from the database where the database can settle it.

    Returns "held", "missing", or None when no rule applies and the model's
    judgement should stand.

    Checked in order of how directly each source speaks to the requirement:

      1. A document actually attached and verified against this requirement.
         The strongest evidence there is: the file was read, identified, and
         its identifiers matched the client's record. Outranks the client's
         own claim either way — an attachment can prove they DO hold
         something they said they did not.
      2. An attachment that failed verification. Marked missing rather than
         held: a mismatched or wrong-type document is not a satisfied
         requirement, and treating it as one would send the wrong paper to an
         insurer.
      3. The client saying they do NOT hold a document. A statement of fact
         from the person who would know — a model marking such a requirement
         "held" hides a real gap, which is the failure that costs a broker.
      4. A field being on record, but only for requirements asking for a
         fact. DOCUMENT_WORDS keeps this away from requirements asking for
         paperwork.

    Deliberately NOT settled deterministically: a document appearing in the
    held list. Names overlap loosely enough that forcing "held" could mark a
    requirement satisfied by a document that does not actually satisfy it,
    and that error is silent. Overlap there is left to the model, which can
    read both and judge.
    """
    # Attachments first — reading the actual file beats every other source.
    for attachment in client.get("_attachments") or []:
        if not _same_requirement(requirement, attachment.get("requirement", "")):
            continue
        settled = ATTACHMENT_VERDICTS.get(attachment.get("verdict"))
        if settled:
            status, template = settled
            return status, template.format(filename=attachment.get("filename", "file"))
        # "check" or "unchecked": an attachment exists but does not settle it.
        return None, ""

    for document in client.get("documents_missing") or []:
        if _matches_document(requirement, str(document)):
            return "missing", f"The client has said they do not have this ({document})."

    if DOCUMENT_WORDS.search(requirement):
        return None, ""

    for pattern, field in FIELD_RULES:
        if pattern.search(requirement):
            if _as_text(client.get(field)):
                return "held", "On record in the client file."
            return "missing", "Not on record in the client file."
    return None, ""


def _normalise_items(raw, requirements: list[dict], client: dict) -> list[dict]:
    """
    Clean the model's items and align them back to the real requirements.

    Aligned by position, because the prompt asks for one item per numbered
    requirement in order. A model that returns fewer is filled in as
    "unclear" rather than silently dropping a requirement — a requirement
    that vanishes from a checklist is the one that gets forgotten.
    """
    by_position: dict[int, dict] = {}
    if isinstance(raw, list):
        for position, item in enumerate(raw):
            if isinstance(item, dict):
                by_position[position] = item

    items = []
    for position, requirement in enumerate(requirements):
        raw_item = by_position.get(position, {})

        status = str(raw_item.get("status") or "").strip().lower()
        if status not in VALID_STATUS:
            status = "unclear"

        reason = _as_text(raw_item.get("reason"))[:400]
        ask = _as_text(raw_item.get("ask"))[:300]

        # The record wins where the record can answer.
        settled, settled_reason = _deterministic_status(requirement["text"], client)
        if settled and settled != status:
            logger.info(
                "Overriding model status %r -> %r for: %s",
                status, settled, requirement["text"][:70],
            )
            status = settled
            reason = settled_reason
            ask = "" if settled == "held" else ask

        if status == "held":
            ask = ""

        items.append(
            {
                "requirement": requirement["text"],
                "status": status,
                "reason": reason,
                "ask": ask,
                # Carried through so a broker can check the requirement
                # against the actual page of the wording it came from.
                "section_title": requirement.get("section_title"),
                "page_start": requirement.get("page_start"),
                "filename": requirement.get("filename"),
            }
        )

    return items


def analyse(*, client: dict, carrier_name: str, requirements: list[dict]) -> dict:
    """
    Check one client's file against one carrier's requirements.

    `client` is the stored row. Only field presence and the redacted notes
    are used; raw identifier values never reach the model.
    """
    requirements = [r for r in requirements if _as_text(r.get("text"))][:MAX_REQUIREMENTS]
    if not requirements:
        raise LLMError(
            f"{carrier_name}'s briefing lists no requirements, so there is "
            "nothing to check against. Open that carrier and press Regenerate "
            "briefing — if it still finds none, its documents may not state "
            "what the buyer has to provide."
        )

    held = [_as_text(d) for d in (client.get("documents_held") or []) if _as_text(d)]
    missing = [_as_text(d) for d in (client.get("documents_missing") or []) if _as_text(d)]

    numbered = "\n".join(
        f"[{position}] {r['text']}" for position, r in enumerate(requirements, start=1)
    )

    data = generate_json(
        load_prompt(
            "gap_analysis",
            client_file=build_client_file(client),
            documents_held="\n".join(f"- {d}" for d in held) or "(none recorded)",
            documents_missing="\n".join(f"- {d}" for d in missing) or "(none recorded)",
            carrier_name=carrier_name,
            requirements=numbered,
        )
    )

    items = _normalise_items(data.get("items"), requirements, client)

    counts = {
        "held": sum(1 for i in items if i["status"] == "held"),
        "missing": sum(1 for i in items if i["status"] == "missing"),
        "unclear": sum(1 for i in items if i["status"] == "unclear"),
    }

    summary = _as_text(data.get("summary"))
    if not summary:
        # A missing summary should not cost the checklist, which is the part
        # that matters. Build a plain one from the counts instead.
        summary = (
            f"{counts['missing']} requirement(s) outstanding and "
            f"{counts['unclear']} to confirm, out of {len(items)} for "
            f"{carrier_name}."
        )

    return {
        "items": items,
        "summary": summary[:1200],
        "stats": {**counts, "total": len(items)},
    }
