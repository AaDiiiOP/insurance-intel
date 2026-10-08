"""
Knowledge query layer (v0.10.0) — Phase 1B.5 "Knowledge Retrieval & Reasoning".

Answers a broker's question from the Knowledge Core FIRST, and only falls back
to retrieval (and, optionally, the local model) when the Knowledge Core has no
fact type for the question. The model is never the database:

    question
      -> interpret (fact type, mode: current / as_of / previous / change /
         policy_period, entity)                       deterministic, no LLM
      -> Knowledge Core can answer?
           yes -> structured answer from facts + evidence + periods
           no  -> keyword retrieval over this tenant's documents
                  -> optional local-model synthesis, accepted ONLY if its quote
                     is found in a retrieved passage and every number in its
                     answer appears in that quote; otherwise abstain

Every answer carries the facts, entities, relationships, evidence, source
pages, effective period, review status and contradictions it rests on.
Nothing un-evidenced is ever returned as an answer; a contradiction, a
missing period or an ambiguous entity gives answer=None and needs_review=True.

Tenant isolation: every read goes through organization-scoped repository
calls (db.*), so another organization's entities, facts, evidence and
documents are invisible here exactly as everywhere else.
"""
from __future__ import annotations

import re
from datetime import date

import db
from doc_intelligence import _numeric_value

from . import entities as _entities
from . import relationships as _relationships
from .fact_types import FACT_DEFINITIONS, target_entity_kind

# ---------------------------------------------------------------------------
# 1. Interpretation (deterministic)
# ---------------------------------------------------------------------------

# Most specific phrases first: "coverage limit" must win over "coverage".
_FACT_TYPE_PHRASES = [
    # Phase 1C structure types first: "sub-limit" must win over "limit",
    # "policy number" over "policy", "insurer" over everything.
    ("policy.sublimit", ("sublimit", "sub limit")),
    ("policy.number", ("policy number", "policy no")),
    ("policy.insurer", ("insurer", "carrier", "insurance company", "underwriter", "issued the policy",
                        "issued this policy", "who issued")),
    ("policy.definition", ("definition", "defined", "meaning", "mean")),
    ("policy.condition", ("condition", "requirement", "warranty")),
    ("property.sum_insured", ("sum insured", "insured value", "declared value", "total insured")),
    # v0.10.2: no bare "limit" — "time limit for notifying a claim" and "age
    # limit of the building" were being answered with the coverage limit.
    ("policy.coverage_limit", ("coverage limit", "limit of indemnity", "limit of liability", "cover limit",
                               "policy limit", "overall limit", "maximum limit")),
    ("policy.deductible", ("deductible", "excess")),
    ("policy.exclusion", ("exclusion", "excluded", "not covered")),
    ("policy.endorsement", ("endorsement",)),
    ("property.risk_factors", ("risk factor", "hazard")),
    ("property.occupancy", ("occupancy", "used for", "use of the premises", "business activity")),
    # v0.10.2: no bare "built" — "when was the building built" asks for a year.
    ("property.construction", ("construction", "building material", "built of", "made of")),
    ("property.location", ("location", "address", "situated", "where is")),
    ("policy.coverage", ("coverage", "covered", "cover", "perils", "insured against")),
]
_PERIOD_PHRASES = ("policy period", "period of insurance", "policy term", "valid until",
                   "valid from", "expiry", "expire", "when does the policy", "renewal date")
# v0.10.2: bare "added"/"removed" no longer mean "compare periods" — "what
# endorsements were added" asks for the list; "added at renewal" compares.
_CHANGE_PHRASES = ("change", "changed", "difference", "differ", "compare", "compared", "increase",
                   "decrease", "went up", "went down", "at renewal", "on renewal", "since renewal",
                   "since last year", "versus")
# A fact type is not meant when the question is about someone else's details:
# "the insurer's address" is not the insured property's location.
_NOT_FOR = {
    "property.location": ("insurer", "carrier", "broker", "agent", "surveyor", "contact",
                          "office", "underwriter", "claimant"),
}
_PREVIOUS_PHRASES = ("previous", "prior", "last year", "before renewal", "earlier policy",
                     "expiring", "old policy")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def _contains_phrase(norm_q: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(_norm(phrase))}(?:s|es)?\b", norm_q) is not None


def interpret(question: str) -> dict:
    """Map a question to (intent, fact_type, mode, date). No model involved."""
    import property_policy_intelligence as ppi   # local import: avoids a cycle

    q = _norm(question)
    fact_type = None
    for ft, phrases in _FACT_TYPE_PHRASES:
        if any(_contains_phrase(q, p) for p in phrases):
            if any(_contains_phrase(q, w) for w in _NOT_FOR.get(ft, ())):
                continue
            fact_type = ft
            break

    stated_date = None
    m = re.search(ppi._DATE_PAT, question or "")
    if m:
        stated_date = ppi._parse_date(m.group(0))

    which_coverage = re.search(r"\bwhich (coverage|cover|peril)s?\b", q) is not None
    endorsement_dates = _contains_phrase(q, "endorsement") and any(_contains_phrase(q, p) for p in (
        "became effective", "took effect", "effective during", "effective in", "come into effect",
        "came into effect", "effective date"))
    if endorsement_dates:
        intent = "endorsement_dates"
    elif any(_contains_phrase(q, p) for p in _PERIOD_PHRASES):
        intent = "policy_period"
    elif which_coverage and re.search(r"\b(highest|largest|biggest|lowest|smallest|maximum|minimum)\b", q):
        intent = "coverage_rank"
    elif any(_contains_phrase(q, p) for p in ("contradiction", "conflict", "disagree", "inconsistent")):
        intent = "conflicts"
    elif fact_type is None and any(_contains_phrase(q, p) for p in (
            "in force", "expired", "still active", "is it active", "policy status", "is the policy current",
            "is the policy active", "is the policy valid")):
        intent = "policy_status"
    elif fact_type is None:
        intent = "unknown"
    else:
        intent = "fact"

    if any(_contains_phrase(q, p) for p in _CHANGE_PHRASES):
        mode = "change"
    elif any(_contains_phrase(q, p) for p in _PREVIOUS_PHRASES):
        mode = "previous"
    elif stated_date:
        mode = "as_of"
    else:
        mode = "current"

    if intent == "unknown" and mode == "change":
        intent = "policy_changes"
    if intent in ("coverage_rank", "conflicts", "endorsement_dates", "policy_status", "policy_changes"):
        fact_type = None
    return {"intent": intent, "fact_type": fact_type, "mode": mode,
            "stated_date": stated_date.isoformat() if stated_date else None}


