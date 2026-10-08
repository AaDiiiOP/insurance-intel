"""
Phase 1D — Business Risk Intelligence service (v0.12.0).

The business is the central object. Everything here is a READ over the
existing Knowledge Core (entities, facts, evidence, relationships, point-in-
time observations) — no second store, no graph database:

    Business
     ├─ has_location ──────> Location ─┬─ supports_operation ─> Operation
     ├─ performs_operation > Operation │
     ├─ has_control ───────> Control <─┘ (has_control)
     ├─ owns ──────────────> Asset           (asset.declared_value)
     ├─ has_claim ─────────> Claim summary   (loss.claims_count, loss.total_incurred)
     ├─ has_risk_factor ───> Risk factor
     ├─ has_risk ──────────> Risk ── mitigated_by ─> Control
     ├─ has_exposure ──────> Exposure
     └─ has_policy ────────> Policy ── insures ─> Location / Asset / Business

    get_business_profile(as_of)  every section, each value with evidence and its observation date
    get_business_graph()         nodes + edges + evidence references
    get_business_changes()       ADDED / CHANGED / UNCHANGED / CONFLICTING between two dates
                                 (never "removed" from mere absence — not restated is not removed)
    find_business_conflicts()    unresolved contradictions with both values and evidence

Nothing is inferred: no risk from an industry, no control that is not
written, no loss that is not in a loss run, no score. A section with no
documented content says so.
"""
from __future__ import annotations

from datetime import date, timedelta

import db
from knowledge import query as kq
from knowledge.fact_types import FACT_DEFINITIONS

SECTION_TYPES = {
    "locations": ("location", "has_location", "location.address"),
    "operations": ("operation", "performs_operation", "operation.description"),
    "assets": ("asset", "owns", "asset.declared_value"),
    "controls": ("control", "has_control", "control.description"),
    "claims": ("claim", "has_claim", None),
    "risk_factors": ("risk_factor", "has_risk_factor", "risk_factor.description"),
    "risks": ("risk", "has_risk", "risk.description"),
    "exposures": ("exposure", "has_exposure", "exposure.description"),
}
CLAIM_TYPES = ("loss.claims_count", "loss.total_incurred")
BUSINESS_TYPES = ("business.legal_name", "business.workforce", "business.survey_risk_rating")


def _day(v) -> date:
    if v is None:
        return date.today()
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _business(conn, org, business_id):
    e = db.get_entity(conn, business_id, org)
    return e if e and e["entity_type"] == "business" else None


def _children(conn, org, entity, rel, kind):
    out = []
    for r in db.find_relationships(conn, org, str(entity["id"]), rel, "from"):
        o = db.get_entity(conn, str(r["to_entity_id"]), org)
        if o is not None and o["entity_type"] == kind:
            out.append(o)
    return out


def _label(e) -> str:
    a = e.get("attributes") or {}
    for k in ("address", e["entity_type"]):
        if a.get(k):
            return a[k]
    return e["name"].split(" — ", 1)[-1]


def _view(conn, org, entity, fact_type, day, mode):
    r = kq._empty("", {"fact_type": fact_type, "mode": mode})
    r = kq._answer_fact(conn, org, r, [entity], fact_type, mode, day)
    return {k: r.get(k) for k in ("answer", "values", "facts_used", "review_status", "contradictions",
                                  "reason", "observed_on")}


def list_businesses(conn, org: str) -> list[dict]:
    return [{"business_id": str(b["id"]), "name": b["name"]} for b in db.list_entities(conn, org, "business")]


