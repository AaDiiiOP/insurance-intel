"""
Phase 1C — Policy Intelligence service (v0.11.0).

A structured, evidence-backed view of an insurance policy built ON the
Knowledge Core — no second store, no second set of rules. Every section is
produced by the same selection logic the Knowledge query layer uses
(knowledge/query.py: stated period covering the day wins, contradictions
abstain, undated values never applied outside known periods, facts without
evidence never used), so the policy view, the query API and comparisons can
never disagree with each other.

    get_policy()           header (number, insurer, period, status), coverages
                           with their own limits, policy limit, deductible, sum
                           insured, endorsements (+ ids and effective dates),
                           exclusions, conditions, definitions, sub-limits,
                           supporting documents by role, unresolved conflicts
    get_policy_history()   every version of every policy fact, labelled
                           current / historical / superseded / amended /
                           contradictory / rejected, grouped by period
    compare_policies()     previous vs current period of one policy, or two
                           policies: typed changes with evidence from both sides
    find_policy_conflicts() unresolved contradictions across the policy and
                           its coverages, both values with evidence
    list_policies()        policies in the organization

Everything is organization-scoped; a policy id from another organization is
indistinguishable from a missing one (None).
"""
from __future__ import annotations

from datetime import date

import db
from knowledge import query as kq
from knowledge.fact_types import FACT_DEFINITIONS

POLICY_FACT_TYPES = ("policy.number", "policy.insurer", "policy.coverage", "policy.coverage_limit",
                     "policy.deductible", "policy.endorsement", "policy.endorsement_ref",
                     "policy.exclusion", "policy.condition", "policy.definition", "policy.sublimit")
_SECTION = {"policy.coverage": "coverages", "policy.endorsement": "endorsements",
            "policy.exclusion": "exclusions", "policy.condition": "conditions",
            "policy.definition": "definitions", "policy.sublimit": "sublimits"}
_CHANGE_NAME = {"policy.coverage": "coverage", "policy.endorsement": "endorsement",
                "policy.endorsement_ref": "endorsement", "policy.exclusion": "exclusion",
                "policy.condition": "condition", "policy.definition": "definition",
                "policy.sublimit": "sublimit", "policy.coverage_limit": "limit",
                "policy.deductible": "deductible", "coverage.limit": "coverage_limit",
                "policy.number": "policy_number", "policy.insurer": "insurer"}


def _day(as_of) -> date:
    if as_of is None:
        return date.today()
    return as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))


def _policy_entity(conn, org: str, policy_id: str):
    e = db.get_entity(conn, policy_id, org)
    return e if e and e["entity_type"] == "policy" else None


def _related(conn, org, entity_id, rel, direction, entity_type=None):
    out = []
    for r in db.find_relationships(conn, org, entity_id, rel, direction):
        other_id = r["to_entity_id"] if direction == "from" else r["from_entity_id"]
        o = db.get_entity(conn, str(other_id), org)
        if o is not None and (entity_type is None or o["entity_type"] == entity_type):
            out.append(o)
    return out


def _coverage_entities(conn, org, policy):
    return _related(conn, org, str(policy["id"]), "provides", "from", "coverage")


def _view(conn, org, targets, fact_type, day, mode="current") -> dict:
    r = kq._empty("", {"fact_type": fact_type, "mode": mode})
    r = kq._answer_fact(conn, org, r, targets, fact_type, mode, day)
    return {k: r[k] for k in ("answer", "values", "facts_used", "review_status", "authoritative",
                              "contradictions", "reason", "effective_period", "candidates")}


def _periods(conn, org, targets) -> list[tuple]:
    known = set()
    for ft in kq._PERIOD_BOUND:
        for f in kq._load_facts(conn, org, targets, ft):
            if f.get("effective_from") and f.get("effective_to"):
                known.add(kq._period_key(f))
    return sorted(known, key=lambda k: k[0])


def _status(periods, day: date) -> str:
    if not periods:
        return "unknown"
    if any(kq._covers(k, day) for k in periods):
        return "current"
    if all(k[1] < day for k in periods):
        return "expired"
    if all(k[0] > day for k in periods):
        return "future"
    return "between_periods"


