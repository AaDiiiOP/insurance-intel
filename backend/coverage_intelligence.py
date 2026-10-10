"""
Phase 1E — Risk → Coverage Intelligence (v0.13.0).

Connects the Business Risk layer (Phase 1D, business_intelligence.py) with
the Policy layer (Phase 1C, policy_intelligence.py). Nothing here extracts,
stores or re-decides a fact: every value comes from those two services, which
use the Knowledge Core's own selection rules (period covering the date wins,
contradictions abstain, facts without evidence are never used).

Four layers are kept apart in every output:

    OBSERVED FACT        values read from documents (with document + page)
    DERIVED RELATIONSHIP a coverage mapping, made by a fixed rule listed below
    POTENTIAL GAP        a finding for a broker to review
    HUMAN JUDGMENT       the broker's review decision (kc_coverage_reviews)

It never says a business is under- or over-insured, that something "is not
covered", or that a condition is breached, and it never recommends buying
insurance. It reports what the documents state, how the stated amounts
compare, and what a person should look at.

Mapping rules (deterministic; no industry inference):
  * Property exposure = the business's assets with a documented declared
    value (Phase 1D), summed as of the date. It maps to a property coverage
    (name contains property / material damage / fire) of a policy in force on
    that date. EXPLICIT_POLICY_LINK when a declared-value document names that
    policy's number (the coverage schedule), else DETERMINISTIC_DOMAIN_RULE.
  * A documented risk or explicit exposure is put into categories by a fixed
    keyword list (CATEGORIES). DIRECT when an in-force coverage's name
    contains the category's term; UNCERTAIN when only a broad property
    coverage could relate to a physical peril; UNMAPPED when nothing does.
  * Exclusions / sub-limits are relevant when they share a category with a
    documented exposure or risk; conditions when they share a control
    category with a documented control (or refer to operations that changed).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date

import business_intelligence as bi
import db
import policy_intelligence as pi
from knowledge import query as kq

# --------------------------------------------------------------------------
# Controlled vocabularies
# --------------------------------------------------------------------------
MAPPING_TYPES = ("DIRECT", "INDIRECT", "PARTIAL", "EXPLICIT_POLICY_LINK", "DETERMINISTIC_DOMAIN_RULE", "UNCERTAIN")
MAPPING_STATUSES = ("MAPPED", "PARTIALLY_MAPPED", "UNMAPPED", "CONFLICTING", "UNCERTAIN", "NOT_APPLICABLE",
                    "NEEDS_REVIEW")
LIMIT_STATES = ("WITHIN_DOCUMENTED_LIMIT", "EXCEEDS_DOCUMENTED_LIMIT", "LIMIT_UNKNOWN", "EXPOSURE_UNKNOWN",
                "NEEDS_REVIEW")
FINDING_TYPES = ("UNMAPPED_RISK", "UNMAPPED_EXPOSURE", "LIMIT_EXCEEDS_EXPOSURE", "EXPOSURE_EXCEEDS_LIMIT",
                 "SUBLIMIT_REVIEW", "EXCLUSION_REVIEW", "CONDITION_REVIEW", "DEDUCTIBLE_REVIEW",
                 "COVERAGE_CONFLICT", "POLICY_PERIOD_CONFLICT", "MISSING_EVIDENCE", "UNCERTAIN_MAPPING")
REVIEW_STATES = ("OPEN", "REVIEWED", "ACCEPTED", "REJECTED", "NEEDS_MORE_EVIDENCE")

CATEGORIES = [
    ("machinery", r"\bmachinery\b|\bbreakdown\b"),
    ("stock", r"\bstock\b|\bgoods\b|\binventory\b"),
    ("buildings", r"\bbuildings?\b|\bcivil works\b"),
    ("fire", r"\bfire\b(?!\s+(protection|alarm|hydrant|extinguisher|safety))|\bexplosion\b"),
    ("flood", r"\bflood(s|ing)?\b|\binundation\b"),
    ("earthquake", r"\bearthquakes?\b"),
    ("storm", r"\bstorms?\b|\bcyclones?\b|\bwindstorms?\b"),
    ("theft", r"\btheft\b|\bburglary\b"),
    ("business_interruption", r"\bbusiness interruption\b|\bloss of profits?\b|\bbusiness income\b"),
]
PHYSICAL = {"machinery", "stock", "buildings", "fire", "flood", "earthquake", "storm", "theft"}
DIRECT_COVERAGE = {   # category -> coverage-name pattern that is a direct match
    "machinery": r"\bmachinery\b",
    "fire": r"\bfire\b",
    "flood": r"\bflood\b",
    "earthquake": r"\bearthquake\b",
    "storm": r"\bstorm\b",
    "theft": r"\btheft\b|\bburglary\b",
    "business_interruption": r"\bbusiness interruption\b|\bloss of profits?\b|\bbusiness income\b",
    "stock": r"\bstock\b",
    "buildings": r"\bbuildings?\b",
}
PROPERTY_COVERAGE = r"\bproperty\b|\bmaterial damage\b|\bfire\b|\ball risks?\b"
CONTROL_CATEGORIES = [
    ("fire_protection", r"\bfire protection\b|\bsprinklers?\b|\bhydrants?\b|\bextinguishers?\b|\bfire alarms?\b"),
    ("security", r"\bsecurity\b|\bcctv\b|\bguard(ing|s)?\b|\balarm system\b"),
    ("backup_power", r"\bbackup power\b|\bgenerators?\b"),
]
_CONFLICT_LABEL = {"policy.coverage_limit": "the policy limit", "policy.coverage": "the coverages",
                   "policy.exclusion": "an exclusion", "policy.endorsement": "an endorsement",
                   "policy.number": "the policy number", "policy.insurer": "the insurer",
                   "policy.condition": "a condition", "policy.sublimit": "a sublimit"}
OPERATIONS_CONDITION = r"\b(occupancy|operations?)\b"

NOTE = ("Intelligence for broker review only: what the documents state and how the stated amounts compare. "
        "Not an underwriting decision, not a statement that anything is or is not insured, and not a "
        "recommendation.")
_NS = uuid.UUID("6f1d3c2a-6e0b-4bfa-9a77-1e5c0b7a9e10")


def _cats(text: str) -> set:
    t = " ".join(str(text or "").lower().split())
    return {c for c, rx in CATEGORIES if re.search(rx, t)}


def _control_cats(text: str) -> set:
    t = " ".join(str(text or "").lower().split())
    return {c for c, rx in CONTROL_CATEGORIES if re.search(rx, t)}


def _num(v):
    return kq._numeric_value(str(v)) if v is not None else None


def _fmt(n) -> str:
    return f"{n:,.0f}" if n is not None else "unknown"


def _ev_of(facts, role):
    out = []
    for f in facts or []:
        for e in f.get("evidence") or []:
            out.append({"role": role, "fact_id": f.get("fact_id"), "value": f.get("value"),
                        "document_id": e.get("document_id"), "document_filename": e.get("document_filename"),
                        "page": e.get("page"), "snippet": e.get("snippet")})
    return out


def _id(org, biz_id, *parts) -> str:
    return str(uuid.uuid5(_NS, "|".join([org, biz_id, *[str(p) for p in parts]])))


def _fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Inputs: business state + in-force policies as of the date
# --------------------------------------------------------------------------

def _policies(conn, org, biz, as_of):
    day = pi._day(as_of)
    in_force, other = [], []
    for p in bi._children(conn, org, biz, "has_policy", "policy"):
        view = pi.get_policy(conn, org, str(p["id"]), as_of=day)
        item = {"policy_id": str(p["id"]), "name": p["name"], "policy_number": view["policy_number"]["answer"],
                "status_on_date": view["period"]["status"], "periods": view["period"]["known"], "view": view}
        (in_force if view["period"]["status"] == "current" else other).append(item)
    return in_force, other


def _doc_text(conn, org, doc_id, cache):
    if doc_id not in cache:
        d = db.get_document_with_text(conn, doc_id, org)
        cache[doc_id] = (d or {}).get("extracted_text") or ""
    return cache[doc_id]


def _policy_linked_assets(conn, org, assets, policy_number, cache) -> bool:
    """A declared-value document (any version) names this policy's number."""
    if not policy_number:
        return False
    for a in assets:
        for f in kq._load_facts(conn, org, [{"id": a["entity_id"]}], "asset.declared_value"):
            for e in f["_evidence"]:
                if policy_number in _doc_text(conn, org, str(e["document_id"]), cache):
                    return True
    return False