# ---------------------------------------------------------------------------
# 2. Entity resolution (deterministic, tenant-scoped)
# ---------------------------------------------------------------------------

def _resolve_entity(conn, organization_id: str, question: str, entity_id: str | None):
    """(entity, candidates). An explicit entity_id wins; otherwise a business
    whose normalized name or alias appears in the question. Two or more
    matches are ambiguous — the caller abstains rather than picking one."""
    if entity_id:
        e = db.get_entity(conn, entity_id, organization_id)
        return (e, [e] if e else [])
    q = f" {_entities.normalize_name(question)} "
    matches = []
    for e in db.list_entities(conn, organization_id, None):
        names = [e["name"], *((e.get("attributes") or {}).get("aliases") or [])]
        keys = {_entities.normalize_name(n) for n in names}
        if any(k and len(k) >= 3 and f" {k} " in q for k in keys):
            matches.append(e)
    # Prefer businesses: "Sunrise Textiles" also prefixes its asset/policy names.
    businesses = [e for e in matches if e["entity_type"] == "business"]
    pool = businesses or matches
    return (pool[0] if len(pool) == 1 else None, pool)


def _targets(conn, organization_id: str, entity: dict, kind: str):
    """Entities that hold facts of `kind` (asset/policy) for `entity`, by
    following its outgoing relationships one hop. Returns (targets, edges)."""
    if entity["entity_type"] == kind:
        return [entity], []
    targets, edges = [], []
    for r in _relationships.find_relationships(conn, organization_id, str(entity["id"]), None, "from"):
        other = db.get_entity(conn, str(r["to_entity_id"]), organization_id)
        if other is not None and other["entity_type"] == kind:
            targets.append(other)
            edges.append({"relationship_id": str(r["id"]), "type": r["relationship_type"],
                          "from_entity_id": str(entity["id"]), "to_entity_id": str(other["id"]),
                          "to_name": other["name"]})
    return targets, edges


# ---------------------------------------------------------------------------
# 2b. Qualifiers (v0.10.2). "What is the deductible FOR FLOOD?" is not "what is
# the deductible?" — the Knowledge Core holds one policy-wide deductible and
# must not present it as the flood deductible. Words left over after removing
# the fact-type phrase, the entity's name, dates, mode words and generic words
# are a qualifier:
#   singleton fact type  -> abstain; the policy-wide value goes in candidates
#   multi-valued type    -> membership: values containing the qualifier, or
#                           abstain ("not among the recorded ...")
# ---------------------------------------------------------------------------
_GENERIC = set("""
what which who whom whose is are was were be been being am do does did done has have had having
the a an of for to in on at by with from into about as and or not no any all each this that these
those there here it its it's they them their our my your me we us i you he she his her
how much many tell show list give get please recorded stated listed noted mentioned found identified
current currently now today present existing total overall standard general main
policy policies insured insurance property premises client company business firm
value values amount amounts figure apply applies applicable applied under per
document documents schedule survey report record records file
previous prior last year years before earlier expiring old renewal change changed changes
difference differ compare compared increase increased decrease decreased went up down since versus
date on as of still ever yet also just only exactly specifically
pvt ltd private limited llp llc inc co corp
""".split())
_MONTH_WORDS = {"january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
                "sep", "sept", "oct", "nov", "dec"}


def _phrase_words() -> set:
    words = set()
    for _, phrases in _FACT_TYPE_PHRASES:
        for p in phrases:
            words.update(_norm(p).split())
    for p in _PERIOD_PHRASES + _CHANGE_PHRASES + _PREVIOUS_PHRASES:
        words.update(_norm(p).split())
    # inflections of the fact-type words that commonly appear in questions
    words.update({"covers", "covering", "covered", "excludes", "exclusions", "excluded", "endorsements",
                  "deductibles", "limits", "located", "factors", "hazards", "risks", "risk", "occupied",
                  "constructed", "insures", "insured", "perils", "peril"})
    return words


def _qualifier_terms(question: str, entity: dict) -> list[str]:
    names = [entity["name"], *((entity.get("attributes") or {}).get("aliases") or [])]
    entity_words = set()
    for n in names:
        entity_words.update(_norm(n).split())
    skip = _GENERIC | _phrase_words() | entity_words | _MONTH_WORDS
    out = []
    for w in _norm(question).split():
        if w in skip or w.isdigit() or re.fullmatch(r"\d+(st|nd|rd|th)", w) or len(w) < 3:
            continue
        if w.endswith("s") and w[:-1] in skip:
            continue
        out.append(w)
    return out


