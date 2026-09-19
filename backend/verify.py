"""
Verifying a document a client actually sent.

Phase 3 asked "do you have it?" and took the client's word. This asks "is
this it?" — the document is read, identified, and cross-checked against what
is already on the client's record.

Three layers, in order of how much they can be trusted:

  1. STRUCTURAL. PAN, GSTIN, IFSC and Aadhaar have fixed formats, and GSTIN
     and Aadhaar carry checksums that can be computed. These are arithmetic:
     a number either validates or it does not, and no model is asked.

  2. CROSS-CHECK. Whatever identifiers the document contains are compared
     against the client's record. A PAN card whose PAN is not the client's
     PAN is the single most valuable thing this module catches, and it is a
     string comparison rather than a judgement.

  3. SUBSTANCE. Whether a document satisfies a requirement — whether this
     really is a balance sheet, whether it covers the right period — has no
     fixed shape and needs a model.

Deliberately NOT template matching. There is no official layout for a
CA-certified P&L or a stock statement; they come out of different accounting
packages, on different letterheads, in different years. Checking against a
master copy would reject valid documents constantly, which trains a broker to
ignore the tool. Substance is judged instead, so an unofficially formatted
document is read on what it says rather than how it looks.

This module reads documents the CLIENT supplies. Documents the broker
generates are a later concern and do not belong here.
"""
import logging
import re

from llm import LLMError, generate_json, load_prompt
from tokenise import Redactor

logger = logging.getLogger(__name__)

# Long documents are truncated for the model. Identifiers are searched for in
# the FULL text first, so truncation never loses a PAN that appears late.
MAX_CHARS = 12000

BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")
IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
AADHAAR_RE = re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}\b")

# PAN's 4th character encodes the holder type. Useful because a requirement
# asking for the ENTITY's PAN is not satisfied by an individual's PAN.
PAN_HOLDER = {
    "P": "individual", "C": "company", "H": "HUF", "F": "partnership firm",
    "A": "association of persons", "T": "trust", "B": "body of individuals",
    "L": "local authority", "J": "artificial juridical person", "G": "government",
}


def gstin_checksum_valid(gstin: str) -> bool:
    """
    Whether a GSTIN's 15th character matches its computed check digit.

    Base-36 weighted sum over the first 14 characters, alternating weights of
    1 and 2, with each product's quotient and remainder both added. This is
    the actual GST algorithm, so a mistyped digit is caught here rather than
    at submission.
    """
    gstin = gstin.strip().upper()
    if len(gstin) != 15 or any(c not in BASE36 for c in gstin):
        return False

    total = 0
    for position, char in enumerate(gstin[:14]):
        product = BASE36.index(char) * (2 if position % 2 else 1)
        total += product // 36 + product % 36

    return BASE36[(36 - total % 36) % 36] == gstin[14]


def aadhaar_checksum_valid(number: str) -> bool:
    """
    Verhoeff check on a 12-digit Aadhaar.

    Included because an Aadhaar with a transposed pair is a common typo and
    is otherwise indistinguishable from a valid one.
    """
    digits = re.sub(r"\D", "", number)
    if len(digits) != 12:
        return False

    d = [
        [0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],
        [3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],
        [6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],[8,7,6,5,9,3,2,1,0,4],
        [9,8,7,6,5,4,3,2,1,0],
    ]
    p = [
        [0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],
        [8,9,1,6,0,4,3,5,2,7],[9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],
        [2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8],
    ]

    check = 0
    for position, digit in enumerate(reversed(digits)):
        check = d[check][p[position % 8][int(digit)]]
    return check == 0