# --------------------------------------------------------------------------
# Coverage map
# --------------------------------------------------------------------------

def _in_force_coverages(in_force):
    """Coverages with their own stated limit (coverage entities), plus coverages
    only NAMED in the policy (policy.coverage) — those have no stated limit."""
    out = []
    for p in in_force:
        names = set()
        for c in p["view"]["coverages"]:
            out.append((p, c))
            names.add(c["name"].lower())
        listed = p["view"]["coverages_listed"]
        for f in listed.get("facts_used") or []:
            v = " ".join(f["value"].lower().split())
            # "Property Damage Cover" is the same coverage as "Property Damage".
            if not any(v == n or n in v or v in n for n in names):
                names.add(v)
                out.append((p, {"coverage_id": None, "name": f["value"],
                                "limit": {"answer": None, "values": [], "facts_used": [], "review_status": None},
                                "named_evidence": _ev_of([f], "coverage")}))
    return out


def _exclusions(p):
    v = p["view"]["exclusions"]
    return [{"text": f["value"], "categories": sorted(_cats(f["value"])), "evidence": _ev_of([f], "exclusion"),
             "review_status": f["status"]} for f in (v.get("facts_used") or [])]


def _sublimits(p):
    v = p["view"]["sublimits"]
    return [{"text": f["value"], "categories": sorted(_cats(f["value"])), "amount": f.get("normalized_value"),
             "evidence": _ev_of([f], "sublimit")} for f in (v.get("facts_used") or [])]


def _limit_view(cov):
    lim = cov["limit"]
    return {"answer": lim.get("answer"), "amount": _num(lim.get("answer")),
            "contradiction": lim.get("review_status") == "contradiction",
            "values": lim.get("values") or [],
            "evidence": _ev_of(lim.get("facts_used"), "coverage_limit") + (cov.get("named_evidence") or [])}


def _property_row(conn, org, biz_id, profile, in_force, cache):
    assets = [a for a in profile["assets"]]
    if not assets:
        return None
    components, total, unknown, ev = [], 0.0, False, []
    for a in assets:
        v = a["value"]
        amount = _num(v.get("answer"))
        if amount is None:
            unknown = True
        else:
            total += amount
        e = _ev_of(v.get("facts_used"), "exposure")
        ev += e
        components.append({"asset": a["name"], "categories": sorted(_cats(a["name"])), "value": v.get("answer"),
                           "amount": amount, "observed_on": v.get("observed_on"),
                           "review_status": v.get("review_status"),
                           "conflicting_values": v.get("values") if v.get("review_status") == "contradiction" else [],
                           "evidence": e})
    unit = {"kind": "property_exposure", "key": "property:declared_values",
            "name": "Insured property (documented declared values)",
            "amount": None if unknown else total, "categories": sorted(set().union(*[set(c["categories"]) for c in components])),
            "components": components}
    cands = [(p, c) for p, c in _in_force_coverages(in_force) if re.search(PROPERTY_COVERAGE, c["name"].lower())]
    rows = []
    if not cands:
        reason = ("No policy period in the available documents includes this date."
                  if not in_force else "No property coverage was identified in the available policy documents.")
        rows.append(_row(org, biz_id, unit, None, None, "UNCERTAIN" if in_force else None, "UNMAPPED",
                         reason, ev, [c["name"] for _, c in _in_force_coverages(in_force)]))
        return rows
    for p, c in cands:
        mtype = ("EXPLICIT_POLICY_LINK" if _policy_linked_assets(conn, org, assets, p["policy_number"], cache)
                 else "DETERMINISTIC_DOMAIN_RULE")
        lim = _limit_view(c)
        si = p["view"].get("sum_insured") or {}
        si_conflict = si.get("review_status") == "contradiction"
        if lim["contradiction"] or si_conflict:
            state, stmt = "NEEDS_REVIEW", "The documents state different limits / sums insured; a person must resolve them first."
        elif unit["amount"] is None:
            state, stmt = "EXPOSURE_UNKNOWN", "A declared value is contradictory or missing, so the documented exposure total is not known."
        elif lim["amount"] is None:
            state, stmt = "LIMIT_UNKNOWN", f"No stated limit for {c['name']} was found."
        elif unit["amount"] > lim["amount"]:
            state = "EXCEEDS_DOCUMENTED_LIMIT"
            stmt = (f"The documented exposure amount ({_fmt(unit['amount'])}) is greater than the stated "
                    f"{c['name']} limit ({_fmt(lim['amount'])}).")
        else:
            state = "WITHIN_DOCUMENTED_LIMIT"
            stmt = (f"The documented exposure amount ({_fmt(unit['amount'])}) is within the stated "
                    f"{c['name']} limit ({_fmt(lim['amount'])}).")
        comparison = {"state": state, "exposure_amount": unit["amount"], "limit_amount": lim["amount"],
                      "statement": stmt,
                      "sum_insured_conflict": si.get("values") if si_conflict else [],
                      "limit_conflict": lim["values"] if lim["contradiction"] else []}
        excl = [dict(x, matched=sorted({comp["asset"] for comp in components
                                        if set(comp["categories"]) & set(x["categories"])}))
                for x in _exclusions(p)]
        excl = [x for x in excl if x["matched"]]
        subl = _sublimits(p)
        for s in subl:
            s["matched"] = sorted({comp["asset"] for comp in components if set(comp["categories"]) & set(s["categories"])})
        if state == "NEEDS_REVIEW":
            status = "CONFLICTING"
        elif state in ("EXPOSURE_UNKNOWN", "LIMIT_UNKNOWN"):
            status = "NEEDS_REVIEW"
        elif state == "EXCEEDS_DOCUMENTED_LIMIT" or excl or any(s["matched"] for s in subl):
            status = "PARTIALLY_MAPPED"
        else:
            status = "MAPPED"
        rationale = (f"{c['name']} of policy {p['policy_number'] or p['name']} is the property coverage in force; "
                     + ("the coverage schedule naming this policy lists these declared values."
                        if mtype == "EXPLICIT_POLICY_LINK" else
                        "fixed rule: declared property values relate to the property coverage."))
        r = _row(org, biz_id, unit, p, c, mtype, status, rationale, ev + lim["evidence"],
                 [cc["name"] for _, cc in _in_force_coverages(in_force)])
        r.update(limit_comparison=comparison, exclusions=excl, sublimits=[s for s in subl if s["matched"]],
                 limit=lim)
        rows.append(r)
    return rows