def _apply_qualifier(result: dict, fact_type: str, terms: list[str]) -> dict:
    singleton = FACT_DEFINITIONS[fact_type].is_singleton
    phrase = " ".join(terms)
    if singleton:
        if result["facts_used"]:
            result["candidates"] = result["facts_used"]
        result.update(answer=None, answer_type="none", authoritative=False, needs_review=True,
                      facts_used=[], evidence=[], source_documents=[], source_pages=[],
                      reason=(f"The question asks about a specific '{phrase}'; only a policy-wide "
                              f"{fact_type} is recorded, so it is not presented as the answer."))
        return result
    if result["answer"] is None:
        return result
    keep = [f for f in result["facts_used"]
            if all(t in _norm(f["value"]) or t.rstrip("s") in _norm(f["value"]) for t in terms)]
    if not keep:
        label = fact_type.split(".")[1].replace("_", " ")
        result["candidates"] = result["facts_used"]
        result.update(answer=None, answer_type="none", authoritative=False, needs_review=True,
                      facts_used=[], evidence=[], source_documents=[], source_pages=[], values=[],
                      reason=(f"'{phrase}' is not among the recorded {label} values; absence from the "
                              "record does not prove the opposite."))
        return result
    ids = {f["fact_id"] for f in keep}
    result["facts_used"] = keep
    result["evidence"] = [e for e in result["evidence"] if e["fact_id"] in ids]
    result["values"] = [f["value"] for f in keep]
    result["answer"] = result["values"]
    docs = {e["document_filename"] for e in result["evidence"]}
    result["source_documents"] = sorted(d for d in docs if d)
    result["source_pages"] = [p for p in result["source_pages"] if p["document"] in docs]
    return result


# ---------------------------------------------------------------------------
# 3. Fact selection
# ---------------------------------------------------------------------------

def _period_key(f: dict):
    return (f.get("effective_from"), f.get("effective_to"))


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _fact_out(f: dict) -> dict:
    return {
        "fact_id": str(f["id"]), "entity_id": str(f["entity_id"]), "fact_type": f["fact_type"],
        "value": f["value"],
        "normalized_value": float(f["normalized_value"]) if f.get("normalized_value") is not None else None,
        "status": f["status"],
        "effective_from": _iso(f.get("effective_from")), "effective_to": _iso(f.get("effective_to")),
        "valid_from": _iso(f.get("valid_from")), "valid_to": _iso(f.get("valid_to")),
        "reviewed_by": f.get("reviewed_by"),
        "confidence": {"value": float(f["confidence_value"]) if f.get("confidence_value") is not None else None,
                       "type": f.get("confidence_type")},
        "evidence": [
            {"evidence_id": str(e["id"]), "document_id": str(e["document_id"]),
             "document_filename": e.get("document_filename"), "page": e.get("page"),
             "section": e.get("section"), "snippet": e.get("snippet"), "source": e.get("source")}
            for e in f.get("_evidence", [])
        ],
    }


def _load_facts(conn, organization_id: str, targets: list[dict], fact_type: str) -> list[dict]:
    """Every non-rejected fact of this type on the target entities, each with
    its evidence. Facts with no evidence are dropped: nothing un-evidenced is
    ever used to answer."""
    out = []
    for t in targets:
        for f in db.list_facts_for_entity(conn, str(t["id"]), organization_id, fact_type, False):
            if f["status"] == "superseded":
                continue
            f = dict(f)
            f["_evidence"] = db.get_evidence_for_fact(conn, str(f["id"]), organization_id)
            if f["_evidence"]:
                out.append(f)
    return out


def _effective_rows(rows: list[dict]) -> list[dict]:
    """Within one stated period: the open rows if any, else the most recently
    closed one(s) — so a corrected-away value never comes back as 'the' value."""
    open_rows = [r for r in rows if r.get("valid_to") is None]
    if open_rows:
        return open_rows
    last = max(r["valid_to"] for r in rows)
    return [r for r in rows if r["valid_to"] == last]


def _by_period(facts: list[dict]) -> dict:
    groups: dict = {}
    for f in facts:
        groups.setdefault(_period_key(f), []).append(f)
    return {k: _effective_rows(v) for k, v in groups.items()}


def _covers(period, day: date) -> bool:
    start, end = period
    return start is not None and end is not None and start <= day <= end


def _distinct_values(rows: list[dict]) -> list[str]:
    seen, out = set(), []
    for r in rows:
        k = " ".join(str(r["value"]).lower().split())
        if k not in seen:
            seen.add(k)
            out.append(r["value"])
    return out


def _number(f: dict):
    if f.get("normalized_value") is not None:
        return float(f["normalized_value"])
    return _numeric_value(f["value"])


# ---------------------------------------------------------------------------
# 4. Answer assembly
# ---------------------------------------------------------------------------

def _empty(question: str, interpretation: dict) -> dict:
    return {
        "question": question, "interpretation": interpretation,
        "answer": None, "answer_type": "none", "values": [],
        "facts_used": [], "entities_used": [], "relationships_used": [],
        "evidence": [], "source_documents": [], "source_pages": [],
        "effective_period": None, "review_status": None, "authoritative": False,
        "confidence": None, "contradictions": [], "needs_review": True,
        "reason": None, "change": None, "retrieved_passages": [], "candidates": [],
    }


