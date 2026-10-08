"""
Phase 1D — deterministic business-risk reader (v0.12.0).

Reads what a document EXPLICITLY states about the insured business, from
labelled lines only — never from the model, never from industry knowledge:

    location      "Location: …" / "Property Location: …" / "Additional Location: …"
    operation     "The insured operates X." / "Operations: X"
    workforce     "Estimated workforce: N" / "Employees: N"
    risk rating   "Overall risk rating: X"           (the surveyor's documented rating — not ours)
    control       "Fire protection: …", "Security: …", "Sprinkler system: …", "Fire alarm: …",
                  "Backup power: …", "Business continuity plan: …", "Access control: …",
                  "Preventive maintenance: …"
    asset value   lines of a "Declared Values" block ("Plant and Machinery: N"; totals excluded),
                  "Revised machinery declared value: N"  (applies only to an asset named …machinery…)
    losses        "claims in period: N", "Total incurred amount: N"
    legal name    "Insured: X"
    risk factor   "Risk Factors: …"
    risk          "Identified risk: …" / "Key risk: …" / "Documented risk: …" / "Hazard: …"
    exposure      "<kind> exposure: …" / "Exposure: …"

Observation date of a document: "Survey date:", "Report date:", "Effective:"
(endorsements); a policy or schedule is dated at the policy period start
(supplied by the caller). Everything else is simply not produced, so the
Knowledge Core reports it as not documented.
"""
from __future__ import annotations

import re

import policy_structure as ps

_LOC_RE = re.compile(r"^\s*(Additional\s+)?(?:Property\s+|Site\s+|Premises\s+)?(?:Location|Address)\s*:\s*(\S.*?)\s*$", re.I)
_DATE_LINE_RE = re.compile(r"^\s*(?:Survey\s+date|Report\s+date|Inspection\s+date|Effective(?:\s+date)?)\s*[:\-]\s*(\S.*?)\s*$", re.I)
_OPERATES_RE = re.compile(r"\bThe\s+insured\s+operates\s+([^.]{3,120})\.", re.I)
_OPERATIONS_LBL_RE = re.compile(r"^\s*Operations?\s*:\s*(\S.*?)\s*$", re.I)
_WORKFORCE_RE = re.compile(r"\b(?:Estimated\s+)?(?:workforce|employees|headcount)\s*:\s*([\d,]+)", re.I)
_RATING_RE = re.compile(r"^\s*Overall\s+risk\s+rating\s*:\s*([A-Za-z][A-Za-z \-]{1,30}?)\.?\s*$", re.I)
_CONTROL_RE = re.compile(
    r"^\s*(Fire\s+protection|Security|Sprinkler\s+system|Sprinklers|Fire\s+alarm|Alarm\s+system|Backup\s+power|"
    r"Business\s+continuity(?:\s+plan)?|Access\s+control|Preventive\s+maintenance)\s*:\s*(\S.*?)\s*$", re.I)
_DECLARED_HEAD_RE = re.compile(r"^\s*Declared\s+Values?\s*:?\s*$", re.I)
_VALUE_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z &/\-]{1,60}?)\s*:\s*\D{0,3}([\d,]{3,})\s*\.?\s*$")
_MACHINERY_REVISED_RE = re.compile(r"Revised\s+machinery\s+declared\s+value\s*:\s*\D{0,3}([\d,]{3,})", re.I)
_CLAIMS_RE = re.compile(r"claims\s+in\s+(?:the\s+)?period\s*:\s*(\d+)", re.I)
_INCURRED_RE = re.compile(r"Total\s+incurred\s+(?:amount)?\s*:\s*\D{0,3}([\d,]{1,})", re.I)
_INSURED_RE = re.compile(r"^\s*Insured\s*:\s*(\S.*?)\s*$", re.I)
_RISK_FACTOR_RE = re.compile(r"^\s*Risk\s+Factors?\s*:\s*(\S.*?)\s*$", re.I)
_RISK_RE = re.compile(r"^\s*(?:Identified|Key|Documented)\s+risks?\s*:\s*(\S.*?)\s*$|^\s*Hazard\s*:\s*(\S.*?)\s*$", re.I)
_EXPOSURE_RE = re.compile(r"^\s*(?:([A-Za-z ]{2,40})\s+)?Exposure\s*:\s*(\S.*?)\s*$", re.I)
_SECTION_END_RE = re.compile(r"^\s*(?:Total\b|Deductible|Observations|Operations|Claims|Policy|Survey|Schedule|Coverage)", re.I)