def _row(org, biz_id, unit, p, c, mtype, status, rationale, evidence, candidates):
    key = c["name"] if c else "none"
    return {"mapping_id": _id(org, biz_id, "mapping", unit["key"], p["policy_id"] if p else "none", key),
            "unit": unit,
            "policy": ({"policy_id": p["policy_id"], "policy_number": p["policy_number"], "name": p["name"],
                        "periods": p["periods"]} if p else None),
            "coverage": ({"coverage_id": c["coverage_id"], "name": c["name"]} if c else None),
            "mapping_type": mtype, "status": status, "rationale": rationale,
            "candidates_considered": candidates, "evidence": evidence,
            "limit_comparison": None, "exclusions": [], "sublimits": [], "limit": None,
            "layer": "derived_relationship"}


def _peril_rows(org, biz_id, items, kind, in_force):
    """Risks / explicit exposures -> coverages by category."""
    rows = []
    covs = _in_force_coverages(in_force)
    for it in items:
        text = it["name"]
        cats = _cats(text)
        unit = {"kind": kind, "key": f"{kind}:{text}", "name": text, "amount": None, "categories": sorted(cats),
                "components": []}
        ev = _ev_of((it.get("value") or {}).get("facts_used"), kind)
        names = [c["name"] for _, c in covs]
        if not in_force:
            rows.append(_row(org, biz_id, unit, None, None, None, "UNMAPPED",
                             "No policy period in the available documents includes this date.", ev, names))
            continue
        direct = [(p, c) for p, c in covs for cat in cats
                  if cat in DIRECT_COVERAGE and re.search(DIRECT_COVERAGE[cat], c["name"].lower())]
        if direct:
            for p, c in direct:
                excl = [dict(x, matched=[text]) for x in _exclusions(p) if set(x["categories"]) & cats]
                subl = [dict(x, matched=[text]) for x in _sublimits(p) if set(x["categories"]) & cats]
                r = _row(org, biz_id, unit, p, c, "DIRECT", "PARTIALLY_MAPPED" if excl or subl else "MAPPED",
                         f"The coverage name '{c['name']}' names this {kind}'s category.",
                         ev + _limit_view(c)["evidence"], names)
                r.update(exclusions=excl, sublimits=subl, limit=_limit_view(c))
                rows.append(r)
            continue
        broad = [(p, c) for p, c in covs if cats & PHYSICAL and re.search(PROPERTY_COVERAGE, c["name"].lower())]
        if broad:
            for p, c in broad:
                excl = [dict(x, matched=[text]) for x in _exclusions(p) if set(x["categories"]) & cats]
                subl = [dict(x, matched=[text]) for x in _sublimits(p) if set(x["categories"]) & cats]
                r = _row(org, biz_id, unit, p, c, "UNCERTAIN", "UNCERTAIN",
                         (f"'{c['name']}' may relate to this physical {kind}, but the policy documents do not "
                          f"name it; review required."), ev + _limit_view(c)["evidence"], names)
                r.update(exclusions=excl, sublimits=subl, limit=_limit_view(c))
                rows.append(r)
            continue
        excl = [dict(x, matched=[text]) for p in in_force for x in _exclusions(p) if set(x["categories"]) & cats]
        r = _row(org, biz_id, unit, None, None, None, "UNMAPPED",
                 "No relevant coverage was identified in the available policy documents: no coverage name "
                 "refers to this " + kind + " and no fixed mapping rule applies.", ev, names)
        r["exclusions"] = excl
        rows.append(r)
    return rows


def _business(conn, org, business_id):
    return bi._business(conn, org, business_id)


def get_coverage_map(conn, org: str, business_id: str, as_of=None, _profile=None) -> dict | None:
    biz = _business(conn, org, business_id)
    if biz is None:
        return None
    day = pi._day(as_of)
    # Business state as of the date (never a future-dated observation as "current").
    profile = _profile or bi.get_business_profile(conn, org, business_id, as_of=day)
    in_force, other = _policies(conn, org, biz, day)
    cache: dict = {}
    rows = _property_row(conn, org, business_id, profile, in_force, cache) or []
    rows += _peril_rows(org, business_id, profile["risks"], "risk", in_force)
    rows += _peril_rows(org, business_id, profile["exposures"], "exposure", in_force)
    mapped_cov = {(r["policy"]["policy_id"], r["coverage"]["name"]) for r in rows if r["coverage"]}
    unrelated = [{"policy_number": p["policy_number"], "coverage": c["name"], "limit": c["limit"].get("answer"),
                  "evidence": _limit_view(c)["evidence"]}
                 for p, c in _in_force_coverages(in_force) if (p["policy_id"], c["name"]) not in mapped_cov]
    deductibles, endorsements, conditions = [], [], []
    for p in in_force:
        v = p["view"]
        d = v["deductible"]
        deductibles.append({"policy_number": p["policy_number"], "value": d.get("answer"),
                            "percentage": bool(d.get("answer") and "%" in str(d["answer"])),
                            "applicability": "stated for the policy, not for a named coverage",
                            "review_status": d.get("review_status"), "values": d.get("values") or [],
                            "evidence": _ev_of(d.get("facts_used"), "deductible")})
        comp_cats = {c for r in rows if r["unit"]["kind"] == "property_exposure"
                     for comp in r["unit"]["components"] for c in comp["categories"]}
        for f in (v["endorsements"].get("facts_used") or []):
            endorsements.append({"policy_number": p["policy_number"], "text": f["value"],
                                 "affects_coverage": [c["name"] for c in v["coverages"]
                                                      if c["name"].lower() in f["value"].lower()],
                                 "relates_to_exposure": sorted(_cats(f["value"]) & comp_cats),
                                 "evidence": _ev_of([f], "endorsement")})
        for r in v["endorsement_refs"]:
            endorsements.append({"policy_number": p["policy_number"], "endorsement_id": r["value"],
                                 "effective_from": r.get("effective_from"), "in_effect_on_date": r["in_effect_on_date"],
                                 "evidence": _ev_of([r], "endorsement")})
        for f in (v["conditions"].get("facts_used") or []):
            conditions.append({"policy_number": p["policy_number"], "text": f["value"],
                               "control_categories": sorted(_control_cats(f["value"])),
                               "evidence": _ev_of([f], "condition")})
    return {"business": {"business_id": business_id, "name": biz["name"]}, "as_of": day.isoformat(),
            "policies_in_force": [{k: p[k] for k in ("policy_id", "name", "policy_number", "periods")}
                                  for p in in_force],
            "policies_not_in_force_on_date": [{k: p[k] for k in ("policy_id", "name", "policy_number", "periods",
                                                                 "status_on_date")} for p in other],
            "mappings": rows, "coverages_without_documented_exposure": unrelated,
            "deductibles": deductibles, "endorsements": endorsements, "conditions": conditions,
            "note": NOTE, "_profile": profile, "_in_force": in_force}


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