def list_policies(conn, org: str) -> list[dict]:
    out = []
    for p in db.list_entities(conn, org, "policy"):
        periods = _periods(conn, org, [p])
        num = _view(conn, org, [p], "policy.number", date.today())
        out.append({"policy_id": str(p["id"]), "name": p["name"], "policy_number": num["answer"],
                    "periods": [{"from": kq._iso(a), "to": kq._iso(b)} for a, b in periods],
                    "status": _status(periods, date.today())})
    return out


def get_policy(conn, org: str, policy_id: str, as_of=None) -> dict | None:
    policy = _policy_entity(conn, org, policy_id)
    if policy is None:
        return None
    day = _day(as_of)
    targets = [policy]
    periods = _periods(conn, org, targets)
    in_force = [k for k in periods if kq._covers(k, day)]

    def v(ft):
        return _view(conn, org, targets, ft, day)

    coverages = []
    for ce in _coverage_entities(conn, org, policy):
        lim = _view(conn, org, [ce], "coverage.limit", day)
        coverages.append({"coverage_id": str(ce["id"]),
                          "name": (ce.get("attributes") or {}).get("coverage") or ce["name"],
                          "limit": lim})
    businesses = _related(conn, org, str(policy["id"]), "has_policy", "to", "business")
    assets = [a for b in businesses for a in _related(conn, org, str(b["id"]), "owns", "from", "asset")]

    refs = []
    for f in kq._load_facts(conn, org, targets, "policy.endorsement_ref"):
        if f.get("valid_to") is not None:
            continue
        start = f.get("effective_from")
        refs.append({**kq._fact_out(f),
                     "in_effect_on_date": bool(start and start <= day and (f.get("effective_to") is None
                                                                           or day <= f["effective_to"]))})

    documents = []
    for d in _related(conn, org, str(policy["id"]), "supports", "to", "document"):
        attrs = d.get("attributes") or {}
        documents.append({"entity_id": str(d["id"]), "name": d["name"],
                          "document_id": attrs.get("document_id"), "role": attrs.get("role")})

    return {
        "policy": {"policy_id": str(policy["id"]), "name": policy["name"]},
        "as_of": day.isoformat(),
        "policy_number": v("policy.number"),
        "insurer": v("policy.insurer"),
        "period": {"in_force": [{"from": kq._iso(a), "to": kq._iso(b)} for a, b in in_force],
                   "known": [{"from": kq._iso(a), "to": kq._iso(b)} for a, b in periods],
                   "status": _status(periods, day)},
        "coverages_listed": v("policy.coverage"),
        "coverages": coverages,
        "policy_limit": v("policy.coverage_limit"),
        "deductible": v("policy.deductible"),
        "sum_insured": _view(conn, org, assets, "property.sum_insured", day) if assets else None,
        "endorsements": v("policy.endorsement"),
        "endorsement_refs": refs,
        "exclusions": v("policy.exclusion"),
        "conditions": v("policy.condition"),
        "definitions": v("policy.definition"),
        "sublimits": v("policy.sublimit"),
        "documents": documents,
        "conflicts": find_policy_conflicts(conn, org, policy_id) or [],
    }


def find_policy_conflicts(conn, org: str, policy_id: str) -> list[dict] | None:
    policy = _policy_entity(conn, org, policy_id)
    if policy is None:
        return None
    out = []
    entities = [(policy, None)] + [(c, (c.get("attributes") or {}).get("coverage") or c["name"])
                                   for c in _coverage_entities(conn, org, policy)]
    for ent, cov in entities:
        types = POLICY_FACT_TYPES if ent is policy else ("coverage.limit",)
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
            out.append({"conflict_id": str(c["id"]), "fact_type": ft, "coverage": cov,
                        "values": [f["value"] for f in facts], "facts": facts})
    return out


def _version_label(f, all_rows, day) -> str:
    if f["status"] == "superseded":
        return "rejected"
    if f["status"] == "contradiction_flagged":
        return "contradictory"
    newer = [o for o in all_rows if str(o["id"]) != str(f["id"]) and kq._period_key(o) == kq._period_key(f)
             and o["valid_from"] > f["valid_from"] and o["status"] != "superseded"]
    if f.get("valid_to") is not None and newer:
        return "superseded"
    if f["status"] == "corrected":
        return "amended" if f.get("valid_to") is None else "amended (historical)"
    if f.get("valid_to") is not None:
        return "historical"
    return "current"