def get_business_profile(conn, org: str, business_id: str, as_of=None) -> dict | None:
    biz = _business(conn, org, business_id)
    if biz is None:
        return None
    day = _day(as_of)
    mode = "as_of" if as_of else "current"
    out = {"business": {"business_id": str(biz["id"]), "name": biz["name"]}, "as_of": day.isoformat(),
           "mode": mode}
    for ft in BUSINESS_TYPES:
        out[ft.split(".")[1]] = _view(conn, org, biz, ft, day, mode)
    for section, (kind, rel, ft) in SECTION_TYPES.items():
        items = []
        for child in _children(conn, org, biz, rel, kind):
            if kind == "claim":
                v = {t.split(".")[1]: _view(conn, org, child, t, day, mode) for t in CLAIM_TYPES}
                if all(x["answer"] is None and not x["contradictions"] for x in v.values()):
                    continue
                items.append({"entity_id": str(child["id"]), "name": _label(child), **v})
                continue
            v = _view(conn, org, child, ft, day, mode)
            if v["answer"] is None and not v["contradictions"]:
                continue          # not observed as of this date
            items.append({"entity_id": str(child["id"]), "name": _label(child), "value": v})
        out[section] = items
    out["documented_values"] = [
        {"asset": i["name"], "declared_value": i["value"]["answer"]} for i in out["assets"]
        if i["value"]["answer"] is not None]
    out["policies"] = [{"policy_id": str(p["id"]), "name": p["name"]}
                       for p in _children(conn, org, biz, "has_policy", "policy")]
    out["conflicts"] = find_business_conflicts(conn, org, business_id) or []
    out["note"] = ("Only what the documents state. No risk, control, loss or exposure is inferred, "
                   "and no risk score is computed.")
    return out


def _all_entities(conn, org, biz):
    """(entity, key-prefix, fact types) for the business and everything it links to."""
    out = [(biz, "", BUSINESS_TYPES)]
    for section, (kind, rel, ft) in SECTION_TYPES.items():
        for child in _children(conn, org, biz, rel, kind):
            out.append((child, f"{kind}:{_label(child)}", CLAIM_TYPES if kind == "claim" else (ft,)))
    return out


def find_business_conflicts(conn, org: str, business_id: str) -> list[dict] | None:
    biz = _business(conn, org, business_id)
    if biz is None:
        return None
    out = []
    for ent, key, types in _all_entities(conn, org, biz):
        for ft in types:
            c = db.get_unresolved_conflict(conn, str(ent["id"]), org, ft)
            if c is None:
                continue
            facts = []
            for fid in db.get_conflict_fact_ids(conn, str(c["id"])):
                f = db.get_fact(conn, str(fid), org)
                if f is not None:
                    f = dict(f)
                    f["_evidence"] = db.get_evidence_for_fact(conn, str(f["id"]), org)
                    facts.append(kq._fact_out(f))
            out.append({"conflict_id": str(c["id"]), "key": key or ft, "fact_type": ft,
                        "values": [f["value"] for f in facts], "facts": facts})
    return out


def _state(facts, day):
    """Latest observation on or before `day` (undated facts count as 'always')."""
    groups = kq._by_period(facts)
    dated = sorted((k for k in groups if k[0] is not None and k[0] <= day), key=lambda k: k[0])
    if dated:
        return groups[dated[-1]], dated[-1][0]
    if (None, None) in groups:
        return groups[(None, None)], None
    return [], None


def _observation_dates(conn, org, biz) -> list[date]:
    ds = set()
    for ent, _, types in _all_entities(conn, org, biz):
        for ft in types:
            for f in kq._load_facts(conn, org, [ent], ft):
                if f.get("effective_from"):
                    ds.add(f["effective_from"])
    return sorted(ds)