def _finding(org, biz_id, ftype, key, statement, evidence, *, policy=None, coverage=None, subject=None,
             rationale=None, values=None):
    f = {"finding_id": _id(org, biz_id, "finding", ftype, key), "type": ftype, "key": key,
         "statement": statement, "subject": subject, "policy": policy, "coverage": coverage,
         "rationale": rationale, "evidence": evidence, "layer": "potential_gap",
         "evidence_sufficient": bool(evidence), "needs_review": True,
         "conclusion": "For broker review — not a coverage or underwriting decision."}
    f["fingerprint"] = _fingerprint({"t": ftype, "k": key, "s": statement, "v": values,
                                     "e": sorted((e.get("fact_id") or "") for e in evidence)})
    return f


def _unit_evidence(r, names):
    if r["unit"]["kind"] == "property_exposure":
        return [e for c in r["unit"]["components"] if c["asset"] in names for e in c["evidence"]]
    return [e for e in r["evidence"] if e["role"] in ("risk", "exposure")]


def _pol(r):
    return ({"policy_id": r["policy"]["policy_id"], "policy_number": r["policy"]["policy_number"]}
            if r.get("policy") else None)


def get_coverage_findings(conn, org: str, business_id: str, as_of=None, _map=None) -> dict | None:
    m = _map or get_coverage_map(conn, org, business_id, as_of=as_of)
    if m is None:
        return None
    biz = _business(conn, org, business_id)
    out = []
    for r in m["mappings"]:
        u, cov = r["unit"], (r["coverage"] or {}).get("name")
        lc = r.get("limit_comparison") or {}
        if u["kind"] == "property_exposure":
            if r["status"] == "UNMAPPED":
                out.append(_finding(org, business_id, "UNMAPPED_EXPOSURE", u["key"],
                                    f"{u['name']}: {r['rationale']}", r["evidence"], subject=u["name"],
                                    values=[u["amount"], m["as_of"]]))
            st = lc.get("state")
            if st == "EXCEEDS_DOCUMENTED_LIMIT":
                out.append(_finding(org, business_id, "EXPOSURE_EXCEEDS_LIMIT", f"{u['key']}|{cov}",
                                    f"Potential limit mismatch: {lc['statement']}", r["evidence"],
                                    policy=_pol(r), coverage=cov, subject=u["name"],
                                    rationale="Sum of current documented declared values vs the stated coverage limit.",
                                    values=[lc["exposure_amount"], lc["limit_amount"]]))
            elif st == "WITHIN_DOCUMENTED_LIMIT" and lc["limit_amount"] > lc["exposure_amount"]:
                out.append(_finding(org, business_id, "LIMIT_EXCEEDS_EXPOSURE", f"{u['key']}|{cov}",
                                    (f"The stated {cov} limit ({_fmt(lc['limit_amount'])}) is greater than the "
                                     f"documented exposure amount ({_fmt(lc['exposure_amount'])})."),
                                    r["evidence"], policy=_pol(r), coverage=cov, subject=u["name"],
                                    values=[lc["exposure_amount"], lc["limit_amount"]]))
            elif st in ("LIMIT_UNKNOWN", "EXPOSURE_UNKNOWN"):
                out.append(_finding(org, business_id, "MISSING_EVIDENCE", f"{u['key']}|{cov}|{st}",
                                    lc["statement"], r["evidence"], policy=_pol(r), coverage=cov,
                                    subject=u["name"], values=[st]))
        elif r["status"] == "UNMAPPED":
            ftype = "UNMAPPED_RISK" if u["kind"] == "risk" else "UNMAPPED_EXPOSURE"
            out.append(_finding(org, business_id, ftype, u["key"],
                                f"{u['name']}: {r['rationale']}", r["evidence"], subject=u["name"],
                                rationale=f"Coverages considered: {', '.join(r['candidates_considered']) or 'none in force'}.",
                                values=[m["as_of"] if not r["candidates_considered"] else None,
                                        r["candidates_considered"]]))
        elif r["status"] == "UNCERTAIN":
            out.append(_finding(org, business_id, "UNCERTAIN_MAPPING", f"{u['key']}|{cov}",
                                f"Uncertain mapping — review required: {r['rationale']}", r["evidence"],
                                policy=_pol(r), coverage=cov, subject=u["name"], values=[cov]))
        for x in r.get("exclusions") or []:
            subj = ", ".join(x["matched"])
            out.append(_finding(org, business_id, "EXCLUSION_REVIEW", f"{x['text']}|{subj}",
                                (f"Exclusion identified in the available policy documents that may affect "
                                 f"{subj}: \"{x['text']}\"."),
                                x["evidence"] + _unit_evidence(r, x["matched"]),
                                policy=_pol(r), coverage=cov, subject=subj,
                                rationale=f"Shares the category {', '.join(x['categories'])} with {subj}.",
                                values=[x["text"], subj]))
        for s in r.get("sublimits") or []:
            out.append(_finding(org, business_id, "SUBLIMIT_REVIEW", f"{s['text']}|{','.join(s['matched'])}",
                                f"Relevant sublimit identified: \"{s['text']}\" (may apply to {', '.join(s['matched'])}).",
                                s["evidence"], policy=_pol(r), coverage=cov, subject=", ".join(s["matched"]),
                                values=[s["text"]]))
    # Exclusions shared by two rows are one finding.
    seen, dedup = set(), []
    for f in out:
        if f["finding_id"] not in seen:
            seen.add(f["finding_id"])
            dedup.append(f)
    out = dedup

    # Sublimits whose applicability is not established.
    matched_sub = {f["key"].split("|")[0] for f in out if f["type"] == "SUBLIMIT_REVIEW"}
    for p in m["_in_force"]:
        for s in _sublimits(p):
            if s["text"] not in matched_sub:
                out.append(_finding(org, business_id, "SUBLIMIT_REVIEW", f"{s['text']}|unmatched",
                                    f"Sublimit stated: \"{s['text']}\"; its applicability to the documented "
                                    f"exposures is not established — review.", s["evidence"],
                                    policy={"policy_id": p["policy_id"], "policy_number": p["policy_number"]},
                                    values=[s["text"]]))

    # Conditions vs documented controls / operation changes.
    profile = m["_profile"]
    controls = [(c["name"], c["value"]) for c in profile["controls"]]
    op_changes = []
    if as_of is None:
        ch = bi.get_business_changes(conn, org, business_id) or {}
        op_changes = [c for c in ch.get("changes") or [] if c["key"].startswith("operation:")
                      and c["kind"] in ("added", "changed")]
    for c in m["conditions"]:
        cats = set(c["control_categories"])
        if cats:
            rel = [(n, v) for n, v in controls if _control_cats(n + " " + str(v.get("answer"))) & cats]
            if rel:
                related = "; ".join(f"{n}: {v.get('answer')}" for n, v in rel)
                stmt = (f"Condition \"{c['text']}\" refers to {', '.join(sorted(cats)).replace('_', ' ')}; a related "
                        f"control is documented ({related}). "
                        f"Whether the condition is met is not established by the documents — review. "
                        f"This is not a finding that the condition is breached.")
                ev = c["evidence"] + [e for _, v in rel for e in _ev_of(v.get("facts_used"), "control")]
            else:
                stmt = (f"Condition \"{c['text']}\" refers to {', '.join(sorted(cats)).replace('_', ' ')}; no related "
                        f"control is documented in the available documents. Missing evidence is not a breach — review.")
                ev = c["evidence"]
            out.append(_finding(org, business_id, "CONDITION_REVIEW", c["text"], stmt, ev,
                                policy={"policy_number": c["policy_number"]}, subject=c["text"],
                                values=[[n for n, _ in rel]]))
        elif re.search(OPERATIONS_CONDITION, c["text"].lower()) and op_changes:
            ev = c["evidence"] + [e for oc in op_changes for e in oc.get("new_evidence") or []]
            out.append(_finding(org, business_id, "CONDITION_REVIEW", c["text"],
                                (f"Condition \"{c['text']}\" refers to changes in operations; the documents show "
                                 f"operation changes ({', '.join(oc['key'] for oc in op_changes)}). Whether the "
                                 f"change was notified is not evidenced — review."), ev,
                                policy={"policy_number": c["policy_number"]}, subject=c["text"],
                                values=[oc["key"] for oc in op_changes]))

    # Deductibles: contradictory or percentage-based.
    for d in m["deductibles"]:
        if d["review_status"] == "contradiction":
            out.append(_finding(org, business_id, "DEDUCTIBLE_REVIEW", f"{d['policy_number']}|contradiction",
                                f"Deductible stated differently in the documents ({' vs '.join(d['values'])}); "
                                f"not selected — review.", d["evidence"],
                                policy={"policy_number": d["policy_number"]}, values=d["values"]))
        elif d["percentage"]:
            out.append(_finding(org, business_id, "DEDUCTIBLE_REVIEW", f"{d['policy_number']}|percentage",
                                f"Deductible is percentage-based ({d['value']}); the amount depends on the loss — review.",
                                d["evidence"], policy={"policy_number": d["policy_number"]}, values=[d["value"]]))

    # Conflicts from the existing contradiction framework (never resolved here).
    for p in bi._children(conn, org, biz, "has_policy", "policy"):
        pnum = pi._view(conn, org, [p], "policy.number", pi._day(as_of))["answer"]
        for c in pi.find_policy_conflicts(conn, org, str(p["id"])) or []:
            if c["fact_type"] == "policy.deductible":
                continue        # reported once, as DEDUCTIBLE_REVIEW
            label = (f"the {c['coverage']} limit" if c["coverage"] else
                     _CONFLICT_LABEL.get(c["fact_type"], c["fact_type"]))
            out.append(_finding(org, business_id, "COVERAGE_CONFLICT", f"{p['id']}|{c['fact_type']}|{c['coverage']}",
                                f"Documents disagree on {label}: {' vs '.join(c['values'])}. Not resolved — review.",
                                _ev_of(c["facts"], "conflict"), policy={"policy_id": str(p["id"]), "policy_number": pnum},
                                coverage=c["coverage"], values=c["values"]))
        periods = pi._periods(conn, org, [p])
        for i, a in enumerate(periods):
            for b in periods[i + 1:]:
                if a != b and a[0] <= b[1] and b[0] <= a[1]:
                    out.append(_finding(org, business_id, "POLICY_PERIOD_CONFLICT", f"{p['id']}|{a}|{b}",
                                        f"Overlapping stated periods for policy {pnum}: {kq._iso(a[0])}–{kq._iso(a[1])} "
                                        f"and {kq._iso(b[0])}–{kq._iso(b[1])} — review.", [],
                                        policy={"policy_id": str(p["id"]), "policy_number": pnum},
                                        values=[str(a), str(b)]))
    for p in m["_in_force"]:
        si = p["view"].get("sum_insured") or {}
        if si.get("review_status") == "contradiction":
            out.append(_finding(org, business_id, "COVERAGE_CONFLICT", f"{p['policy_id']}|property.sum_insured",
                                f"Documents disagree on the property sum insured: {' vs '.join(si.get('values') or [])}. "
                                f"Not resolved — review.", _ev_of(si.get("facts_used"), "conflict"),
                                policy={"policy_id": p["policy_id"], "policy_number": p["policy_number"]},
                                values=si.get("values")))

    # A finding without evidence is never authoritative.
    for f in out:
        if not f["evidence"]:
            f["evidence_sufficient"] = False
            f["certainty"] = "UNCERTAIN"
    _attach_reviews(conn, org, business_id, out, "finding")
    by_type = {}
    for f in out:
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    return {"business": m["business"], "as_of": m["as_of"], "findings": out, "counts": by_type, "note": NOTE}