def _clean(v: str) -> str:
    return " ".join((v or "").split()).rstrip(".").strip()


def _num(v: str):
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def extract_business_structure(text: str) -> dict:
    """{"role", "observed_on" (ISO or None), "items": [...]}.

    item = {kind, name, fact_type, value, normalized_value, page, snippet, primary?}
    kind in location / operation / control / asset / claim / risk / risk_factor /
    exposure / business; `name` names the entity the fact belongs to.
    """
    import property_policy_intelligence as ppi

    role = ps.classify_document_role(text)
    lines = list(ps._lines_with_pages(text))
    items, observed_on = [], None

    def add(kind, name, ft, value, page, snippet, normalized=None, **extra):
        # A legal name keeps its final "." ("Pvt. Ltd."); descriptions drop a sentence's full stop.
        value = " ".join((value or "").split()) if ft == "business.legal_name" else _clean(value)
        if not value or not name:
            return
        it = {"kind": kind, "name": _clean(name), "fact_type": ft, "value": value,
              "normalized_value": normalized, "page": page, "snippet": " ".join(snippet.split())[:500], **extra}
        if not any(i["kind"] == kind and i["name"] == it["name"] and i["fact_type"] == ft and i["value"] == value
                   for i in items):
            items.append(it)

    for page, line in lines:
        m = _DATE_LINE_RE.match(line)
        if m and observed_on is None:
            d = ppi._parse_date(m.group(1))
            observed_on = d.isoformat() if d else None

    in_declared = False
    for page, line in lines:
        if _DECLARED_HEAD_RE.match(line):
            in_declared = True
            continue
        if in_declared:
            if _SECTION_END_RE.match(line) or ps._FOOTER_RE.search(line):
                in_declared = False
            else:
                m = _VALUE_LINE_RE.match(line)
                if m:
                    add("asset", m.group(1), "asset.declared_value", f"{_num(m.group(2)):,.0f}", page, line,
                        _num(m.group(2)))
                    continue
                if line.strip().endswith(":") or not re.search(r"\d", line):
                    in_declared = False

        m = _LOC_RE.match(line)
        if m and role in ("risk_survey", "policy", "unknown"):
            add("location", m.group(2), "location.address", m.group(2), page, line,
                primary=not m.group(1) and role == "risk_survey")
        for m in _OPERATES_RE.finditer(line):
            add("operation", m.group(1), "operation.description", m.group(1), page, line)
        m = _OPERATIONS_LBL_RE.match(line)
        if m and role in ("risk_survey", "unknown"):
            add("operation", m.group(1), "operation.description", m.group(1), page, line)
        m = _WORKFORCE_RE.search(line)
        if m and _num(m.group(1)) is not None:
            add("business", "business", "business.workforce", f"{_num(m.group(1)):,.0f}", page, line,
                _num(m.group(1)))
        m = _RATING_RE.match(line)
        if m:
            add("business", "business", "business.survey_risk_rating", m.group(1), page, line)
        m = _CONTROL_RE.match(line)
        if m:
            add("control", m.group(1).title(), "control.description", m.group(2), page, line)
        m = _MACHINERY_REVISED_RE.search(line)
        if m and _num(m.group(1)) is not None:
            add("asset", "__machinery__", "asset.declared_value", f"{_num(m.group(1)):,.0f}", page, line,
                _num(m.group(1)))
        m = _CLAIMS_RE.search(line)
        if m:
            add("claim", "loss run", "loss.claims_count", m.group(1), page, line, float(m.group(1)))
        m = _INCURRED_RE.search(line)
        if m and _num(m.group(1)) is not None:
            add("claim", "loss run", "loss.total_incurred", f"{_num(m.group(1)):,.0f}", page, line, _num(m.group(1)))
        m = _INSURED_RE.match(line)
        if m:
            add("business", "business", "business.legal_name", m.group(1), page, line)
        m = _RISK_FACTOR_RE.match(line)
        if m:
            add("risk_factor", m.group(1), "risk_factor.description", m.group(1), page, line)
        m = _RISK_RE.match(line)
        if m:
            v = m.group(1) or m.group(2)
            add("risk", v, "risk.description", v, page, line)
        m = _EXPOSURE_RE.match(line)
        if m and not re.match(r"^\s*(?:Total|Declared)", line, re.I):
            v = f"{_clean(m.group(1)) + ': ' if m.group(1) else ''}{_clean(m.group(2))}"
            add("exposure", v, "exposure.description", v, page, line)

    return {"role": role, "observed_on": observed_on, "items": items}