def get_policy_history(conn, org: str, policy_id: str, as_of=None) -> dict | None:
    policy = _policy_entity(conn, org, policy_id)
    if policy is None:
        return None
    day = _day(as_of)
    groups: dict = {}
    entities = [(policy, None, POLICY_FACT_TYPES)] + [
        (c, (c.get("attributes") or {}).get("coverage") or c["name"], ("coverage.limit",))
        for c in _coverage_entities(conn, org, policy)]
    for ent, cov, types in entities:
        for ft in types:
            rows = []
            for f in db.list_facts_for_entity(conn, str(ent["id"]), org, ft, False):
                f = dict(f)
                f["_evidence"] = db.get_evidence_for_fact(conn, str(f["id"]), org)
                rows.append(f)
            for f in rows:
                key = kq._period_key(f) if ft != "policy.endorsement_ref" else (None, None)
                label = (f"{kq._iso(key[0])} to {kq._iso(key[1])}" if key[0] else "no stated period")
                item = {**kq._fact_out(f), "version": _version_label(f, rows, day), "coverage": cov}
                groups.setdefault(label, []).append(item)
    return {"policy": {"policy_id": str(policy["id"]), "name": policy["name"]},
            "periods": [{"period": k, "facts": sorted(v, key=lambda x: (x["fact_type"], x["valid_from"] or ""))}
                        for k, v in sorted(groups.items())]}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _side(conn, org, policy, period_key) -> dict:
    """{key: rows} for one side. key = fact_type, or ("coverage.limit", name).
    period_key None -> the facts with no stated period (still current)."""
    out = {}
    entities = [(policy, None, [ft for ft in POLICY_FACT_TYPES if ft != "policy.endorsement_ref"])] + [
        (c, (c.get("attributes") or {}).get("coverage") or c["name"], ["coverage.limit"])
        for c in _coverage_entities(conn, org, policy)]
    for ent, cov, types in entities:
        for ft in types:
            facts = kq._load_facts(conn, org, [ent], ft)
            groups = kq._by_period(facts)
            rows = groups.get(period_key, [])
            if rows:
                out[(ft, cov) if cov else ft] = rows
    # Endorsement ids belong to the period their own effective date falls in.
    if period_key and period_key[0]:
        refs = [f for f in kq._load_facts(conn, org, [policy], "policy.endorsement_ref")
                if f.get("valid_to") is None and f.get("effective_from")
                and period_key[0] <= f["effective_from"] <= period_key[1]]
        if refs:
            out["policy.endorsement_ref"] = refs
    return out


def _review(rows) -> str:
    st = {r["status"] for r in rows}
    if "contradiction_flagged" in st:
        return "contradiction"
    return "verified" if st and st <= {"verified", "corrected"} else "unverified"


def _ev(rows):
    return [dict(e, value=r["value"]) for r in rows for e in kq._fact_out(r)["evidence"]]


def _diff(old: dict, new: dict) -> list[dict]:
    changes = []
    for key in sorted(set(old) | set(new), key=str):
        ft = key[0] if isinstance(key, tuple) else key
        cov = key[1] if isinstance(key, tuple) else None
        a, b = old.get(key, []), new.get(key, [])
        name = _CHANGE_NAME.get(ft, ft)
        singleton = FACT_DEFINITIONS[ft].is_singleton
        base = {"fact_type": ft, "coverage": cov}
        if any(r["status"] == "contradiction_flagged" for r in a + b) or \
                (singleton and (len(kq._distinct_values(a)) > 1 or len(kq._distinct_values(b)) > 1)):
            changes.append({**base, "change_type": "unresolved_contradiction",
                            "old_values": kq._distinct_values(a), "new_values": kq._distinct_values(b),
                            "old_evidence": _ev(a), "new_evidence": _ev(b), "review_status": "contradiction"})
            continue
        if singleton:
            if not a or not b:
                changes.append({**base, "change_type": "unknown",
                                "old_value": a[0]["value"] if a else None, "new_value": b[0]["value"] if b else None,
                                "note": "stated on one side only — absence is not evidence of removal",
                                "old_evidence": _ev(a), "new_evidence": _ev(b), "review_status": _review(a + b)})
                continue
            x, y = a[0], b[0]
            nx, ny = kq._number(x), kq._number(y)
            ch = {**base, "old_value": x["value"], "new_value": y["value"],
                  "old_evidence": _ev([x]), "new_evidence": _ev([y]), "review_status": _review([x, y])}
            if FACT_DEFINITIONS[ft].is_numeric and nx is not None and ny is not None:
                ch["absolute_change"] = ny - nx
                ch["percentage_change"] = round((ny - nx) / nx * 100, 1) if nx else None
                ch["change_type"] = (f"{name}_increase" if ny > nx else f"{name}_decrease" if ny < nx
                                     else f"{name}_unchanged")
            else:
                same = kq._norm(x["value"]) == kq._norm(y["value"])
                ch["change_type"] = f"{name}_unchanged" if same else f"{name}_changed"
            changes.append(ch)
        else:
            ka = {kq._norm(r["value"]): r for r in a}
            kb = {kq._norm(r["value"]): r for r in b}
            for k in sorted(set(kb) - set(ka)):
                changes.append({**base, "change_type": f"{name}_added", "new_value": kb[k]["value"],
                                "new_evidence": _ev([kb[k]]), "review_status": _review([kb[k]])})
            for k in sorted(set(ka) - set(kb)):
                changes.append({**base, "change_type": f"{name}_removed", "old_value": ka[k]["value"],
                                "old_evidence": _ev([ka[k]]), "review_status": _review([ka[k]]),
                                "note": "not stated in the newer version"})
            for k in sorted(set(ka) & set(kb)):
                changes.append({**base, "change_type": f"{name}_unchanged", "value": kb[k]["value"],
                                "old_evidence": _ev([ka[k]]), "new_evidence": _ev([kb[k]]),
                                "review_status": _review([ka[k], kb[k]])})
    return changes