# --------------------------------------------------------------------------
# Human review
# --------------------------------------------------------------------------

def _attach_reviews(conn, org, business_id, items, subject_type):
    rows = [r for r in db.list_coverage_reviews(conn, org, business_id) if r["subject_type"] == subject_type]
    for it in items:
        sid = it["finding_id"] if subject_type == "finding" else it["mapping_id"]
        fp = it["fingerprint"] if subject_type == "finding" else _mapping_fingerprint(it)
        mine = [r for r in rows if str(r["subject_id"]) == sid]
        current = [r for r in mine if r["fingerprint"] == fp]
        last = current[-1] if current else None
        it["review_status"] = last["decision"] if last else "OPEN"
        it["review"] = ({"decision": last["decision"], "reviewer": last["reviewer"], "comment": last["comment"],
                         "at": kq._iso(last["created_at"])} if last else None)
        it["review_stale"] = bool(mine) and not current
        it["review_history"] = [{"decision": r["decision"], "reviewer": r["reviewer"], "comment": r["comment"],
                                 "at": kq._iso(r["created_at"]), "applies_to_current_evidence": r["fingerprint"] == fp}
                                for r in mine]


def _mapping_fingerprint(r):
    return _fingerprint({"s": r["status"], "t": r["mapping_type"], "lc": (r.get("limit_comparison") or {}).get("state"),
                         "a": r["unit"]["amount"], "e": sorted((e.get("fact_id") or "") for e in r["evidence"])})