def get_business_changes(conn, org: str, business_id: str, from_date=None, to_date=None) -> dict | None:
    """Changes between the business state as of `from_date` and as of `to_date`.
    Default: the latest observation date vs. the observation date before it.

    added        first observed in (from, to]
    changed      re-observed in (from, to] with a different value
    unchanged    re-observed in (from, to] with the same value
    conflicting  an unresolved contradiction in the state at `to`
    Facts not re-observed in the window are listed as not_reobserved and are
    never reported as removed — absence from a later document is not evidence."""
    biz = _business(conn, org, business_id)
    if biz is None:
        return None
    dates = _observation_dates(conn, org, biz)
    if to_date is None and from_date is None:
        if len(dates) < 2:
            return {"status": "insufficient", "changes": [], "dates": [d.isoformat() for d in dates],
                    "reason": "Fewer than two observation dates are recorded; nothing to compare."}
        b, a = dates[-1], dates[-2]
    else:
        b = _day(to_date)
        a = _day(from_date) if from_date else next((d for d in reversed(dates) if d < b), b - timedelta(days=1))
    changes, not_reobserved = [], []
    for ent, key, types in _all_entities(conn, org, biz):
        for ft in types:
            facts = kq._load_facts(conn, org, [ent], ft)
            if not facts:
                continue
            k = key if (key and len(types) == 1) else (f"{key}|{ft}" if key else ft)
            old_rows, old_d = _state(facts, a)
            new_rows, new_d = _state(facts, b)
            window = new_d is not None and a < new_d <= b
            item = {"key": k, "fact_type": ft, "entity_id": str(ent["id"])}
            if any(r["status"] == "contradiction_flagged" for r in new_rows) or len(kq._distinct_values(new_rows)) > 1:
                changes.append({**item, "kind": "conflicting", "values": kq._distinct_values(new_rows),
                                "new_evidence": _ev(new_rows), "review_status": "contradiction"})
                continue
            if not window:
                if new_rows:
                    not_reobserved.append({**item, "value": new_rows[0]["value"],
                                           "observed_on": new_d.isoformat() if new_d else None})
                continue
            new_v = new_rows[0]
            base = {**item, "new_value": new_v["value"], "observed_on": new_d.isoformat(),
                    "new_evidence": _ev([new_v])}
            if not old_rows:
                changes.append({**base, "kind": "added", "review_status": _review([new_v])})
                continue
            old_v = old_rows[0]
            base.update(old_value=old_v["value"], old_evidence=_ev([old_v]),
                        previously_observed_on=old_d.isoformat() if old_d else None)
            same = kq._norm(old_v["value"]) == kq._norm(new_v["value"])
            if not same and FACT_DEFINITIONS[ft].is_numeric:
                no, nn = kq._number(old_v), kq._number(new_v)
                if no is not None and nn is not None:
                    same = abs(no - nn) < 0.5
                    if not same:
                        base["absolute_change"] = nn - no
                        base["percentage_change"] = round((nn - no) / no * 100, 1) if no else None
            changes.append({**base, "kind": "unchanged" if same else "changed",
                            "review_status": _review([old_v, new_v])})
    order = {"conflicting": 0, "added": 1, "changed": 2, "unchanged": 3}
    changes.sort(key=lambda c: (order[c["kind"]], c["key"]))
    material = [c for c in changes if c["kind"] in ("added", "changed", "conflicting")]
    return {"status": "compared", "from": a.isoformat(), "to": b.isoformat(), "changes": changes,
            "not_reobserved": not_reobserved,
            "summary": {k: sum(c["kind"] == k for c in changes) for k in order} | {"material": len(material)}}


def _ev(rows):
    return [dict(e, value=r["value"]) for r in rows for e in kq._fact_out(r)["evidence"]]


def _review(rows) -> str:
    st = {r["status"] for r in rows}
    if "contradiction_flagged" in st:
        return "contradiction"
    return "verified" if st and st <= {"verified", "corrected"} else "unverified"


def get_business_graph(conn, org: str, business_id: str) -> dict | None:
    """Nodes and edges reachable from the business (two hops), each node with
    its current facts' values and evidence references. Read-only."""
    biz = _business(conn, org, business_id)
    if biz is None:
        return None
    nodes, edges, seen = {}, [], set()

    def node(e):
        nid = str(e["id"])
        if nid in nodes:
            return
        facts = []
        for f in db.list_facts_for_entity(conn, nid, org, None, True):
            ev = db.get_evidence_for_fact(conn, str(f["id"]), org)
            if not ev:
                continue
            facts.append({"fact_id": str(f["id"]), "fact_type": f["fact_type"], "value": f["value"],
                          "status": f["status"],
                          "observed_on": kq._iso(f.get("effective_from")),
                          "evidence": [{"document_filename": x.get("document_filename"), "page": x.get("page"),
                                        "snippet": x.get("snippet")} for x in ev[:3]]})
        nodes[nid] = {"id": nid, "type": e["entity_type"], "label": _label(e), "facts": facts}

    frontier = [(biz, 0)]
    node(biz)
    while frontier:
        e, depth = frontier.pop(0)
        if depth >= 2:
            continue
        for r in db.find_relationships(conn, org, str(e["id"]), None, "from"):
            o = db.get_entity(conn, str(r["to_entity_id"]), org)
            if o is None or o["entity_type"] in ("document",):
                continue
            key = (str(e["id"]), str(o["id"]), r["relationship_type"])
            if key in seen:
                continue
            seen.add(key)
            node(o)
            edges.append({"from": str(e["id"]), "to": str(o["id"]), "type": r["relationship_type"]})
            frontier.append((o, depth + 1))
    return {"business_id": str(biz["id"]), "nodes": list(nodes.values()), "edges": edges}