def _finish(result: dict, rows: list[dict], period=None) -> dict:
    facts = [_fact_out(r) for r in rows]
    result["facts_used"] = facts
    ev = [dict(e, fact_id=f["fact_id"]) for f in facts for e in f["evidence"]]
    result["evidence"] = ev
    result["source_documents"] = sorted({e["document_filename"] for e in ev if e["document_filename"]})
    result["source_pages"] = sorted({(e["document_filename"], e["page"]) for e in ev},
                                    key=lambda x: (str(x[0]), x[1] or 0))
    result["source_pages"] = [{"document": d, "page": p} for d, p in result["source_pages"]]
    if period and period != (None, None):
        result["effective_period"] = {"from": _iso(period[0]), "to": _iso(period[1])}
    statuses = {f["status"] for f in facts}
    if "contradiction_flagged" in statuses:
        result["review_status"] = "contradiction"
    elif statuses and statuses <= {"verified", "corrected"}:
        result["review_status"] = "verified"
    else:
        result["review_status"] = "unverified"
    conf = [f["confidence"]["value"] for f in facts if f["confidence"]["value"] is not None]
    result["confidence"] = {"min": min(conf), "type": "lowest of the facts used"} if conf else None
    result["authoritative"] = result["answer"] is not None and result["review_status"] == "verified"
    result["needs_review"] = not result["authoritative"]
    return result


def _conflicts(conn, organization_id: str, targets: list[dict], fact_type: str) -> list[dict]:
    out = []
    for t in targets:
        c = db.get_unresolved_conflict(conn, str(t["id"]), organization_id, fact_type)
        if c is not None:
            out.append({"conflict_id": str(c["id"]), "entity_id": str(t["id"]), "fact_type": fact_type,
                        "fact_ids": [str(x) for x in db.get_conflict_fact_ids(conn, str(c["id"]))]})
    return out


# Fact types whose effective dates ARE the policy period (Phase 1C adds the
# structure types). policy.endorsement_ref is deliberately absent: its dates are
# the endorsement's own effective date, not the policy period.
_PERIOD_BOUND = ("policy.coverage", "policy.coverage_limit", "policy.deductible",
                 "policy.exclusion", "policy.endorsement", "property.sum_insured",
                 "policy.number", "policy.insurer", "policy.condition", "policy.definition",
                 "policy.sublimit")


def _outside_known_periods(conn, organization_id, targets, fact_type, day: date) -> bool:
    """v0.10.2 (found by the real-Qwen safety probe): an UNDATED value of a
    period-bound fact type (limit, deductible, sum insured, …) must not be
    given for a day outside every policy period known for this entity — e.g.
    when the policy document's limit was missed and only a schedule's undated
    figure exists, asking about a date before the policy began must abstain."""
    if fact_type not in _PERIOD_BOUND:
        return False
    known = set()
    for ft in _PERIOD_BOUND:
        for f in _load_facts(conn, organization_id, targets, ft):
            if f.get("effective_from") and f.get("effective_to"):
                known.add(_period_key(f))
    return bool(known) and not any(_covers(k, day) for k in known)