def review(conn, org: str, business_id: str, subject_type: str, subject_id: str, decision: str,
           reviewer: str, comment: str | None = None) -> dict | None:
    """Store a broker decision on a CURRENT mapping/finding. None if the
    business or the subject does not exist in this organization."""
    if decision not in REVIEW_STATES or subject_type not in ("mapping", "finding") or not (reviewer or "").strip():
        raise ValueError("decision must be one of " + ", ".join(REVIEW_STATES) + "; reviewer is required.")
    m = get_coverage_map(conn, org, business_id)
    if m is None:
        return None
    if subject_type == "finding":
        items = get_coverage_findings(conn, org, business_id, _map=m)["findings"]
        it = next((f for f in items if f["finding_id"] == subject_id), None)
        fp = it and it["fingerprint"]
    else:
        it = next((r for r in m["mappings"] if r["mapping_id"] == subject_id), None)
        fp = it and _mapping_fingerprint(it)
    if it is None:
        return None
    row = db.add_coverage_review(conn, org, business_id, subject_type, subject_id, fp, decision,
                                 reviewer.strip(), comment, it["evidence"])
    return {"subject_type": subject_type, "subject_id": subject_id, "decision": row["decision"],
            "reviewer": row["reviewer"], "comment": row["comment"], "at": kq._iso(row["created_at"]),
            "evidence": it["evidence"]}


# --------------------------------------------------------------------------
# Public views
# --------------------------------------------------------------------------

def public_map(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


def coverage_map_view(conn, org, business_id, as_of=None):
    m = get_coverage_map(conn, org, business_id, as_of=as_of)
    if m is None:
        return None
    _attach_reviews(conn, org, business_id, m["mappings"], "mapping")
    return public_map(m)


def unmapped_risks(conn, org, business_id, as_of=None):
    m = get_coverage_map(conn, org, business_id, as_of=as_of)
    if m is None:
        return None
    rows = [r for r in m["mappings"] if r["unit"]["kind"] in ("risk", "exposure")
            and r["status"] in ("UNMAPPED", "UNCERTAIN")]
    risks = m["_profile"]["risks"]
    return {"as_of": m["as_of"], "documented_risks": len(risks), "unmapped_or_uncertain": rows,
            "note": ("No risk is documented in the available documents." if not risks else NOTE)}


def coverage_conflicts(conn, org, business_id, as_of=None):
    f = get_coverage_findings(conn, org, business_id, as_of=as_of)
    if f is None:
        return None
    m = get_coverage_map(conn, org, business_id, as_of=as_of)
    limit_conf = [r for r in m["mappings"] if r["status"] == "CONFLICTING"]
    return {"as_of": f["as_of"],
            "conflicts": [x for x in f["findings"] if x["type"] in ("COVERAGE_CONFLICT", "POLICY_PERIOD_CONFLICT")
                          or (x["type"] == "DEDUCTIBLE_REVIEW" and x["key"].endswith("|contradiction"))],
            "conflicting_mappings": limit_conf}


def _change_dates(conn, org, biz):
    ds = set(bi._observation_dates(conn, org, biz))
    for p in bi._children(conn, org, biz, "has_policy", "policy"):
        for a, _b in pi._periods(conn, org, [p]):
            ds.add(a)
        for f in kq._load_facts(conn, org, [p], "policy.endorsement_ref"):
            if f.get("effective_from"):
                ds.add(f["effective_from"])
    return sorted(d for d in ds if d <= date.today())


def _row_key(r):
    return (r["unit"]["key"], (r["policy"] or {}).get("policy_number"), (r["coverage"] or {}).get("name"))


def get_coverage_changes(conn, org, business_id, from_date=None, to_date=None) -> dict | None:
    """Coverage state as of A vs as of B, plus policy-period changes with the
    related documented exposure. Never reports 'removed': a mapping not present
    on the later date is 'not_in_effect_on_later_date'."""
    biz = _business(conn, org, business_id)
    if biz is None:
        return None
    dates = _change_dates(conn, org, biz)
    if from_date is None and to_date is None:
        if len(dates) < 2:
            return {"status": "insufficient", "changes": [], "policy_changes": [],
                    "reason": "Fewer than two dated states are recorded; nothing to compare."}
        a, b = dates[-2], dates[-1]
    else:
        b = pi._day(to_date)
        a = pi._day(from_date) if from_date else next((d for d in reversed(dates) if d < b), None)
        if a is None:
            return {"status": "insufficient", "changes": [], "policy_changes": [],
                    "reason": "No earlier dated state exists before the later date."}
    ma, mb = get_coverage_map(conn, org, business_id, as_of=a), get_coverage_map(conn, org, business_id, as_of=b)
    ra, rb = {_row_key(r): r for r in ma["mappings"]}, {_row_key(r): r for r in mb["mappings"]}
    changes = []
    for k in sorted(set(ra) | set(rb), key=str):
        x, y = ra.get(k), rb.get(k)
        name = (y or x)["unit"]["name"]
        cov = k[2]
        if x is None:
            changes.append({"kind": "newly_mapped", "unit": name, "coverage": cov, "new_status": y["status"],
                            "new_limit_state": (y.get("limit_comparison") or {}).get("state"),
                            "evidence": y["evidence"]})
            continue
        if y is None:
            changes.append({"kind": "not_in_effect_on_later_date", "unit": name, "coverage": cov,
                            "old_status": x["status"], "note": "not reported as removed",
                            "evidence": x["evidence"]})
            continue
        sa, sb = (x.get("limit_comparison") or {}), (y.get("limit_comparison") or {})
        kind = ("status_changed" if x["status"] != y["status"] or sa.get("state") != sb.get("state")
                else "unchanged")
        changes.append({"kind": kind, "unit": name, "coverage": cov, "old_status": x["status"],
                        "new_status": y["status"], "old_limit_state": sa.get("state"), "new_limit_state": sb.get("state"),
                        "old_exposure": sa.get("exposure_amount"), "new_exposure": sb.get("exposure_amount"),
                        "old_limit": sa.get("limit_amount"), "new_limit": sb.get("limit_amount"),
                        "statement": sb.get("statement"),
                        "evidence": x["evidence"] + y["evidence"]})
    prop = next((r for r in mb["mappings"] if r["unit"]["kind"] == "property_exposure"), None)
    policy_changes = []
    for p in bi._children(conn, org, biz, "has_policy", "policy"):
        cmp = pi.compare_policies(conn, org, str(p["id"]), as_of=b)
        if not cmp or cmp["status"] != "compared":
            policy_changes.append({"policy": p["name"], "status": "insufficient",
                                   "reason": (cmp or {}).get("reason")})
            continue
        for c in cmp["changes"]:
            if c["change_type"].endswith("_unchanged"):
                continue
            item = {"policy": p["name"], **{k: c.get(k) for k in ("change_type", "coverage", "old_value", "new_value",
                                                                  "percentage_change", "old_values", "new_values")},
                    "evidence": (c.get("old_evidence") or []) + (c.get("new_evidence") or [])}
            if "limit" in c["change_type"] and prop and prop["unit"]["amount"] is not None:
                item.update(related_exposure=prop["unit"]["amount"], status="NEEDS_REVIEW",
                            impact=(f"Limit changed from {c.get('old_value')} to {c.get('new_value')}; related "
                                    f"documented exposure {_fmt(prop['unit']['amount'])}."))
            policy_changes.append(item)
    return {"status": "compared", "from": a.isoformat(), "to": b.isoformat(), "changes": changes,
            "policy_changes": policy_changes,
            "summary": {"status_changed": sum(c["kind"] == "status_changed" for c in changes),
                        "newly_mapped": sum(c["kind"] == "newly_mapped" for c in changes),
                        "not_in_effect_on_later_date": sum(c["kind"] == "not_in_effect_on_later_date" for c in changes),
                        "unchanged": sum(c["kind"] == "unchanged" for c in changes),
                        "policy_changes": sum(1 for c in policy_changes if c.get("change_type"))},
            "note": NOTE}


def get_coverage_audit(conn, org, business_id, as_of=None) -> dict | None:
    m = get_coverage_map(conn, org, business_id, as_of=as_of)
    if m is None:
        return None
    f = get_coverage_findings(conn, org, business_id, as_of=as_of, _map=m)
    _attach_reviews(conn, org, business_id, m["mappings"], "mapping")
    p = m["_profile"]
    ch = get_coverage_changes(conn, org, business_id) if as_of is None else None
    review_counts = {}
    for x in f["findings"]:
        review_counts[x["review_status"]] = review_counts.get(x["review_status"], 0) + 1
    return {"business": m["business"], "as_of": m["as_of"],
            "risks": p["risks"], "exposures": p["exposures"], "controls": p["controls"],
            "coverage_map": public_map(m),
            "mapped": [r for r in m["mappings"] if r["status"] in ("MAPPED", "PARTIALLY_MAPPED")],
            "unmapped": [r for r in m["mappings"] if r["status"] in ("UNMAPPED", "UNCERTAIN")],
            "limit_comparisons": [dict(r["limit_comparison"], coverage=r["coverage"]["name"],
                                       policy_number=r["policy"]["policy_number"], evidence=r["evidence"])
                                  for r in m["mappings"] if r.get("limit_comparison")],
            "findings": f["findings"], "finding_counts": f["counts"], "review_counts": review_counts,
            "changes": ch, "note": NOTE}


def get_coverage_graph(conn, org, business_id, as_of=None) -> dict | None:
    """The Phase 1D business graph plus DERIVED coverage edges (marked derived,
    with rule, status and evidence). Nothing derived is stored as a fact."""
    g = bi.get_business_graph(conn, org, business_id)
    if g is None:
        return None
    m = get_coverage_map(conn, org, business_id, as_of=as_of)
    f = get_coverage_findings(conn, org, business_id, as_of=as_of, _map=m)
    nodes = {n["id"]: dict(n, derived=False) for n in g["nodes"]}
    edges = [dict(e, derived=False) for e in g["edges"]]
    labels = {(n["type"], n["label"].lower()): n["id"] for n in g["nodes"]}

    def node(nid, ntype, label, ev=None):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "type": ntype, "label": label, "facts": [], "derived": True,
                          "evidence": ev or []}
        return nid

    for r in m["mappings"]:
        if not r["coverage"]:
            continue
        cov_id = r["coverage"]["coverage_id"] or _id(org, business_id, "coverage", r["coverage"]["name"])
        node(cov_id, "coverage", r["coverage"]["name"])
        if r["unit"]["kind"] == "property_exposure":
            srcs = [labels.get(("asset", c["asset"].lower())) for c in r["unit"]["components"]]
        else:
            srcs = [labels.get((r["unit"]["kind"], r["unit"]["name"].lower()))]
        for s in [s for s in srcs if s]:
            edges.append({"from": s, "to": cov_id, "type": "mapped_to_coverage", "derived": True,
                          "mapping_type": r["mapping_type"], "status": r["status"], "evidence": r["evidence"]})
        for x in r.get("exclusions") or []:
            xid = node(_id(org, business_id, "exclusion", x["text"]), "exclusion", x["text"], x["evidence"])
            for s in [s for s in srcs if s]:
                edges.append({"from": s, "to": xid, "type": "potentially_affected_by_exclusion", "derived": True,
                              "status": "NEEDS_REVIEW", "evidence": x["evidence"]})
        if r.get("limit") and r["limit"]["answer"]:
            lid = node(_id(org, business_id, "limit", cov_id), "coverage_limit", r["limit"]["answer"],
                       r["limit"]["evidence"])
            edges.append({"from": cov_id, "to": lid, "type": "has_limit", "derived": True,
                          "status": (r.get("limit_comparison") or {}).get("state"), "evidence": r["limit"]["evidence"]})
    for x in f["findings"]:
        if x["type"] == "CONDITION_REVIEW":
            cid = node(_id(org, business_id, "condition", x["key"]), "condition", x["key"], x["evidence"])
            for n in g["nodes"]:
                if n["type"] == "control" and _control_cats(n["label"]) & _control_cats(x["key"]):
                    edges.append({"from": cid, "to": n["id"], "type": "condition_refers_to_control", "derived": True,
                                  "status": "NEEDS_REVIEW", "evidence": x["evidence"]})
    return {"business": m["business"], "as_of": m["as_of"], "nodes": list(nodes.values()), "edges": edges,
            "note": "Edges with derived=true are coverage mappings made by fixed rules, not facts read from a document."}


