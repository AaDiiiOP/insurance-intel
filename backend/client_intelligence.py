"""
Phase 2B — Unified client intelligence (v0.15.0).

One read-only summary of everything the existing engines already know about a
client, so a broker can see it at a glance from the client page:

    client ─ business_entity_id ─> business (Phase 1D)
                                    ├─ profile, changes, conflicts      business_intelligence
                                    ├─ has_policy ─> policies           policy_intelligence (Phase 1C)
                                    └─ coverage audit / findings        coverage_intelligence (Phase 1E)
    client ─ submissions / client_documents ─> document processing status

Nothing here reasons about insurance. Every number below is a COUNT of what an
existing engine returned; every value shown keeps the engine's own evidence
(document, page, snippet) and review status. This module adds no extraction,
no mapping, no temporal logic, and stores nothing.

Rules carried through:
- The business comes only from the client row in the caller's organization;
  every engine call is scoped to that organization.
- Each section reports its own status: available / not_linked / not_analyzed
  / unavailable (with the error). One engine failing never hides the others
  (each section runs in its own savepoint).
- Absence is never turned into a statement: a missing value is listed as
  "not found in the documents", "not re-observed" stays "not re-observed",
  conflicting values are all listed.
"""
from __future__ import annotations

import logging
from datetime import date

import business_intelligence as bi
import coverage_intelligence as ci
import db
import policy_intelligence as pi

log = logging.getLogger(__name__)

NOTE = ("A summary of what the documents say and what the system found, for review by an authorized "
        "broker. Findings are not an underwriting decision and not advice; nothing here states that the "
        "client is under- or over-insured.")

PROFILE_SECTIONS = ("locations", "operations", "assets", "controls", "claims", "risk_factors", "risks",
                    "exposures")
PROFILE_FIELDS = ("legal_name", "workforce", "survey_risk_rating")
POLICY_FIELDS = ("policy_number", "insurer", "policy_limit", "sum_insured", "deductible", "sublimits",
                 "exclusions", "conditions", "endorsements")

# How a fact was produced (kc_facts.confidence_type) -> plain words for brokers.
SOURCE_LABELS = {
    "deterministic": "read by rule from the document",
    "model_self_reported": "AI-extracted, not yet verified",
    "ocr_engine": "read by OCR",
}


class SectionUnavailable(Exception):
    pass


def _section(conn, name, fn):
    """Run one engine call in its own savepoint; a failure becomes an
    'unavailable' section instead of breaking the whole response."""
    try:
        with conn.transaction():
            return fn()
    except SectionUnavailable as e:
        return {"status": "unavailable", "error": str(e)}
    except Exception as e:  # noqa: BLE001 — isolate one engine's failure
        log.exception("client intelligence section %s failed", name)
        return {"status": "unavailable", "error": f"{name} could not be loaded ({type(e).__name__})."}