def find_identifiers(text: str) -> dict:
    """
    Every identifier in the document, with its structural verdict.

    GSTIN is searched before PAN and its match consumed, because a GSTIN
    contains a PAN and would otherwise be reported as both.
    """
    found = {}

    gstins = GSTIN_RE.findall(text)
    if gstins:
        found["gstin"] = [
            {"value": g, "checksum_valid": gstin_checksum_valid(g)} for g in _unique(gstins)
        ]

    # Remove GSTINs before looking for PANs, so the PAN inside one is not
    # double-reported.
    without_gstin = GSTIN_RE.sub(" ", text)
    pans = PAN_RE.findall(without_gstin)
    if pans:
        found["pan"] = [
            {
                "value": p,
                "holder_type": PAN_HOLDER.get(p[3], "unknown"),
                # PAN carries no checksum; structure is all that can be checked.
                "checksum_valid": None,
            }
            for p in _unique(pans)
        ]

    ifscs = IFSC_RE.findall(text)
    if ifscs:
        found["ifsc"] = [{"value": i, "checksum_valid": None} for i in _unique(ifscs)]

    aadhaars = AADHAAR_RE.findall(text)
    if aadhaars:
        found["aadhaar"] = [
            {"value": a, "checksum_valid": aadhaar_checksum_valid(a)}
            for a in _unique(aadhaars)
        ]

    return found


def _unique(values: list[str]) -> list[str]:
    seen, out = set(), []
    for value in values:
        key = re.sub(r"\s", "", value).upper()
        if key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


def cross_check(identifiers: dict, client: dict) -> list[dict]:
    """
    Compare what the document says against what is on the client's record.

    The highest-value check in this module: a document belonging to a
    different entity is otherwise invisible, and a mismatched PAN reaching an
    insurer is a real problem rather than a tidiness one.

    A mismatch is reported, never auto-resolved — the record could be the
    wrong one, and only the broker can say which.
    """
    notes = []

    for field, label in (("pan", "PAN"), ("gstin", "GSTIN")):
        on_record = str(client.get(field) or "").strip().upper()
        in_document = [
            entry["value"].upper().replace(" ", "") for entry in identifiers.get(field, [])
        ]
        if not on_record or not in_document:
            continue

        if on_record in in_document:
            notes.append(
                {"level": "ok", "text": f"{label} in the document matches the record."}
            )
        else:
            notes.append(
                {
                    "level": "mismatch",
                    "text": (
                        f"{label} in the document ({in_document[0]}) does not match "
                        f"the {label} on record ({on_record}). Check which is right "
                        "before submitting."
                    ),
                }
            )

    # A GSTIN embeds the holder's PAN. If both appear, they must agree.
    gstins = [e["value"].upper() for e in identifiers.get("gstin", [])]
    pan_on_record = str(client.get("pan") or "").strip().upper()
    for gstin in gstins:
        if pan_on_record and len(gstin) == 15 and gstin[2:12] != pan_on_record:
            notes.append(
                {
                    "level": "mismatch",
                    "text": (
                        f"The GSTIN in the document embeds PAN {gstin[2:12]}, which is "
                        f"not the PAN on record ({pan_on_record})."
                    ),
                }
            )

    for kind, entries in identifiers.items():
        for entry in entries:
            if entry.get("checksum_valid") is False:
                notes.append(
                    {
                        "level": "warning",
                        "text": (
                            f"The {kind.upper()} {entry['value']} fails its checksum, so "
                            "it is not a validly formed number. Usually a typo — worth "
                            "confirming before it goes to an insurer."
                        ),
                    }
                )

    return notes


