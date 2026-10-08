"""
Phase 1C — deterministic policy-structure reader (v0.11.0).

Reads the STRUCTURE of insurance documents from labelled lines only — never
from the model, never by inference:

    document role      policy / schedule / endorsement / risk_survey / loss_run / unknown
    policy.number      "Policy Number: X" / "Policy No: X"
    policy.insurer     "Carrier: X" / "Insurer: X" / "Insurance Company: X" / "Underwriter: X"
    policy.condition   sentences under a "Conditions" heading
    policy.definition  '"Term" means ...' / '"Term" shall mean ...'
    policy.sublimit    a line that says "sub-limit"/"sublimit" AND states an amount
    coverage.limit     "<Coverage> Sum Insured: X" / "<Coverage> Limit [of Indemnity]: X",
                       only for a coverage named on a "Perils Covered:" / "Coverages:" line
    endorsement        "Endorsement <ID>" heading + "Policy: <number>" + "Effective: <date>"

Anything not stated in that labelled form is simply not produced — the
Knowledge Core then reports it as unknown rather than guessed. Every item
carries the page and the exact line it came from (the evidence snippet).
"""
from __future__ import annotations

import re

_PAGE_RE = re.compile(r"^\s*\[Page (\d+)\]\s*$")
_NUMBER_RE = re.compile(r"^\s*Policy\s*(?:Number|No\.?|#)\s*[:\-]\s*(\S.*?)\s*$", re.I)
_INSURER_RE = re.compile(r"^\s*(?:Carrier|Insurer|Insurance\s+Company|Underwriter)\s*[:\-]\s*(\S.*?)\s*$", re.I)
_COND_HEAD_RE = re.compile(r"^\s*(?:General\s+|Policy\s+)?Conditions\s*:?\s*$", re.I)
_DEFN_RE = re.compile(r"[\"“]([^\"”]{2,60})[\"”]\s+(?:means|shall\s+mean)\s+([^.]{3,400}\.?)", re.I)
_SUBLIMIT_RE = re.compile(r"sub[\s-]?limit", re.I)
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:crore|cr\b|lakh|lac\b)?", re.I)
_PERILS_RE = re.compile(r"^\s*(?:Perils\s+Covered|Coverages?|Covers?)\s*[:\-]\s*(\S.*?)\s*$", re.I)
_ENDT_HEAD_RE = re.compile(r"^\s*Endorsement\s+(?:No\.?\s*|Number\s*|#\s*)?([A-Za-z0-9][\w\-/.]*\d[\w\-/.]*)\s*$", re.I)
_POLICY_REF_RE = re.compile(r"^\s*Policy\s*(?:Number|No\.?|Ref(?:erence)?)?\s*:\s*(\S.*?)\s*$", re.I)
_EFFECTIVE_RE = re.compile(r"^\s*Effective(?:\s+Date|\s+From)?\s*[:\-]\s*(\S.*?)\s*$", re.I)
# A heading line ends a Conditions block.
_HEADING_RE = re.compile(
    r"^\s*(?:Exclusions?|Definitions?|Coverage|Coverages|Property\s+Details|Policy\s+Details|Endorsements?|"
    r"Schedule|Insured|Deductible|Declared\s+Values|Change\s+\d+|Signed|Notes?)\s*:?\s*$", re.I)
_FOOTER_RE = re.compile(r"NOT\s+REAL\s+INSURANCE\s+DATA|^\s*Page\s+\d+\s*$", re.I)

_ROLE_TITLES = [
    ("endorsement", re.compile(r"^\s*(?:Policy\s+)?Endorsement\b", re.I)),
    ("schedule", re.compile(r"^\s*(?:Coverage\s+|Policy\s+)?Schedule\b|Declarations?\b", re.I)),
    ("risk_survey", re.compile(r"^\s*(?:Property\s+)?Risk\s+Survey\b|^\s*Survey\s+Report\b", re.I)),
    ("loss_run", re.compile(r"^\s*(?:Synthetic\s+|Claims?\s+)?Loss\s+Run\b|^\s*Claims?\s+History\b", re.I)),
    ("policy", re.compile(r"^\s*(?:Property\s+|Fire\s+|Commercial\s+)?(?:Insurance\s+)?Policy(?:\s+Wording|\s+Document)?\s*$", re.I)),
]


def _clean(v: str) -> str:
    return " ".join((v or "").split())


def _lines_with_pages(text: str):
    page = None
    for line in (text or "").splitlines():
        m = _PAGE_RE.match(line)
        if m:
            page = int(m.group(1))
            continue
        if line.strip():
            yield page, line


def classify_document_role(text: str) -> str:
    """Role from the document's own title lines (the first few non-empty lines)."""
    head = [l for _, l in _lines_with_pages(text)][:6]
    for role, rx in _ROLE_TITLES:
        if any(rx.search(l) for l in head):
            return role
    return "unknown"