def _walk_facts(obj, out: dict):
    """Collect every fact an engine used (deduplicated by fact id)."""
    if isinstance(obj, dict):
        for f in obj.get("facts_used") or []:
            if isinstance(f, dict) and f.get("fact_id"):
                out[str(f["fact_id"])] = f
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_facts(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_facts(v, out)


def _fact_provenance(facts: dict) -> dict:
    """Counts of the facts behind what is shown: review state, how each was
    produced, and how many carry no evidence at all."""
    by_status, by_source, no_evidence = {}, {}, 0
    for f in facts.values():
        st = f.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        ctype = (f.get("confidence") or {}).get("type") if isinstance(f.get("confidence"), dict) else None
        src = SOURCE_LABELS.get(ctype or "", "source not recorded")
        by_source[src] = by_source.get(src, 0) + 1
        if not f.get("evidence"):
            no_evidence += 1
    return {"facts": len(facts), "by_status": by_status, "by_source": by_source,
            "without_evidence": no_evidence}


def _first_evidence(view: dict | None) -> list:
    """Evidence references (document, page, snippet) behind a value view."""
    refs = []
    for f in (view or {}).get("facts_used") or []:
        for e in f.get("evidence") or []:
            refs.append({"document_id": str(e.get("document_id")) if e.get("document_id") else None,
                         "document_filename": e.get("document_filename"),
                         "page": e.get("page_number", e.get("page")),
                         "snippet": e.get("snippet") or e.get("text")})
    return refs[:3]


def _value(view: dict | None) -> dict:
    """A compact, honest rendering of one value view."""
    view = view or {}
    ans = view.get("answer")
    contradictions = view.get("contradictions") or []
    if contradictions or view.get("review_status") == "contradiction":
        state = "conflicting"
    elif ans is None:
        state = "missing"
    elif view.get("review_status") in ("verified", "deterministic"):
        state = "verified" if view.get("review_status") == "verified" else "read_by_rule"
    else:
        state = "unverified"
    return {"answer": ans, "state": state,
            "values": view.get("values") or ([] if ans is None else [ans]),
            "review_status": view.get("review_status"),
            "observed_on": view.get("observed_on"),
            "reason": view.get("reason") if ans is None else None,
            "evidence": _first_evidence(view)}


def _documents(conn, org, client_id):
    paperwork = db.list_client_documents(conn, client_id, org)
    intel, errors, by_status = [], [], {}
    for s in db.list_client_submissions(conn, client_id, org):
        for d in db.list_submission_documents(conn, str(s["id"]), org):
            st = d.get("status") or "unknown"
            by_status[st] = by_status.get(st, 0) + 1
            intel.append(d)
            if d.get("error_message"):
                errors.append({"document_id": str(d["id"]), "filename": d["filename"],
                               "status": st, "error": d["error_message"]})
    verdicts = {}
    for d in paperwork:
        verdicts[d["verdict"]] = verdicts.get(d["verdict"], 0) + 1
    return {"status": "available", "intelligence_documents": len(intel), "by_status": by_status,
            "paperwork_documents": len(paperwork), "paperwork_verdicts": verdicts, "errors": errors}


def _profile(conn, org, business_id, day):
    p = bi.get_business_profile(conn, org, business_id, as_of=day)
    if p is None:
        raise SectionUnavailable("The linked business was not found in this organization.")
    facts: dict = {}
    _walk_facts(p, facts)
    counts = {k: len(p.get(k) or []) for k in PROFILE_SECTIONS}
    analyzed = any(counts.values()) or any((p.get(k) or {}).get("answer") for k in PROFILE_FIELDS)
    return {"status": "available" if analyzed else "not_analyzed",
            "as_of": p.get("as_of"), "mode": p.get("mode"),
            "fields": {k: _value(p.get(k)) for k in PROFILE_FIELDS},
            "counts": counts,
            "missing_sections": [k for k, n in counts.items() if n == 0],
            "provenance": _fact_provenance(facts),
            "_policies": p.get("policies") or []}


def _policies(conn, org, policy_refs, day):
    items, facts = [], {}
    for ref in policy_refs:
        v = pi.get_policy(conn, org, ref["policy_id"], as_of=day)
        if v is None:
            continue
        _walk_facts(v, facts)
        conflicts = pi.find_policy_conflicts(conn, org, ref["policy_id"]) or []
        period = v.get("period") or {}
        items.append({
            "policy_id": ref["policy_id"], "name": ref.get("name"),
            "fields": {k: _value(v.get(k)) for k in POLICY_FIELDS},
            "period_status": period.get("status"),  # current / expired / not_started / unknown
            "periods": period.get("known") or [],
            "coverages": [c.get("name") for c in v.get("coverages") or []],
            "endorsement_documents": len(v.get("endorsement_refs") or []),
            "conflicts": len(conflicts),
        })
    return {"status": "available" if items else "none_found", "items": items,
            "in_force": sum(1 for i in items if i["period_status"] == "current"),
            "provenance": _fact_provenance(facts)}


def _coverage(conn, org, business_id, day):
    a = ci.get_coverage_audit(conn, org, business_id, as_of=day)
    if a is None:
        raise SectionUnavailable("Coverage review could not find the linked business.")
    mappings = (a.get("coverage_map") or {}).get("mappings") or []
    status_counts = {}
    for m in mappings:
        status_counts[m["status"]] = status_counts.get(m["status"], 0) + 1
    open_findings = [f for f in a.get("findings") or [] if f.get("review_status") in (None, "OPEN")
                     or f.get("review_stale")]
    return {"status": "available" if (mappings or a.get("findings")) else "not_analyzed",
            "mapping_status_counts": status_counts,
            "finding_counts": a.get("finding_counts") or {},
            "review_counts": a.get("review_counts") or {},
            "open_findings": [{"finding_id": f["finding_id"], "type": f["type"], "statement": f["statement"],
                               "review_status": f.get("review_status"), "review_stale": f.get("review_stale"),
                               "evidence_sufficient": f.get("evidence_sufficient"),
                               "evidence": (f.get("evidence") or [])[:3]} for f in open_findings][:25],
            "limit_comparisons": len(a.get("limit_comparisons") or []),
            "note": a.get("note")}


def _conflicts(conn, org, business_id, policy_ids):
    biz = bi.find_business_conflicts(conn, org, business_id) or []
    pol = []
    for pid in policy_ids:
        for c in pi.find_policy_conflicts(conn, org, pid) or []:
            pol.append(dict(c, policy_id=pid))
    def slim(c):
        return {"key": c.get("key") or c.get("fact_type"), "fact_type": c.get("fact_type"),
                "values": c.get("values") or [], "policy_id": c.get("policy_id"),
                "evidence": [e for f in c.get("facts") or [] for e in (f.get("evidence") or [])][:4]}
    return {"status": "available", "business": [slim(c) for c in biz], "policies": [slim(c) for c in pol],
            "total": len(biz) + len(pol)}


def _changes(conn, org, business_id):
    ch = bi.get_business_changes(conn, org, business_id)
    if ch is None:
        raise SectionUnavailable("Changes could not find the linked business.")
    if ch.get("status") != "compared":
        return {"status": "insufficient", "reason": ch.get("reason"), "dates": ch.get("dates") or []}
    return {"status": "available", "from": ch.get("from"), "to": ch.get("to"),
            "summary": ch.get("summary") or {},
            # Not restated in the later documents. This is NOT a removal.
            "not_reobserved": len(ch.get("not_reobserved") or [])}


def _attention(sections: dict) -> list[dict]:
    """Things a broker should look at, each pointing to the tab that shows it."""
    out = []
    conf = sections.get("conflicts") or {}
    if conf.get("status") == "available" and conf.get("total"):
        out.append({"kind": "conflict", "tab": "conflicts", "count": conf["total"],
                    "text": f"{conf['total']} conflicting value(s) between documents — both kept, a person must resolve them."})
    cov = sections.get("coverage") or {}
    if cov.get("status") == "available" and cov.get("open_findings"):
        n = len(cov["open_findings"])
        out.append({"kind": "finding", "tab": "coverage", "count": n,
                    "text": f"{n} coverage finding(s) open for broker review."})
    for name in ("profile", "policies"):
        prov = (sections.get(name) or {}).get("provenance") or {}
        unverified = (prov.get("by_status") or {}).get("needs_review", 0)
        if unverified:
            out.append({"kind": "unverified", "tab": "business" if name == "profile" else "policies",
                        "count": unverified,
                        "text": f"{unverified} {'business' if name == 'profile' else 'policy'} fact(s) not yet verified by a person."})
    prof = sections.get("profile") or {}
    if prof.get("status") in ("available",) and prof.get("missing_sections"):
        out.append({"kind": "missing", "tab": "business", "count": len(prof["missing_sections"]),
                    "text": "Not found in the documents: " + ", ".join(s.replace("_", " ") for s in prof["missing_sections"]) + "."})
    docs = sections.get("documents") or {}
    if docs.get("errors"):
        out.append({"kind": "processing_error", "tab": "documents", "count": len(docs["errors"]),
                    "text": f"{len(docs['errors'])} document(s) failed processing."})
    for name, s in sections.items():
        if (s or {}).get("status") == "unavailable":
            out.append({"kind": "unavailable", "tab": "overview", "count": 1,
                        "text": f"{name.capitalize()} could not be loaded: {s.get('error')}"})
    return out


def get_client_intelligence(conn, org: str, client: dict, as_of: date | None = None) -> dict:
    client_id = str(client["id"])
    sections: dict = {"documents": _section(conn, "documents", lambda: _documents(conn, org, client_id))}
    business = None
    biz_id = str(client["business_entity_id"]) if client.get("business_entity_id") else None
    if biz_id:
        ent = db.get_entity(conn, biz_id, org)
        if ent is not None and ent["entity_type"] == "business":
            business = {"business_id": biz_id, "name": ent["name"]}
    if business is None:
        reason = ("This client is not linked to an analyzed business yet. Upload documents for intelligence, "
                  "run analysis, then link the business from the Documents panel.")
        for name in ("profile", "policies", "coverage", "conflicts", "changes"):
            sections[name] = {"status": "not_linked", "reason": reason}
    else:
        prof = _section(conn, "profile", lambda: _profile(conn, org, biz_id, as_of))
        refs = prof.pop("_policies", []) if prof.get("status") != "unavailable" else []
        sections["profile"] = prof
        sections["policies"] = (_section(conn, "policies", lambda: _policies(conn, org, refs, as_of))
                                if prof.get("status") != "unavailable"
                                else {"status": "unavailable", "error": "Needs the business profile."})
        sections["coverage"] = _section(conn, "coverage", lambda: _coverage(conn, org, biz_id, as_of))
        sections["conflicts"] = _section(conn, "conflicts",
                                         lambda: _conflicts(conn, org, biz_id, [r["policy_id"] for r in refs]))
        sections["changes"] = _section(conn, "changes", lambda: _changes(conn, org, biz_id))
    return {"client": {"client_id": client_id, "business_name": client["business_name"],
                       "status": client.get("status")},
            "business": business,
            "as_of": (as_of or date.today()).isoformat(),
            "historical": as_of is not None and as_of < date.today(),
            "sections": sections,
            "attention": _attention(sections),
            "note": NOTE}