def _answer_fact(conn, organization_id, result, targets, fact_type, mode, day: date):
    singleton = FACT_DEFINITIONS[fact_type].is_singleton
    facts = _load_facts(conn, organization_id, targets, fact_type)
    if not facts:
        result["reason"] = f"No evidenced {fact_type} fact is recorded for this entity."
        return result
    periods = _by_period(facts)
    dated = sorted((k for k in periods if k[0] is not None), key=lambda k: k[0])

    if mode in ("previous", "change"):
        if len(dated) < 2:
            result["reason"] = ("Only one stated policy period is known for this fact, so there is "
                                "no previous value to report.")
            result["candidates"] = [_fact_out(r) for k in periods for r in periods[k]]
            return result
        # "current" = the period covering `day`, else the latest; previous = the one before it.
        cur_idx = next((i for i, k in enumerate(dated) if _covers(k, day)), len(dated) - 1)
        if cur_idx == 0:
            result["reason"] = "No earlier stated period exists before the current one."
            return result
        old_k, new_k = dated[cur_idx - 1], dated[cur_idx]
        old_rows, new_rows = periods[old_k], periods[new_k]
        bad = [k for k, rows in ((old_k, old_rows), (new_k, new_rows))
               if any(r["status"] == "contradiction_flagged" for r in rows)
               or (singleton and len(_distinct_values(rows)) > 1)]
        if bad:
            result["contradictions"] = [{"period": {"from": _iso(k[0]), "to": _iso(k[1])},
                                         "values": _distinct_values(periods[k])} for k in bad]
            result["reason"] = "A period involved has conflicting values; resolve it before comparing."
            return _finish(result, old_rows + new_rows)
        if mode == "previous":
            result["values"] = _distinct_values(old_rows)
            result["answer"] = result["values"][0] if singleton else result["values"]
            result["answer_type"] = "deterministic"
            return _finish(result, old_rows, old_k)
        # change
        change = {"from_period": {"from": _iso(old_k[0]), "to": _iso(old_k[1])},
                  "to_period": {"from": _iso(new_k[0]), "to": _iso(new_k[1])}}
        if singleton:
            a, b = old_rows[0], new_rows[0]
            change.update(old_value=a["value"], new_value=b["value"])
            na, nb = _number(a), _number(b)
            if na is not None and nb is not None:
                change["absolute_change"] = nb - na
                change["percent_change"] = round((nb - na) / na * 100, 1) if na else None
                change["direction"] = "increased" if nb > na else "decreased" if nb < na else "unchanged"
            else:
                same = " ".join(a["value"].lower().split()) == " ".join(b["value"].lower().split())
                change["direction"] = "unchanged" if same else "changed"
        else:
            ka = {" ".join(r["value"].lower().split()): r["value"] for r in old_rows}
            kb = {" ".join(r["value"].lower().split()): r["value"] for r in new_rows}
            change.update(added=[kb[k] for k in sorted(set(kb) - set(ka))],
                          removed=[ka[k] for k in sorted(set(ka) - set(kb))],
                          unchanged=[kb[k] for k in sorted(set(ka) & set(kb))])
            change["direction"] = "changed" if change["added"] or change["removed"] else "unchanged"
        result["change"] = change
        result["answer"] = change["direction"]
        result["answer_type"] = "deterministic"
        return _finish(result, old_rows + new_rows)

    # current / as_of. Rule: a stated period covering `day` wins. If periods
    # are stated but none covers `day`, abstain (an expired or future policy is
    # not "current"). Undated facts (e.g. occupancy, which no policy period
    # governs) answer a current question, but are never assumed to have
    # applied on a date the question names explicitly.
    in_period = [k for k in dated if _covers(k, day)]
    undated_open = [r for r in periods.get((None, None), []) if r.get("valid_to") is None]
    if in_period:
        rows = [r for k in in_period for r in periods[k]]
        period = in_period[0] if len(in_period) == 1 else None
    elif dated:
        result["reason"] = (f"No stated policy period covers {day.isoformat()} "
                            f"(known periods: {', '.join(f'{_iso(k[0])} to {_iso(k[1])}' for k in dated)}).")
        result["candidates"] = [_fact_out(r) for k in dated for r in periods[k]]
        return result
    elif undated_open and mode != "as_of" and not _outside_known_periods(
            conn, organization_id, targets, fact_type, day):
        rows, period = undated_open, None
    else:
        result["reason"] = (f"No stated policy period covers {day.isoformat()}; undated facts are "
                            "not assumed to have applied on that date." if undated_open
                            else "No current fact; only historical values are recorded.")
        result["candidates"] = [_fact_out(r) for r in undated_open]
        return result

    conflicts = _conflicts(conn, organization_id, targets, fact_type)
    values = _distinct_values(rows)
    if any(r["status"] == "contradiction_flagged" for r in rows) or (singleton and len(values) > 1):
        result["contradictions"] = conflicts or [{"values": values}]
        result["reason"] = "Sources disagree on this value; a person must correct one side first."
        result["values"] = values
        return _finish(result, rows, period)
    result["values"] = values
    result["answer"] = values[0] if singleton else values
    result["answer_type"] = "deterministic"
    return _finish(result, rows, period)


def _answer_period(conn, organization_id, result, targets, day: date):
    facts = []
    for ft in _PERIOD_BOUND:
        facts += [f for f in _load_facts(conn, organization_id, targets, ft) if f.get("effective_from")]
    if not facts:
        result["reason"] = "No policy period is stated in this entity's documents."
        return result
    periods = sorted({_period_key(f) for f in facts}, key=lambda k: k[0])
    chosen = next((k for k in periods if _covers(k, day)), periods[-1])
    rows = [f for f in facts if _period_key(f) == chosen]
    # one representative evidenced fact per document is enough to show where it was stated
    seen, rep = set(), []
    for r in rows:
        doc = str(r["_evidence"][0]["document_id"])
        if doc not in seen:
            seen.add(doc)
            rep.append(r)
    result["answer"] = f"{_iso(chosen[0])} to {_iso(chosen[1])}"
    result["values"] = [{"from": _iso(k[0]), "to": _iso(k[1])} for k in periods]
    result["answer_type"] = "deterministic"
    result = _finish(result, rep, chosen)
    # The period itself is read deterministically from the document text, so
    # its authority does not depend on the review status of the facts used.
    result["authoritative"] = True
    result["needs_review"] = False
    result["review_status"] = "deterministic"
    return result


# ---------------------------------------------------------------------------
# 5. Hybrid fallback: keyword retrieval + optional evidence-validated LLM
# ---------------------------------------------------------------------------

_STOP = set("a an the of to in on for is are was were what which who when where how does do did "
            "this that these those it its with by and or any from at be has have our my their there "
            "policy please tell me about".split())


def retrieve_passages(conn, organization_id: str, question: str, document_ids: list[str],
                      limit: int = 5) -> list[dict]:
    """Keyword retrieval over this tenant's documents: each line scored by how
    many of the question's content words it contains, returned with its
    neighbours, document and page. Deterministic and explainable."""
    terms = [t for t in _norm(question).split() if t not in _STOP and len(t) > 2]
    if not terms:
        return []
    hits = []
    for doc_id in document_ids:
        row = db.get_document_with_text(conn, doc_id, organization_id)
        if not row or not row.get("extracted_text"):
            continue
        page = None
        lines = row["extracted_text"].splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"\[Page (\d+)\]", line.strip())
            if m:
                page = int(m.group(1))
                continue
            words = set(_norm(line).split())
            score = sum(1 for t in terms if t in words)
            if score:
                ctx = " ".join(l.strip() for l in lines[max(0, i - 1): i + 2]
                               if l.strip() and not l.strip().startswith("[Page"))
                hits.append({"document_id": str(row["id"]), "document_filename": row["filename"],
                             "page": page, "text": ctx, "score": score / len(terms),
                             "retrieval": "keyword"})
    hits.sort(key=lambda h: -h["score"])
    out, seen = [], set()
    for h in hits:
        if h["text"] not in seen:
            seen.add(h["text"])
            out.append(h)
        if len(out) >= limit:
            break
    return out