def _amount(s: str):
    m = _AMOUNT_RE.search(s.replace("₹", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def extract_policy_structure(text: str) -> dict:
    """Return {"role", "items": [...], "endorsements": [...], "policy_refs": [...]}.

    items: {fact_type, value, normalized_value, page, snippet, coverage?}
    endorsements: {endorsement_id, policy_ref, effective_date, page, snippet}
    """
    import property_policy_intelligence as ppi     # date parser lives there

    role = classify_document_role(text)
    lines = list(_lines_with_pages(text))
    items, endorsements = [], []

    def add(ft, value, page, snippet, normalized=None, coverage=None):
        value = _clean(value)
        if not value:
            return
        item = {"fact_type": ft, "value": value, "normalized_value": normalized,
                "page": page, "snippet": _clean(snippet)[:500]}
        if coverage:
            item["coverage"] = coverage
        if not any(i["fact_type"] == ft and i["value"] == value and i.get("coverage") == coverage for i in items):
            items.append(item)

    # Policy number / insurer: on policy and schedule documents only. An
    # endorsement's "Policy: X" is a REFERENCE to a policy, not its own number.
    if role in ("policy", "schedule", "unknown"):
        for page, line in lines:
            m = _NUMBER_RE.match(line)
            if m:
                add("policy.number", m.group(1), page, line)
            m = _INSURER_RE.match(line)
            if m:
                add("policy.insurer", m.group(1), page, line)

    # Coverages named on a perils/coverages line -> per-coverage limits.
    coverages = []
    for page, line in lines:
        m = _PERILS_RE.match(line)
        if m:
            coverages += [_clean(c) for c in re.split(r",|;|\band\b", m.group(1)) if _clean(c)]
    for cov in coverages:
        rx = re.compile(rf"^\s*{re.escape(cov)}\s+(?:Sum\s+Insured|Limit(?:\s+of\s+(?:Indemnity|Liability))?)"
                        rf"\s*[:\-]\s*(.+)$", re.I)
        for page, line in lines:
            m = rx.match(line)
            if m and not _SUBLIMIT_RE.search(line):
                amt = _amount(m.group(1))
                if amt is not None and "%" not in m.group(1):
                    add("coverage.limit", f"{amt:,.0f}", page, line, amt, coverage=cov)

    # Conditions block: lines after a "Conditions" heading until the next heading/footer.
    i = 0
    while i < len(lines):
        page, line = lines[i]
        if _COND_HEAD_RE.match(line):
            block, j = [], i + 1
            while j < len(lines) and not _HEADING_RE.match(lines[j][1]) and not _FOOTER_RE.search(lines[j][1]) \
                    and not _COND_HEAD_RE.match(lines[j][1]):
                block.append(lines[j])
                j += 1
            joined = " ".join(_clean(l) for _, l in block)
            joined = re.sub(r"^\s*\d+[.)]\s*", "", joined)
            for sent in re.split(r"(?<=[.;])\s+(?=[A-Z0-9(])|\s+\d+[.)]\s+", joined):
                sent = _clean(re.sub(r"^\d+[.)]\s*", "", sent))
                if len(sent) >= 8:
                    pg = next((p for p, l in block if sent[:25] in _clean(l)), block[0][0] if block else page)
                    add("policy.condition", sent, pg, sent)
            i = j
            continue
        i += 1

    # Definitions anywhere in the text.
    for page, line in lines:
        for m in _DEFN_RE.finditer(line):
            add("policy.definition", f"\"{_clean(m.group(1))}\" means {_clean(m.group(2))}", page, m.group(0))

    # Sub-limits: the line must say so AND state an amount (percentages stay text).
    for page, line in lines:
        if _SUBLIMIT_RE.search(line) and _AMOUNT_RE.search(line):
            amt = None if "%" in line else _amount(line.split(":", 1)[-1])
            add("policy.sublimit", line, page, line, amt)

    # Endorsement metadata.
    if role == "endorsement":
        cur = None
        for page, line in lines:
            m = _ENDT_HEAD_RE.match(line)
            if m:
                cur = {"endorsement_id": m.group(1), "policy_ref": None, "effective_date": None,
                       "page": page, "snippet": _clean(line)}
                endorsements.append(cur)
                continue
            if cur is None:
                continue
            m = _POLICY_REF_RE.match(line)
            if m and cur["policy_ref"] is None:
                cur["policy_ref"] = _clean(m.group(1))
                continue
            m = _EFFECTIVE_RE.match(line)
            if m and cur["effective_date"] is None:
                d = ppi._parse_date(m.group(1))
                cur["effective_date"] = d.isoformat() if d else None

    return {"role": role, "items": items, "endorsements": endorsements,
            "coverages": coverages}