def compare_policies(conn, org: str, left_policy_id: str, right_policy_id: str | None = None,
                     as_of=None) -> dict | None:
    """Previous vs current period of ONE policy (right_policy_id None), or two
    policies (each side = its latest stated period, else its undated facts).
    Absent-on-one-side singletons are 'unknown', never 'removed'."""
    left = _policy_entity(conn, org, left_policy_id)
    if left is None:
        return None
    day = _day(as_of)
    if right_policy_id is None:
        periods = _periods(conn, org, [left])
        if len(periods) < 2:
            return {"status": "insufficient", "changes": [],
                    "reason": "Only one stated policy period is known for this policy; nothing to compare.",
                    "periods": [{"from": kq._iso(a), "to": kq._iso(b)} for a, b in periods]}
        cur = next((i for i, k in enumerate(periods) if kq._covers(k, day)), len(periods) - 1)
        if cur == 0:
            return {"status": "insufficient", "changes": [],
                    "reason": "No earlier stated period exists before the current one."}
        old_k, new_k = periods[cur - 1], periods[cur]
        old, new = _side(conn, org, left, old_k), _side(conn, org, left, new_k)
        left_desc = right_desc = {"policy_id": str(left["id"]), "name": left["name"]}
    else:
        right = _policy_entity(conn, org, right_policy_id)
        if right is None:
            return None
        pl, pr = _periods(conn, org, [left]), _periods(conn, org, [right])
        old_k, new_k = (pl[-1] if pl else (None, None)), (pr[-1] if pr else (None, None))
        old, new = _side(conn, org, left, old_k), _side(conn, org, right, new_k)
        left_desc = {"policy_id": str(left["id"]), "name": left["name"]}
        right_desc = {"policy_id": str(right["id"]), "name": right["name"]}
    changes = _diff(old, new)
    if old_k != new_k:
        changes.insert(0, {"fact_type": "policy.period", "change_type": "policy_period_changed",
                           "old_value": f"{kq._iso(old_k[0])} to {kq._iso(old_k[1])}" if old_k[0] else None,
                           "new_value": f"{kq._iso(new_k[0])} to {kq._iso(new_k[1])}" if new_k[0] else None,
                           "review_status": "deterministic"})
    material = [c for c in changes if not c["change_type"].endswith("_unchanged")]
    return {"status": "compared", "left": left_desc, "right": right_desc,
            "old_period": {"from": kq._iso(old_k[0]), "to": kq._iso(old_k[1])},
            "new_period": {"from": kq._iso(new_k[0]), "to": kq._iso(new_k[1])},
            "changes": changes,
            "summary": {"material_changes": len(material),
                        "contradictions": sum(c["change_type"] == "unresolved_contradiction" for c in changes),
                        "unknown": sum(c["change_type"] == "unknown" for c in changes),
                        "types": sorted({c["change_type"] for c in material})}}