_SYNTH_PROMPT = """You answer an insurance broker's question using ONLY the passages below.
Return JSON: {{"answer": string or null, "passage": number, "quote": string}}.
"quote" must be copied exactly from that passage and must contain every number in "answer".
If the passages do not answer the question, return {{"answer": null, "passage": 0, "quote": ""}}.

Question: {question}

Passages:
{passages}
"""


def synthesize(question: str, passages: list[dict]) -> dict:
    """Ask the local model, then VALIDATE: the quote must be in the cited
    passage and every number in the answer must appear in the quote. Any
    failure -> no answer. Returns {answer, passage, quote, rejected_reason}."""
    import property_policy_intelligence as ppi
    from llm import LLMError, generate_json

    numbered = "\n".join(f"[{i + 1}] ({p['document_filename']}, page {p['page']}) {p['text']}"
                         for i, p in enumerate(passages))
    try:
        raw = generate_json(_SYNTH_PROMPT.format(question=question, passages=numbered))
    except LLMError as exc:
        return {"answer": None, "rejected_reason": f"model unavailable: {exc}"}
    answer = (raw or {}).get("answer")
    quote = (raw or {}).get("quote") or ""
    try:
        idx = int((raw or {}).get("passage") or 0) - 1
    except (TypeError, ValueError):
        idx = -1
    if not answer:
        return {"answer": None, "rejected_reason": "model found no answer in the passages"}
    if not (0 <= idx < len(passages)) or not quote.strip():
        return {"answer": None, "rejected_reason": "model gave no valid supporting quote"}
    if not ppi.verify_snippet(quote, passages[idx]["text"]):
        return {"answer": None, "rejected_reason": "model's quote is not in the cited passage"}
    passage_text = passages[idx]["text"]
    for num in re.findall(r"\d[\d,]*(?:\.\d+)?", f"{answer} {quote}"):
        if not ppi.numeric_value_grounded(num, quote) or not ppi.numeric_value_grounded(num, passage_text):
            return {"answer": None,
                    "rejected_reason": f"model's quote is not in the cited passage (number {num} is not there)"}
    return {"answer": str(answer), "passage": passages[idx], "quote": quote, "rejected_reason": None}


# ---------------------------------------------------------------------------
# 5b. Phase 1C policy intents (v0.11.0) — all deterministic, all evidence-backed
# ---------------------------------------------------------------------------

def _coverages_for(conn, organization_id, entity):
    policies, edges = _targets(conn, organization_id, entity, "policy")
    covs = []
    for p in policies:
        for r in _relationships.find_relationships(conn, organization_id, str(p["id"]), "provides", "from"):
            c = db.get_entity(conn, str(r["to_entity_id"]), organization_id)
            if c is not None and c["entity_type"] == "coverage":
                covs.append(c)
    return policies, edges, covs


def _cov_name(c) -> str:
    return (c.get("attributes") or {}).get("coverage") or c["name"]