# --------------------------------------------------------------------------
# Questions (called from knowledge/query.py for the cov_* intents)
# --------------------------------------------------------------------------
COVERAGE_INTENTS = [
    # order matters: first match wins
    ("cov_advice", r"\b(adequately|sufficiently|properly|fully|enough) (insured|covered)\b|\bunder ?insured\b|"
                   r"\bover ?insured\b|\benough (insurance|cover|coverage)\b|"
                   r"\b(buy|purchase|get|take out|add) (more|additional|extra) (insurance|cover|coverage)\b|"
                   r"\bshould (the |this )?(company|business|insured|client) (buy|purchase|increase|take|get)\b|"
                   r"\binsurance (adequate|sufficient|enough)\b|\b(adequate|sufficient) (insurance|cover|coverage)\b"),
    ("cov_conditions", r"\bconditions?\b.*\b(review|affect\w*|met|breach\w*|satisf\w*)\b"),
    ("cov_exclusions", r"\bexclusions?\b.*\b(affect\w*|apply to|relevant to|impact\w*)\b.*"
                       r"\b(risks?|exposures?|assets?|property|stock|machinery|business)\b"),
    ("cov_limits", r"\b(exceed|exceeds|exceeding|above|greater than|more than)\b.*\blimits?\b|\blimit mismatch\w*\b|"
                   r"\bexposures?\b.*\blimits?\b"),
    ("cov_unmapped", r"\bunmapped\b|\bno (clearly )?mapped coverage\b|\bnot (clearly )?mapped\b|"
                     r"\bwithout (any |relevant |mapped )?coverage\b|\bno relevant coverage\b"),
    ("cov_status_changes", r"\bcoverage status\b.*\bchang|\bchang\w*\b.*\bcoverage status\b|\bcoverage mappings? chang"),
    ("cov_conflicts", r"\bcoverage conflicts?\b|\bconflicts? (in|between|across) (the )?(coverages?|policies|policy documents)\b"),
    ("cov_findings", r"\bfindings?\b|\bbroker review\b"),
    ("cov_state", r"\bcoverage (state|position|picture)\b"),
    ("cov_mapped", r"\b(relevant|mapped|related) coverage\b|\bcoverage mappings?\b|\bmapped to coverage\b|"
                   r"\bwhich polic(y|ies) (protect|cover|respond|apply)\w*\b|\bprotects? (this|the) (business )?exposure"),
]