def verify(*, text: str, requirement: str, client: dict) -> dict:
    """
    Verify one uploaded document against one requirement.

    Returns a verdict plus the reasoning behind it. The verdict is derived
    from the layers above, not taken from the model alone: a mismatch found
    by string comparison outranks a model saying the document looks fine.
    """
    if not text or not text.strip():
        return {
            "verdict": "unreadable",
            "doc_type": "",
            "satisfies": False,
            "reason": "No readable text could be extracted from this file.",
            "identifiers": {},
            "notes": [],
            "key_facts": [],
            "improvements": ["Send a copy the text can be read from — a PDF "
                             "exported from the source, rather than a photo or scan."],
            "score": 0,
            "score_parts": {},
        }

    # Full text, before truncation — an identifier on the last page still counts.
    identifiers = find_identifiers(text)
    notes = cross_check(identifiers, client)

    # The model reads the document with identifiers replaced, same boundary
    # as everywhere else: it judges substance, and never needs the numbers.
    redactor = Redactor()
    for kind, entries in identifiers.items():
        for entry in entries:
            redactor.register(kind.upper(), entry["value"])

    redacted = redactor.redact(text[:MAX_CHARS])

    try:
        data = generate_json(
            load_prompt(
                "verify_document",
                requirement=requirement,
                document_text=redacted,
                business_name=client.get("business_name") or "(not on record)",
            )
        )
    except LLMError as exc:
        # Structural findings survive a model failure and are still worth
        # showing — a checksum failure is useful on its own.
        return {
            "verdict": "unchecked",
            "doc_type": "",
            "satisfies": False,
            "reason": f"The document was read but could not be assessed: {exc}",
            "identifiers": identifiers,
            "notes": notes,
            "key_facts": [],
            "improvements": [],
            "score": None,
            "score_parts": {},
        }

    scores = _scores(data)
    doc_type = _clean(data.get("document_type"))
    satisfies = bool(data.get("satisfies_requirement"))
    reason = _clean(data.get("reason"), 400)
    key_facts = _key_facts(data.get("key_facts"))
    improvements = _strings(data.get("improvements"))

    for concern in _strings(data.get("concerns")):
        notes.append({"level": "warning", "text": concern})

    # Verdict. A mismatch outranks everything the model said: the document may
    # well be a perfectly good balance sheet, and still be the wrong company's.
    if any(n["level"] == "mismatch" for n in notes):
        verdict = "mismatch"
    elif not satisfies:
        verdict = "wrong_document"
    elif any(n["level"] == "warning" for n in notes):
        verdict = "check"
    else:
        verdict = "verified"

    # A document that is the wrong one, or belongs to someone else, scores
    # zero regardless of how complete and legible it is. A pristine balance
    # sheet for a different company is not 90% of the way to acceptable.
    overall = 0 if verdict in {"mismatch", "wrong_document", "unreadable"} else scores["overall"]

    return {
        "verdict": verdict,
        "doc_type": doc_type,
        "satisfies": satisfies,
        "reason": reason,
        "identifiers": identifiers,
        "notes": notes,
        "key_facts": key_facts,
        "improvements": improvements,
        "score": overall,
        "score_parts": scores["parts"],
    }


# How the three scores combine. Completeness is weighted hardest because an
# incomplete document is the one an underwriter sends straight back; legibility
# least, because a slightly rough scan that still reads is usable.
SCORE_WEIGHTS = {"completeness": 0.5, "currency": 0.3, "legibility": 0.2}


def _scores(data: dict) -> dict:
    """
    Pull the three sub-scores out and combine them.

    A missing or unparseable score becomes 70 rather than 0 or 100: a model
    that failed to answer has told us nothing, and treating silence as either
    a pass or a failure would be inventing a judgement it did not make.
    """
    parts = {}
    for key in SCORE_WEIGHTS:
        raw = data.get(key)
        try:
            value = int(round(float(raw)))
        except (TypeError, ValueError):
            value = 70
        parts[key] = max(0, min(100, value))

    overall = round(sum(parts[k] * w for k, w in SCORE_WEIGHTS.items()))
    return {"parts": parts, "overall": overall}


def _clean(value, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("text") or item.get("concern") or ""
        text = _clean(item, 300)
        if text:
            out.append(text)
    return out[:8]


def _key_facts(value) -> list[dict]:
    """Facts the document states — period covered, totals, dates."""
    if not isinstance(value, list):
        return []
    facts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("label") or item.get("name"), 60)
        detail = _clean(item.get("value") or item.get("detail"), 120)
        if label and detail:
            facts.append({"label": label, "value": detail})
    return facts[:10]