def _policy_intent(conn, organization_id, result, entity, question, interp, day):
    """Returns a finished result for a Phase 1C intent, or None to fall through."""
    intent, q = interp["intent"], _norm(question)
    policies, edges, covs = _coverages_for(conn, organization_id, entity)

    def use(targets_extra):
        result["relationships_used"] = edges
        for t in policies + targets_extra:
            if str(t["id"]) != str(entity["id"]) and not any(
                    e["entity_id"] == str(t["id"]) for e in result["entities_used"]):
                result["entities_used"].append({"entity_id": str(t["id"]), "entity_type": t["entity_type"],
                                                "name": t["name"]})

    # A limit / sum insured question that names one of the policy's coverages
    # is answered from THAT coverage's own limit (never the policy-wide one).
    if intent in ("fact", "unknown") and covs and (
            interp.get("fact_type") in ("policy.coverage_limit", "property.sum_insured", None)
            and re.search(r"\b(limit|sum insured|insured for|cover amount)\b", q)):
        named = [c for c in covs if _norm(_cov_name(c)) and f" {_norm(_cov_name(c))} " in f" {q} "]
        if len(named) == 1:
            c = named[0]
            use([c])
            interp.update(intent="fact", fact_type="coverage.limit", coverage=_cov_name(c))
            return _answer_fact(conn, organization_id, result, [c], "coverage.limit", interp["mode"], day)
    if intent in ("fact", "unknown"):
        return None
    if not policies:
        result["reason"] = "No policy is linked to this entity."
        return result
    use([])

    if intent == "coverage_rank":
        if not covs:
            result["reason"] = "No coverage with its own stated limit is recorded for this policy."
            return result
        rows, problems = [], []
        for c in covs:
            r = _answer_fact(conn, organization_id, _empty("", {}), [c], "coverage.limit", "current", day)
            if r["answer"] is None:
                problems.append(f"{_cov_name(c)}: {r['reason']}")
            else:
                rows.append((c, r))
        if problems:
            result["reason"] = "Cannot rank: " + "; ".join(problems)
            return result
        lowest = re.search(r"\b(lowest|smallest|minimum)\b", q) is not None
        rows.sort(key=lambda cr: _number({"value": cr[1]["answer"], "normalized_value":
                                          cr[1]["facts_used"][0]["normalized_value"]}) or 0, reverse=not lowest)
        top_c, top_r = rows[0]
        if len(rows) > 1 and rows[0][1]["answer"] == rows[1][1]["answer"]:
            result["reason"] = "Two coverages share the same limit; no single answer."
            return result
        facts = [f for _, r in rows for f in r["facts_used"]]
        result["answer"] = f"{_cov_name(top_c)} ({top_r['answer']})"
        result["values"] = [f"{_cov_name(c)}: {r['answer']}" for c, r in rows]
        result["answer_type"] = "deterministic"
        result["facts_used"] = facts
        result["evidence"] = [dict(e, fact_id=f["fact_id"]) for f in facts for e in f["evidence"]]
        return _finish_from_out(result, facts)

    if intent == "conflicts":
        import policy_intelligence as pi
        found = []
        for p in policies:
            found += pi.find_policy_conflicts(conn, organization_id, str(p["id"])) or []
        result["answer"] = [f"{c['coverage'] or c['fact_type']}: {' vs '.join(c['values'])}" for c in found]
        result["values"] = result["answer"]
        result["contradictions"] = found
        result["answer_type"] = "deterministic"
        facts = [f for c in found for f in c["facts"]]
        result["facts_used"] = facts
        result["evidence"] = [dict(e, fact_id=f["fact_id"]) for f in facts for e in f["evidence"]]
        result["reason"] = None if found else "No unresolved contradiction is recorded for this policy."
        out = _finish_from_out(result, facts)
        out["review_status"] = "contradiction" if found else "deterministic"
        out["authoritative"] = True       # the list of open conflicts is itself a recorded fact
        out["needs_review"] = bool(found)
        return out

    if intent == "endorsement_dates":
        refs = [f for f in _load_facts(conn, organization_id, policies, "policy.endorsement_ref")
                if f.get("valid_to") is None]
        periods = sorted({_period_key(f) for ft in _PERIOD_BOUND
                          for f in _load_facts(conn, organization_id, policies, ft) if f.get("effective_from")},
                         key=lambda k: k[0])
        cur = next((k for k in periods if _covers(k, day)), None)
        if cur is None:
            result["reason"] = f"No stated policy period covers {day.isoformat()}."
            return result
        inside = [f for f in refs if f.get("effective_from") and cur[0] <= f["effective_from"] <= cur[1]]
        undated = [f for f in refs if not f.get("effective_from")]
        result["answer"] = [f"{f['value']} (effective {_iso(f['effective_from'])})" for f in inside]
        result["values"] = result["answer"]
        result["answer_type"] = "deterministic"
        if undated:
            result["candidates"] = [_fact_out(f) for f in undated]
            result["reason"] = f"{len(undated)} endorsement(s) have no readable effective date and are not counted."
        elif not inside:
            result["reason"] = "No endorsement with a stated effective date falls in this policy period."
        return _finish(result, inside, cur)

    if intent == "policy_status":
        periods = sorted({_period_key(f) for ft in _PERIOD_BOUND
                          for f in _load_facts(conn, organization_id, policies, ft) if f.get("effective_from")},
                         key=lambda k: k[0])
        if not periods:
            result["reason"] = "No policy period is stated, so the status cannot be established."
            return result
        import policy_intelligence as pi
        status = pi._status(periods, day)
        rep = [f for ft in _PERIOD_BOUND for f in _load_facts(conn, organization_id, policies, ft)
               if f.get("effective_from")][:1]
        result["answer"] = f"{status} on {day.isoformat()}"
        result["values"] = [{"from": _iso(a), "to": _iso(b)} for a, b in periods]
        result["answer_type"] = "deterministic"
        out = _finish(result, rep)
        out.update(authoritative=True, needs_review=False, review_status="deterministic")
        return out

    if intent == "policy_changes":
        import policy_intelligence as pi
        cmp = pi.compare_policies(conn, organization_id, str(policies[0]["id"]), as_of=day)
        if cmp is None or cmp["status"] != "compared":
            result["reason"] = (cmp or {}).get("reason") or "Nothing to compare."
            return result
        material = [c for c in cmp["changes"] if not c["change_type"].endswith("_unchanged")]
        result["change"] = cmp
        result["answer"] = [_describe_change(c) for c in material] or ["no material change"]
        result["values"] = result["answer"]
        result["answer_type"] = "deterministic"
        ev = [e for c in material for side in ("old_evidence", "new_evidence") for e in c.get(side) or []]
        result["evidence"] = ev
        result["source_documents"] = sorted({e["document_filename"] for e in ev if e.get("document_filename")})
        result["source_pages"] = [{"document": d, "page": p} for d, p in
                                  sorted({(e["document_filename"], e["page"]) for e in ev},
                                         key=lambda x: (str(x[0]), x[1] or 0))]
        statuses = {c["review_status"] for c in material}
        result["review_status"] = ("contradiction" if "contradiction" in statuses else
                                   "verified" if statuses <= {"verified", "deterministic"} else "unverified")
        result["authoritative"] = result["review_status"] == "verified"
        result["needs_review"] = not result["authoritative"]
        result["contradictions"] = [c for c in material if c["change_type"] == "unresolved_contradiction"]
        return result
    return None