def coverage_intent_of(q: str):
    for name, rx in COVERAGE_INTENTS:
        if re.search(rx, q):
            return name
    return None


def _row_line(r):
    cov = r["coverage"]["name"] if r["coverage"] else "no coverage identified"
    pol = f" (policy {r['policy']['policy_number']})" if r.get("policy") and r["policy"].get("policy_number") else ""
    line = f"{r['unit']['name']} → {cov}{pol}: {r['status']}"
    if r["mapping_type"]:
        line += f", {r['mapping_type']}"
    lc = r.get("limit_comparison")
    if lc:
        line += f". {lc['statement']}"
    elif r["status"] in ("UNMAPPED", "UNCERTAIN"):
        line += f". {r['rationale']}"
    return line


def answer(conn, org, result, biz, intent, question, day, mode) -> dict:
    as_of = day if mode == "as_of" else None
    bid = str(biz["id"])
    q = " ".join(str(question).lower().split())
    result["answer_type"] = "deterministic"
    result["authoritative"] = False
    result["needs_review"] = True
    result["review_status"] = "needs_review"
    lines, ev = [], []

    def done(reason_if_empty):
        if not lines:
            result["answer"] = None
            result["reason"] = reason_if_empty
        else:
            result["answer"] = lines
            result["values"] = lines
            result["reason"] = NOTE
        result["evidence"] = ev
        result["source_documents"] = sorted({e["document_filename"] for e in ev if e.get("document_filename")})
        result["source_pages"] = [{"document": d, "page": p} for d, p in
                                  sorted({(e.get("document_filename"), e.get("page")) for e in ev},
                                         key=lambda x: (str(x[0]), x[1] or 0))]
        return result

    if intent == "cov_status_changes":
        ch = get_coverage_changes(conn, org, bid)
        result["change"] = ch
        if ch["status"] != "compared":
            return done(ch["reason"])
        for c in ch["changes"]:
            if c["kind"] == "status_changed":
                lines.append(f"{c['unit']} → {c['coverage']}: {c['old_status']} → {c['new_status']}"
                             + (f" ({c['old_limit_state']} → {c['new_limit_state']})"
                                if c["old_limit_state"] != c["new_limit_state"] else "")
                             + f" between {ch['from']} and {ch['to']}.")
                ev += c["evidence"]
            elif c["kind"] in ("newly_mapped", "not_in_effect_on_later_date"):
                lines.append(f"{c['unit']} → {c['coverage']}: {c['kind'].replace('_', ' ')} "
                             f"between {ch['from']} and {ch['to']}.")
                ev += c["evidence"]
        if not lines:
            lines.append(f"No coverage status changed between {ch['from']} and {ch['to']}.")
        return done("")

    m = get_coverage_map(conn, org, bid, as_of=as_of)
    rows = m["mappings"]
    risks = m["_profile"]["risks"]
    want = ("risk" if re.search(r"\brisks?\b", q) else "exposure" if re.search(r"\bexposures?\b", q) else None)

    if intent == "cov_advice":
        f = get_coverage_findings(conn, org, bid, as_of=as_of, _map=m)
        for x in f["findings"]:
            if x["type"] in ("EXPOSURE_EXCEEDS_LIMIT", "UNMAPPED_RISK", "UNMAPPED_EXPOSURE", "EXCLUSION_REVIEW",
                             "COVERAGE_CONFLICT", "UNCERTAIN_MAPPING", "MISSING_EVIDENCE"):
                lines.append(f"{x['type']}: {x['statement']}")
                ev += x["evidence"]
        result["answer"] = None
        result["answer_type"] = "declined_judgment"
        result["reason"] = ("Aurevia does not decide whether a business is adequately insured, under-insured or "
                            "should buy more insurance — that is a broker's judgment. "
                            + ("Findings for review: " + " | ".join(lines) if lines else
                               "No potential limit mismatch, unmapped risk or coverage conflict was identified in "
                               "the available documents; that is not a statement that cover is adequate."))
        result["values"] = lines
        result["evidence"] = ev
        return result

    if intent in ("cov_mapped", "cov_unmapped"):
        keep = ("MAPPED", "PARTIALLY_MAPPED", "CONFLICTING", "NEEDS_REVIEW") if intent == "cov_mapped" \
            else ("UNMAPPED", "UNCERTAIN")
        sel = [r for r in rows if r["status"] in keep and
               (want is None or r["unit"]["kind"] == want or (want == "exposure" and r["unit"]["kind"] == "property_exposure"))]
        for r in sel:
            lines.append(_row_line(r))
            ev += r["evidence"]
        if want == "risk" and not risks:
            return done("No risk is documented in the available documents, so there is nothing to map.")
        if not m["policies_in_force"]:
            return done(f"No policy period in the available documents includes {m['as_of']}.")
        return done("No documented " + (want or "risk or exposure") +
                    (" has a coverage mapping." if intent == "cov_mapped" else " is unmapped or uncertain."))

    if intent == "cov_limits" or intent == "cov_state":
        if intent == "cov_state":
            if m["policies_in_force"]:
                lines.append("Policies in force on " + m["as_of"] + ": " +
                             ", ".join(p["policy_number"] or p["name"] for p in m["policies_in_force"]) + ".")
            for r in rows:
                lines.append(_row_line(r))
                ev += r["evidence"]
            return done(f"No policy period in the available documents includes {m['as_of']}, and no documented "
                        f"exposure or risk was observed by then.")
        for r in rows:
            if r.get("limit_comparison"):
                lc = r["limit_comparison"]
                lines.append(f"{lc['state']}: {lc['statement']}")
                ev += r["evidence"]
        return done("No documented exposure amount could be compared with a stated coverage limit.")

    f = get_coverage_findings(conn, org, bid, as_of=as_of, _map=m)
    types = {"cov_exclusions": ("EXCLUSION_REVIEW",), "cov_conditions": ("CONDITION_REVIEW",),
             "cov_conflicts": ("COVERAGE_CONFLICT", "POLICY_PERIOD_CONFLICT", "DEDUCTIBLE_REVIEW"),
             "cov_findings": FINDING_TYPES}[intent]
    for x in f["findings"]:
        if x["type"] not in types:
            continue
        if intent == "cov_conflicts" and x["type"] == "DEDUCTIBLE_REVIEW" and not x["key"].endswith("|contradiction"):
            continue
        if intent == "cov_findings" and x["review_status"] not in ("OPEN", "NEEDS_MORE_EVIDENCE"):
            continue
        lines.append(f"{x['type']}: {x['statement']}" + (f" [review: {x['review_status']}]" if intent == "cov_findings" else ""))
        ev += x["evidence"]
    empty = {"cov_exclusions": "No exclusion in the available policy documents was matched to a documented risk or exposure.",
             "cov_conditions": "No policy condition was matched to a documented control or change.",
             "cov_conflicts": "No unresolved coverage conflict in the available documents.",
             "cov_findings": "No open coverage finding."}[intent]
    return done(empty)
