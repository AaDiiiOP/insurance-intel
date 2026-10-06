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
    ("property.sum_insured", ("sum insured", "insured value", "declared value", "total insured")),
    ("policy.coverage_limit", ("coverage limit", "limit of indemnity", "limit of liability", "cover limit", "limit")),
    ("policy.deductible", ("deductible", "excess")),
    ("policy.exclusion", ("exclusion", "excluded", "not covered")),
    ("policy.endorsement", ("endorsement",)),
    ("property.risk_factors", ("risk factor", "hazard")),
    ("property.occupancy", ("occupancy", "used for", "use of the premises", "business activity")),
    ("property.construction", ("construction", "built", "building material")),
    ("property.location", ("location", "address", "situated", "where is")),
    ("policy.coverage", ("coverage", "covered", "cover", "perils", "insured against")),
]
_PERIOD_PHRASES = ("policy period", "period of insurance", "policy term", "valid until",
                   "valid from", "expiry", "expire", "when does the policy", "renewal date")
_CHANGE_PHRASES = ("change", "changed", "difference", "differ", "compare", "increase",
                   "decrease", "went up", "went down", "added", "removed", "dropped")
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
            fact_type = ft
            break

    stated_date = None
    m = re.search(ppi._DATE_PAT, question or "")
    if m:
        stated_date = ppi._parse_date(m.group(0))

    if any(_contains_phrase(q, p) for p in _PERIOD_PHRASES):
        intent = "policy_period"
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
    elif undated_open and mode != "as_of":
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
    for ft in ("policy.coverage", "policy.coverage_limit", "policy.deductible",
               "property.sum_insured", "policy.exclusion", "policy.endorsement"):
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
        return _answer_fact(conn, organization_id, result, targets, interp["fact_type"],
                            interp["mode"], day)

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