def _describe_change(c: dict) -> str:
    who = f" ({c['coverage']})" if c.get("coverage") else ""
    t = c["change_type"]
    if "old_value" in c and "new_value" in c:
        pct = f", {c['percentage_change']:+}%" if c.get("percentage_change") is not None else ""
        return f"{t}{who}: {c['old_value']} -> {c['new_value']}{pct}"
    if "new_value" in c:
        return f"{t}{who}: {c['new_value']}"
    if "old_value" in c:
        return f"{t}{who}: {c['old_value']}"
    return f"{t}{who}: {' / '.join(c.get('old_values', []))} vs {' / '.join(c.get('new_values', []))}"


def _finish_from_out(result: dict, facts_out: list[dict]) -> dict:
    """_finish for facts already in _fact_out shape."""
    ev = result["evidence"]
    result["source_documents"] = sorted({e["document_filename"] for e in ev if e.get("document_filename")})
    result["source_pages"] = [{"document": d, "page": p} for d, p in
                              sorted({(e["document_filename"], e["page"]) for e in ev},
                                     key=lambda x: (str(x[0]), x[1] or 0))]
    st = {f["status"] for f in facts_out}
    result["review_status"] = ("contradiction" if "contradiction_flagged" in st else
                               "verified" if st and st <= {"verified", "corrected"} else "unverified")
    result["authoritative"] = result["answer"] is not None and result["review_status"] == "verified"
    result["needs_review"] = not result["authoritative"]
    return result


# ---------------------------------------------------------------------------
# 6. Entry point
# ---------------------------------------------------------------------------

def query(conn, *, organization_id: str, question: str, entity_id: str | None = None,
          as_of: str | date | None = None, use_llm: bool = True) -> dict:
    interp = interpret(question)
    result = _empty(question, interp)
    day = (date.fromisoformat(interp["stated_date"]) if interp["stated_date"]
           else (as_of if isinstance(as_of, date) else date.fromisoformat(as_of)) if as_of
           else date.today())
    interp["as_of"] = day.isoformat()

    entity, candidates = _resolve_entity(conn, organization_id, question, entity_id)
    if entity is None:
        result["candidates"] = [{"entity_id": str(c["id"]), "entity_type": c["entity_type"],
                                 "name": c["name"]} for c in candidates]
        result["reason"] = ("Several entities match this question; choose one." if candidates
                            else "No entity named in the question was found; pass entity_id.")
        return result
    result["entities_used"] = [{"entity_id": str(entity["id"]), "entity_type": entity["entity_type"],
                                "name": entity["name"]}]

    if interp["intent"] in ("coverage_rank", "conflicts", "endorsement_dates", "policy_status",
                            "policy_changes") or interp["intent"] in ("fact", "unknown"):
        handled = _policy_intent(conn, organization_id, result, entity, question, interp, day)
        if handled is not None:
            return handled

    if interp["intent"] in ("fact", "policy_period"):
        kind = "policy" if interp["intent"] == "policy_period" else target_entity_kind(interp["fact_type"])
        targets, edges = _targets(conn, organization_id, entity, kind)
        result["relationships_used"] = edges
        for t in targets:
            if str(t["id"]) != str(entity["id"]):
                result["entities_used"].append({"entity_id": str(t["id"]),
                                                "entity_type": t["entity_type"], "name": t["name"]})
        if not targets:
            result["reason"] = f"No {kind} is linked to this entity."
            return result
        if interp["intent"] == "policy_period":
            return _answer_period(conn, organization_id, result, targets, day)
        result = _answer_fact(conn, organization_id, result, targets, interp["fact_type"],
                              interp["mode"], day)
        terms = _qualifier_terms(question, entity)
        interp["qualifier"] = terms
        if terms and interp["mode"] in ("current", "as_of", "previous"):
            result = _apply_qualifier(result, interp["fact_type"], terms)
        return result

    # Unknown question type: the Knowledge Core has no fact type for it.
    doc_ids = _documents_for(conn, organization_id, entity)
    passages = retrieve_passages(conn, organization_id, question, doc_ids)
    result["retrieved_passages"] = passages
    if not passages:
        result["reason"] = "The Knowledge Core has no fact for this and no passage matched."
        return result
    if not use_llm:
        result["reason"] = "Not a known fact type; passages retrieved for a person to read."
        return result
    s = synthesize(question, passages)
    if s["answer"] is None:
        result["reason"] = f"No evidence-backed answer: {s['rejected_reason']}."
        return result
    p = s["passage"]
    result.update(answer=s["answer"], answer_type="llm_synthesis", review_status="unverified",
                  authoritative=False, needs_review=True,
                  confidence={"type": "model_self_reported", "note": "model synthesis, quote verified"},
                  evidence=[{"document_id": p["document_id"], "document_filename": p["document_filename"],
                             "page": p["page"], "snippet": s["quote"], "source": "retrieval"}],
                  source_documents=[p["document_filename"]],
                  source_pages=[{"document": p["document_filename"], "page": p["page"]}],
                  reason="Answered by the local model from a retrieved passage; quote verified, not reviewed.")
    return result


def _documents_for(conn, organization_id: str, entity: dict) -> list[str]:
    """Documents that are evidence for this entity or the entities it links to."""
    ids = {str(entity["id"])}
    for r in _relationships.find_relationships(conn, organization_id, str(entity["id"]), None, "from"):
        ids.add(str(r["to_entity_id"]))
    docs = []
    for eid in ids:
        for f in db.list_facts_for_entity(conn, eid, organization_id, None, False):
            for e in db.get_evidence_for_fact(conn, str(f["id"]), organization_id):
                d = str(e["document_id"])
                if d not in docs:
                    docs.append(d)
    return docs
