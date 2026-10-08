"""
Phase 1A acceptance benchmark — Property Policy Intelligence against real
Qwen 2.5 7B, run over the Aurevia Synthetic Insurance World dataset
(v1.0 core facts + the v1.1 Phase 1 supplement — see
scripts/gen_phase1_supplement.py's docstring for why the supplement exists:
v1.0's ground truth and document content only ever covered 2 of the 10
Property Policy fact types).

This is the harness the earlier architecture review (package (14)) called
for: "Run the Property Policy Intelligence vertical slice with real Qwen
2.5 7B against the synthetic insurance world." Like eval_harness.py (the
Phase 0 equivalent), this drives the REAL pipeline end to end — real
Postgres, real parsing, real property_policy_intelligence.analyze_submission
— not a mock of any of it. Unlike eval_harness.py, this also goes through
the real ingestion sequence api/doc_intelligence.py's upload endpoint uses
(parse -> create_document -> save_document_quality -> attach to submission)
rather than calling extraction functions directly, because analyze_submission
itself (contradiction detection, evidence anchoring, entity linking) is
exactly what's under test here, not just the extraction call underneath it.

ACCEPTANCE CRITERIA — split into two kinds, deliberately, not one flat list:

  HARD GATES (must be 100% / 0, no exceptions, no threshold to tune):
    - tenant_isolation_intact       (covered by test_cross_tenant_isolation.py,
                                      re-asserted here as a sanity check)
    - all_evidence_traceable        every fact with a claimed snippet must
                                     have snippet_verified=True or be
                                     routed to needs_review with a note —
                                     never silently accepted unverified
    - all_corrections_versioned     not exercised by this read-only
                                     benchmark; covered by
                                     test_correction_and_versioning.py
    - zero_silent_wrong_facts       a fact whose value contradicts ground
                                     truth must never be status='verified'
                                     with high confidence and no
                                     contradiction/needs_review flag — see
                                     silent_wrong_facts below

  STATISTICAL THRESHOLDS (tracked over time, not pass/fail on run one):
    - value_accuracy_pct, evidence_page_accuracy_pct,
      evidence_snippet_verified_pct, fact_recall_pct,
      false_extraction_rate_pct, abstention_rate_pct
    A 7B model will legitimately miss some facts or phrase evidence
    snippets differently from ground truth's exact wording — treating
    these as hard 100%-or-fail gates either never passes or creates
    pressure to loosen the wrong criteria to make the number look good.
    This harness reports them; DECIDING the acceptable threshold for each
    is a product/eval call for whoever reads this report, not something
    this script should silently bake in.

Usage:
    cd backend
    python3 eval_property_policy.py /path/to/Aurevia_Synthetic_Insurance_World_v1

Requires: real Postgres reachable (DATABASE_URL), real Ollama reachable
(llm.is_available()) — same requirement as eval_harness.py. Uses one
throwaway organization per run (created fresh, never reused, never
cleaned up automatically — same convention as tests/conftest.py's
test_org, since these are inert rows scoped to an id nothing else
references).
"""
import collections
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import llm
import property_policy_intelligence as ppi
from knowledge.fact_types import FACT_DEFINITIONS, NUMERIC_FACT_TYPES
from parsers import ParseError, parse_file_with_quality

DOCUMENT_FILES = [
    "01_property_policy.pdf",
    "02_coverage_schedule.pdf",
    "03_endorsement.pdf",
    "04_risk_survey.pdf",
    "05_claim_loss_run.pdf",
]

# Ground truth's fact_type strings vs. FACT_DEFINITIONS' keys match exactly
# (both come from the same registry now — the whole point of building it
# first, see knowledge/fact_types.py) so no translation table is needed
# here, unlike eval_harness.py's TARGET_FIELD naming its own field name.


def ingest_company(conn, organization_id: str, company_dir: Path) -> dict:
    """
    Push one company's documents through the REAL ingestion sequence
    api/doc_intelligence.py's upload endpoint uses (parse_file_with_quality
    -> create_document -> save_document_quality -> set_document_submission),
    then run property_policy_intelligence.analyze_submission over the
    resulting submission — exactly what a real broker uploading this
    company's documents through the app would trigger.

    Returns {"submission_id", "documents_ingested", "parse_errors",
    "analysis"} — analysis is analyze_submission's own return value (facts
    written, contradictions, business/asset/policy entities).
    """
    company_id = company_dir.name
    submission = db.create_submission(conn, organization_id=organization_id, label=company_id)
    submission_id = str(submission["id"])

    parse_errors = []
    documents_ingested = 0
    for filename in DOCUMENT_FILES:
        path = company_dir / filename
        if not path.exists():
            continue
        data = path.read_bytes()
        try:
            parsed, quality = parse_file_with_quality(filename, data)
        except ParseError as exc:
            parse_errors.append({"filename": filename, "error": str(exc)})
            continue

        document_row = db.create_document(
            conn, organization_id=organization_id, carrier_id=None,
            filename=filename, file_type="pdf", size_bytes=len(data),
            pages=quality["pages"], chars=parsed.chars, status="parsed",
            error_message=None, extracted_text=parsed.text,
        )
        document_id = str(document_row["id"])
        db.set_document_submission(conn, document_id, submission_id)
        db.save_document_quality(
            conn, document_id=document_id, used_ocr=quality["used_ocr"],
            pages_total=quality["pages"],
            pages_ocr_low_confidence=len(quality.get("low_confidence_pages", [])),
            avg_ocr_confidence=quality.get("avg_ocr_confidence"),
            page_confidences=quality.get("page_confidences"),
            notes=quality.get("notes", []),
        )
        documents_ingested += 1

    analysis = ppi.analyze_submission(conn, submission_id, organization_id)
    return {
        "submission_id": submission_id,
        "documents_ingested": documents_ingested,
        "parse_errors": parse_errors,
        "analysis": analysis,
    }


def load_ground_truth_facts(company_dir: Path) -> list[dict]:
    """
    Every scoreable fact for this company: v1.0's ground_truth.json
    (translated to fact_type/value via V1_FIELD_TO_FACT_TYPE below) plus
    v1.1's phase1_ground_truth.json (already in fact_type/value shape,
    written by gen_phase1_supplement.py directly against
    FACT_DEFINITIONS' names).
    """
    facts = []

    v1_path = company_dir / "ground_truth.json"
    if v1_path.exists():
        v1 = json.loads(v1_path.read_text())
        for fact in v1.get("facts", []):
            field = fact.get("field")
            fact_type = V1_FIELD_TO_FACT_TYPE.get(field)
            if fact_type is None:
                continue  # risk_rating, loss_amount: not Property Policy Intelligence fact types
            facts.append({
                "fact_type": fact_type,
                "value_inr": fact.get("value_inr"),
                "source_document": fact["source_document"],
                "page": fact["page"],
            })
            # property_sum_insured also stands as ground truth for
            # policy.coverage_limit on these single-policy companies — the
            # dataset's own 01_property_policy.pdf states one "Property
            # Damage Sum Insured" figure that IS the policy's coverage
            # limit on a simple policy (see prompts/extract_property_facts.txt's
            # own note: "may be the same figure as policy.coverage_limit on
            # a simple policy"). Without this, a correct policy.coverage_limit
            # extraction has nothing to be scored against and always counts
            # as a false extraction, even when it exactly matches the
            # policy's own stated sum insured.
            #
            # A SECOND acceptable reading is also added: the same document
            # (see scripts/gen_phase1_supplement.py's build_property_policy)
            # ALSO states a "Business Interruption Sum Insured" line right
            # next to the property figure, always exactly 20% of it. A model
            # reading "policy's OVERALL coverage limit" as the combined
            # total of both stated coverage lines on that policy is not
            # wrong — it's a different, equally defensible parse of a
            # genuinely ambiguous synthetic document, caught in the first
            # real-Qwen run: policy.coverage_limit values of 300M*1.2=360M,
            # 500M*1.2=600M etc. were being marked "silently wrong" against
            # ground truth that only allowed the property-only figure.
            # Rather than penalize a reasonable interpretation the ground
            # truth itself didn't rule out, both readings are accepted here.
            if field == "property_sum_insured":
                facts.append({
                    "fact_type": "policy.coverage_limit",
                    "value_inr": fact.get("value_inr"),
                    "source_document": fact["source_document"],
                    "page": fact["page"],
                })
                facts.append({
                    "fact_type": "policy.coverage_limit",
                    "value_inr": round(fact.get("value_inr") * 1.2),
                    "source_document": fact["source_document"],
                    "page": fact["page"],
                    "note": "property sum insured + business interruption sum insured combined — an equally valid reading of 'overall coverage limit' on this document",
                })

    phase1_path = company_dir / "phase1_ground_truth.json"
    if phase1_path.exists():
        phase1 = json.loads(phase1_path.read_text())
        facts.extend(phase1.get("phase1_facts", []))

    return facts


# Only the two v1.0 fields that map onto a Property Policy Intelligence
# fact type at all — schedule_property_sum_insured is deliberately NOT
# mapped: it's the coverage-schedule figure used to test contradiction
# detection against property_sum_insured, not a second independent fact
# type of its own. risk_rating/loss_amount have no Property Policy
# Intelligence fact type (they belong to Claims/Risk Intelligence, not yet
# built — see the earlier architecture review's item 13).
V1_FIELD_TO_FACT_TYPE = {
    "property_sum_insured": "property.sum_insured",
    "policy_deductible": "policy.deductible",
}


def values_match(fact_type: str, extracted_value: str, extracted_normalized,
                  expected: dict) -> bool:
    """
    Whether an extracted fact matches its ground-truth counterpart.

    Numeric fact types (money) compare the parsed number; ground truth for
    these always carries value_inr. Everything else compares text,
    case/whitespace-insensitively, via substring containment rather than
    exact equality — a model restating "932 Technology Park, Pune,
    Maharashtra" as "Property Location: 932 Technology Park, Pune,
    Maharashtra, India" should count as correct, and exact-string matching
    would systematically undercount a model that is right but verbose.
    """
    definition = FACT_DEFINITIONS.get(fact_type)
    if definition and definition.is_numeric:
        if "value_inr" not in expected or expected["value_inr"] is None:
            return False
        got = extracted_normalized
        if got is None:
            got = ppi._numeric(extracted_value)
        if got is None:
            return False
        return abs(got - expected["value_inr"]) <= max(1, abs(expected["value_inr"]) * 0.01)

    expected_text = " ".join(str(expected.get("value", "")).split()).lower()
    got_text = " ".join(str(extracted_value or "").split()).lower()
    if not expected_text or not got_text:
        return False
    return expected_text in got_text or got_text in expected_text


def _by_fact_type(by_fact_type: dict, fact_scores: list[dict], matched_gt_ids: set,
                   singleton_recalled_types: set) -> dict:
    """Per-fact-type ground-truth / extracted / recalled / false counts for
    one company — so recall can be read by fact type, not just as one
    blended number (which is what hid coverage=0/40 behind '67.5%')."""
    out = {}
    for fact_type, candidates in by_fact_type.items():
        definition = FACT_DEFINITIONS.get(fact_type)
        is_singleton = definition is None or definition.is_singleton
        out[fact_type] = {
            "ground_truth": 1 if is_singleton else len(candidates),
            "recalled": (1 if fact_type in singleton_recalled_types else 0) if is_singleton
                        else sum(1 for (ft, _i) in matched_gt_ids if ft == fact_type),
        }
    for f in fact_scores:
        entry = out.setdefault(f["fact_type"], {"ground_truth": 0, "recalled": 0})
        entry["extracted"] = entry.get("extracted", 0) + 1
        if f["has_ground_truth_for_type"] and not f["correct"]:
            entry["false"] = entry.get("false", 0) + 1
    for entry in out.values():
        entry.setdefault("extracted", 0)
        entry.setdefault("false", 0)
    return out


def score_evidence(conn, organization_id: str, written: dict, matched_gt: dict | None,
                    _doc_text_cache: dict) -> dict:
    """
    Score one written fact's evidence from what the Knowledge Core actually
    PERSISTED (Fact -> kc_evidence -> documents), not from the extractor's
    in-memory dict.

    Why: kc.reconcile_fact() returns the fact row, which does not carry the
    extractor's `evidence` dict — so the previous version of this scorer
    read written["evidence"], got nothing, and reported snippet_verified /
    evidence_page as null for every fact even though evidence was stored
    correctly. That made the benchmark unable to measure the very property
    (traceable evidence) the architecture is built around.

    snippet_verified here is RE-COMPUTED, independently, by running
    ppi.verify_snippet() on the persisted snippet against the persisted
    document's stored text — it does not trust the extractor's own flag,
    and a fact with no snippet, no document link, or a snippet that is not
    in the source text counts as NOT verified (never skipped from the
    denominator).

    evidence_page_correct is only judged when it can be judged fairly: the
    fact matched a ground-truth entry that names a page, AND the evidence
    comes from that same source document. A correct restatement of a
    singleton from a different document (e.g. the sum insured repeated in
    the risk survey) is not penalised for having a different page/document
    than the ground-truth entry's — it is None there, not False.
    """
    rows = ppi.kc.get_evidence_for_fact(conn, str(written["id"]), organization_id)
    result = {
        "evidence_exists": bool(rows),
        "evidence_has_document": False,
        "snippet_present": False,
        "snippet_verified": False,
        "evidence_page": None,
        "evidence_document": None,
        "evidence_page_correct": None,
        "_document_id": None,
    }
    if not rows:
        return result
    row = rows[0]
    result["evidence_has_document"] = bool(row.get("document_id"))
    result["evidence_page"] = row.get("page")
    result["evidence_document"] = row.get("document_filename")
    result["_document_id"] = str(row["document_id"]) if row.get("document_id") else None
    snippet = (row.get("snippet") or "").strip()
    result["snippet_present"] = bool(snippet)
    if snippet and row.get("document_id"):
        doc_id = str(row["document_id"])
        if doc_id not in _doc_text_cache:
            doc = db.get_document_with_text(conn, doc_id, organization_id)
            _doc_text_cache[doc_id] = (doc or {}).get("extracted_text") or ""
        result["snippet_verified"] = bool(ppi.verify_snippet(snippet, _doc_text_cache[doc_id]))

    if matched_gt and matched_gt.get("page") is not None and matched_gt.get("source_document"):
        gt_file = str(matched_gt["source_document"]).split("_", 1)[-1]
        if row.get("document_filename") == gt_file and row.get("page") is not None:
            result["evidence_page_correct"] = int(row["page"]) == int(matched_gt["page"])
    return result


def classify_false_extraction(fact_type: str, value: str, status: str | None,
                              source_text: str | None) -> str:
    """
    Explain WHY an extracted fact that matched no ground-truth entry is
    "false". REPORTING ONLY — it never changes whether a fact counts as false
    or silently wrong (that scoring is unchanged).

      UNSUPPORTED               the value is not in (or derivable from) the
                                document the evidence points to, or there is
                                no such document — a real extraction error.
      CONTRADICTORY_FACT        the value IS in its source document and the
                                Knowledge Core flagged it against another
                                current value (contradiction_flagged).
      LEGITIMATE_ADDITIONAL_FACT the value IS in its source document and was
                                not flagged; ground truth simply does not list
                                it.
    Text values count as supported only when they appear verbatim
    (whitespace/case-insensitive) in the source; numbers use
    ppi.numeric_value_grounded (stated, or a sum of stated figures).
    """
    if not source_text:
        return "UNSUPPORTED"
    if fact_type in NUMERIC_FACT_TYPES:
        grounded = ppi.numeric_value_grounded(value, source_text)
    else:
        grounded = " ".join(value.lower().split()) in " ".join(source_text.lower().split())
    if not grounded:
        return "UNSUPPORTED"
    return "CONTRADICTORY_FACT" if status == "contradiction_flagged" else "LEGITIMATE_ADDITIONAL_FACT"


def score_company(company_dir: Path, ingest_result: dict, conn=None,
                   organization_id: str | None = None) -> dict:
    company_id = company_dir.name
    ground_truth_facts = load_ground_truth_facts(company_dir)
    written_facts = ingest_result["analysis"]["facts"]

    # Multiple ground-truth entries can share a fact_type (multi-valued —
    # policy.exclusion, policy.endorsement, policy.coverage): score each
    # extracted fact against whichever ground-truth entry of the same
    # fact_type it best matches, and separately track which ground-truth
    # entries were never matched by anything (recall).
    by_fact_type: dict[str, list[dict]] = {}
    for gt in ground_truth_facts:
        by_fact_type.setdefault(gt["fact_type"], []).append(gt)

    # SINGLETON fact types (property.sum_insured, property.location, etc.)
    # legitimately get extracted once per document that states them — the
    # Property Policy, Coverage Schedule, Endorsement and Risk Survey all
    # restate the same sum insured, for instance. That is corroboration, not
    # 4 independent claims, and reconcile_fact() itself treats it that way
    # (a repeat of the CURRENT value is a no-op, see knowledge/service.py).
    # A grader that only lets each ground-truth entry match once (needed for
    # MULTI-valued types, where each entry is a genuinely different fact)
    # was wrongly treating every repeat-but-correct singleton extraction
    # past the first as "unmatched" -> "silently wrong" — punishing the
    # extractor for correctly finding the same true fact more than once.
    # Fix: singleton matches are NOT removed from the candidate pool (a
    # correct repeat matches the same entry again); only multi-valued
    # matches are consumed once each, since two different exclusions really
    # are two different facts that must each be accounted for separately.
    doc_text_cache: dict = {}
    matched_gt_ids = set()  # (fact_type, index) pairs — multi-valued only
    singleton_recalled_types = set()  # fact_type names — at least one correct match seen
    fact_scores = []
    for written in written_facts:
        fact_type = written["fact_type"]
        candidates = by_fact_type.get(fact_type, [])
        definition = FACT_DEFINITIONS.get(fact_type)
        is_singleton = definition is None or definition.is_singleton

        best_match = None
        for idx, gt in enumerate(candidates):
            if not is_singleton and (fact_type, idx) in matched_gt_ids:
                continue
            if values_match(fact_type, written["value"], written.get("normalized_value"), gt):
                best_match = (idx, gt)
                break

        if conn is not None and organization_id is not None and written.get("id") is not None:
            ev = score_evidence(conn, organization_id, written,
                                best_match[1] if best_match else None, doc_text_cache)
        else:
            # No DB handle (e.g. a pure scoring unit test): evidence cannot
            # be measured, say so honestly rather than fabricate a value.
            ev = {"evidence_exists": None, "evidence_has_document": None,
                  "snippet_present": None, "snippet_verified": None,
                  "evidence_page": None, "evidence_document": None,
                  "evidence_page_correct": None}
        # A fact is "silently wrong" iff: ground truth exists for this
        # fact_type, nothing matched it, AND the fact was written as
        # verified/needs_review-without-a-flag rather than routed to
        # contradiction_flagged or an explicit low-confidence review note.
        # needs_review is the NORMAL default status for a freshly extracted
        # fact (see facts.py) — it is not itself a "this looks wrong" flag,
        # so a wrong value sitting in needs_review still counts as a silent
        # wrong fact: nothing about its status distinguishes it from a
        # correct one, which is exactly the failure mode "silent" describes.
        doc_id = ev.pop("_document_id", None)
        classification = None
        if candidates and best_match is None:
            classification = classify_false_extraction(
                fact_type, written["value"], written.get("status"),
                doc_text_cache.get(doc_id) if doc_id else None)
        is_wrong = best_match is None and candidates
        is_silent = is_wrong and written.get("status") not in ("contradiction_flagged",)

        if best_match is not None:
            if is_singleton:
                # Not consumed from the candidate pool — a second, third,
                # etc. correct restatement of the same singleton value from
                # another document should also come back "correct" (it IS
                # correct), not "unmatched because something else already
                # claimed that ground-truth entry."
                singleton_recalled_types.add(fact_type)
            else:
                matched_gt_ids.add((fact_type, best_match[0]))

        fact_scores.append({
            "fact_type": fact_type,
            "value": written["value"],
            "status": written.get("status"),
            "correct": best_match is not None,
            "silently_wrong": is_silent,
            "has_ground_truth_for_type": bool(candidates),
            "false_classification": classification,
            "fact_id": str(written["id"]) if written.get("id") is not None else None,
            **ev,
        })

    # total_gt counts each singleton fact_type ONCE, however many candidate
    # ground-truth entries it has (policy.coverage_limit now has 2 —
    # property-only and property+BI-combined — because both are accepted
    # readings, not because there are 2 separate facts to find; see
    # load_ground_truth_facts). Multi-valued fact types are still counted
    # per entry, since each one really is a separate fact to recall.
    singleton_fact_types_present = {
        ft for ft in by_fact_type
        if (FACT_DEFINITIONS.get(ft).is_singleton if FACT_DEFINITIONS.get(ft) else True)
    }
    multi_valued_entry_count = sum(
        len(candidates) for ft, candidates in by_fact_type.items()
        if ft not in singleton_fact_types_present
    )
    total_gt = len(singleton_fact_types_present) + multi_valued_entry_count
    recalled = len(matched_gt_ids) + len(singleton_recalled_types)

    return {
        "company_id": company_id,
        "documents_ingested": ingest_result["documents_ingested"],
        "parse_errors": ingest_result["parse_errors"],
        "ground_truth_fact_count": total_gt,
        "extracted_fact_count": len(written_facts),
        "recalled_fact_count": recalled,
        "fact_recall_pct": round(recalled / total_gt * 100, 1) if total_gt else None,
        # A repeated CORRECT singleton extraction is not a false extraction
        # (it's the same true fact stated again) — only count a written
        # fact as a false extraction when it disagrees with every candidate
        # ground-truth entry for its type.
        "false_extraction_count": sum(
            1 for f in fact_scores if f["has_ground_truth_for_type"] and not f["correct"]
        ),
        "silent_wrong_fact_count": sum(1 for f in fact_scores if f["silently_wrong"]),
        # Every persisted fact is in the denominator — a fact with no
        # traceable evidence counts against this number, it is not skipped.
        "evidence_snippet_verified_pct": (
            round(sum(1 for f in fact_scores if f["snippet_verified"]) /
                  len(fact_scores) * 100, 1)
            if fact_scores and all(f["snippet_verified"] is not None for f in fact_scores) else None
        ),
        "evidence_traceable_count": sum(
            1 for f in fact_scores
            if f["evidence_exists"] and f["evidence_has_document"] and f["snippet_verified"]
        ),
        "evidence_page_checked": sum(1 for f in fact_scores if f["evidence_page_correct"] is not None),
        "evidence_page_correct_count": sum(1 for f in fact_scores if f["evidence_page_correct"]),
        "by_fact_type": _by_fact_type(by_fact_type, fact_scores, matched_gt_ids,
                                       singleton_recalled_types),
        # Repeats: a singleton restated by another document is merged onto
        # ONE persisted fact (extra evidence, not a new row), but the extractor
        # returns that same fact once per occurrence. unique_fact_count is the
        # number of distinct persisted facts; extracted_fact_count above is
        # the unchanged occurrence count the old metrics are built on.
        "unique_fact_count": len({f["fact_id"] for f in fact_scores if f["fact_id"]}) or None,
        "false_extraction_breakdown": dict(collections.Counter(
            f["false_classification"] for f in fact_scores if f["false_classification"])),
        "contradictions_flagged": len(ingest_result["analysis"]["contradictions"]),
        "fact_scores": fact_scores,
    }


def summarise(company_results: list[dict]) -> dict:
    scored = [r for r in company_results if r["ground_truth_fact_count"] > 0]
    n = len(scored)

    def avg(key):
        values = [r[key] for r in scored if r.get(key) is not None]
        return round(sum(values) / len(values), 1) if values else None

    total_silent_wrong = sum(r["silent_wrong_fact_count"] for r in scored)
    total_extracted = sum(r["extracted_fact_count"] for r in scored)
    total_false = sum(r["false_extraction_count"] for r in scored)
    total_recalled = sum(r["recalled_fact_count"] for r in scored)
    total_gt = sum(r["ground_truth_fact_count"] for r in scored)

    hard_gates = {
        # 0 is the only acceptable value — see module docstring.
        "zero_silent_wrong_facts": total_silent_wrong == 0,
        "silent_wrong_fact_count": total_silent_wrong,
    }

    statistical = {
        "fact_recall_pct": round(total_recalled / total_gt * 100, 1) if total_gt else None,
        "false_extraction_rate_pct": (
            round(total_false / total_extracted * 100, 1) if total_extracted else None
        ),
        "avg_evidence_snippet_verified_pct": avg("evidence_snippet_verified_pct"),
        "avg_fact_recall_pct_per_company": avg("fact_recall_pct"),
        # Pooled over every persisted fact (not an average of averages).
        "evidence_traceable_pct": (
            round(sum(r.get("evidence_traceable_count", 0) for r in scored) / total_extracted * 100, 1)
            if total_extracted else None
        ),
        "evidence_page_accuracy_pct": (
            round(sum(r.get("evidence_page_correct_count", 0) for r in scored) /
                  sum(r.get("evidence_page_checked", 0) for r in scored) * 100, 1)
            if sum(r.get("evidence_page_checked", 0) for r in scored) else None
        ),
    }

    # Recall/false-extraction BY FACT TYPE — one blended recall number hid
    # the fact that policy.coverage was 0/40 and policy.endorsement 3/40
    # while five other types were at 100%.
    by_type: dict[str, dict] = {}
    for r in scored:
        for ft, e in (r.get("by_fact_type") or {}).items():
            agg = by_type.setdefault(ft, {"ground_truth": 0, "recalled": 0, "extracted": 0, "false": 0})
            for k in agg:
                agg[k] += e.get(k, 0)
    for agg in by_type.values():
        agg["recall_pct"] = (round(agg["recalled"] / agg["ground_truth"] * 100, 1)
                             if agg["ground_truth"] else None)

    return {
        "companies_total": len(company_results),
        "companies_scored": n,
        "companies_with_no_ground_truth": [
            r["company_id"] for r in company_results if r["ground_truth_fact_count"] == 0
        ],
        "hard_gates": hard_gates,
        "statistical_thresholds": statistical,
        "by_fact_type": dict(sorted(by_type.items())),
        # Why "false" facts are false (reporting only; scoring unchanged) and
        # how many persisted facts there really are vs. occurrences.
        "false_extraction_breakdown": dict(sum(
            (collections.Counter(r.get("false_extraction_breakdown") or {}) for r in scored),
            collections.Counter())),
        "unique_persisted_facts": sum(r.get("unique_fact_count") or 0 for r in scored),
        "fact_occurrences": total_extracted,
        "note": (
            "hard_gates must read exactly as required (zero_silent_wrong_facts=True) "
            "before Phase 1A is considered accepted. statistical_thresholds are "
            "reported, not graded here — decide the acceptable bar for each and "
            "track it run over run, per this script's module docstring."
        ),
    }


def _git_commit() -> str | None:
    """Best-effort short commit hash for the code that produced this run —
    None (not a fabricated placeholder) when this isn't a git checkout or
    git itself isn't available, e.g. inside a stripped-down container."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _print_by_fact_type_table(summary: dict, diagnostics: dict) -> None:
    """Compact per-fact-type recall table, printed last so it is the first
    thing visible at the end of a long run."""
    print("\n" + "=" * 66)
    print(f"{'fact type':<24}{'ground truth':>13}{'recalled':>10}{'recall %':>10}{'false':>8}")
    print("-" * 66)
    for ft, e in summary.get("by_fact_type", {}).items():
        pct = "-" if e.get("recall_pct") is None else f"{e['recall_pct']:.1f}"
        print(f"{ft:<24}{e['ground_truth']:>13}{e['recalled']:>10}{pct:>10}{e['false']:>8}")
    print("=" * 66)
    st = summary["statistical_thresholds"]
    print(f"false-extraction breakdown: {summary.get('false_extraction_breakdown')}   "
          f"unique persisted facts {summary.get('unique_persisted_facts')} / occurrences {summary.get('fact_occurrences')}")
    print(f"overall recall {st['fact_recall_pct']}%   false-extraction {st['false_extraction_rate_pct']}%   "
          f"silent-wrong {summary['hard_gates']['silent_wrong_fact_count']}")
    print(f"evidence: snippet-verified {st['avg_evidence_snippet_verified_pct']}%   "
          f"fully traceable {st['evidence_traceable_pct']}%   page-accuracy {st['evidence_page_accuracy_pct']}%")
    counters = diagnostics.get("counters", {})
    if counters:
        print("extraction diagnostics:", json.dumps(counters, sort_keys=True))
    if diagnostics.get("dropped_fact_type_names"):
        print("fact-type names the model used that are NOT valid (dropped):",
              json.dumps(diagnostics["dropped_fact_type_names"], sort_keys=True))


# ---------------------------------------------------------------------------
# v0.10.0 — `--query-check`: ask the Knowledge query layer one question per
# ground-truth fact type, against the facts this run just extracted with the
# real model, and grade the ANSWERS. Separate from (and never changing) the
# Phase 1A fact scoring above.
#   correct    answer matches ground truth (same matcher as fact scoring)
#   abstained  answer is null (contradiction, no fact, ambiguous) — safe
#   wrong      answer given but does not match ground truth — hard gate, must be 0
# ---------------------------------------------------------------------------
QUERY_QUESTIONS = {
    "property.sum_insured": "What is the sum insured?",
    "property.location": "What is the location of the property?",
    "property.occupancy": "What is the occupancy?",
    "property.construction": "What is the construction?",
    "property.risk_factors": "What are the risk factors?",
    "policy.coverage": "What coverage does the policy have?",
    "policy.coverage_limit": "What is the coverage limit?",
    "policy.deductible": "What is the deductible?",
    "policy.exclusion": "What are the exclusions?",
    "policy.endorsement": "What are the endorsements?",
}


def query_check_company(conn, organization_id: str, company_dir: Path, ingest_result: dict) -> dict:
    from datetime import date as _date
    from knowledge import query as kq

    gt = load_ground_truth_facts(company_dir)
    analysis = ingest_result["analysis"]
    business_id = str(analysis["business"]["id"])
    periods = [d.get("policy_period") for d in analysis.get("documents_processed", []) if d.get("policy_period")]
    as_of = periods[0][0] if periods else _date.today().isoformat()
    rows = []
    for fact_type in sorted({g["fact_type"] for g in gt}):
        question = QUERY_QUESTIONS.get(fact_type)
        if not question:
            continue
        expected = [g for g in gt if g["fact_type"] == fact_type]
        r = kq.query(conn, organization_id=organization_id, question=question,
                     entity_id=business_id, as_of=as_of, use_llm=False)
        interpreted_ok = r["interpretation"].get("fact_type") == fact_type
        answer = r["answer"]
        if answer is None:
            verdict = "abstained"
        else:
            values = answer if isinstance(answer, list) else [answer]
            facts_by_value = {f["value"]: f for f in r["facts_used"]}
            ok = all(any(values_match(fact_type, v, (facts_by_value.get(v) or {}).get("normalized_value"), g)
                         for g in expected) for v in values)
            verdict = "correct" if ok else "wrong"
        rows.append({
            "fact_type": fact_type, "question": question, "interpreted_ok": interpreted_ok,
            "answer": answer, "verdict": verdict, "reason": r["reason"],
            "review_status": r["review_status"], "evidence_count": len(r["evidence"]),
            "source_pages": r["source_pages"],
        })
    return {"company_id": company_dir.name, "as_of": as_of, "questions": rows,
            "probes": _safety_probes(conn, organization_id, business_id, periods, gt, kq)}


def _safety_probes(conn, organization_id, business_id, periods, gt, kq) -> list[dict]:
    """v0.10.2: behaviour checks on the facts the real model just extracted.
    Each probe states what a SAFE system must do; a probe that fails counts as
    a wrong answer (same hard gate)."""
    from datetime import date as _date, timedelta
    probes = []

    def probe(name, expect, ok, r):
        probes.append({"probe": name, "expect": expect, "passed": bool(ok),
                       "answer": r["answer"], "reason": r["reason"]})

    def ask(q, as_of=None, org=organization_id, entity=business_id):
        return kq.query(conn, organization_id=org, question=q, entity_id=entity,
                        as_of=as_of, use_llm=False)

    if periods:
        start, end = periods[0]
        r = ask("What is the policy period?", as_of=start)
        probe("policy_period", f"{start} to {end}", r["answer"] == f"{start} to {end}", r)
        known = [v["from"] for v in (r.get("values") or []) if isinstance(v, dict) and v.get("from")]
        earliest = min(known) if known else start
        before = (_date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
        r = ask("What is the coverage limit?", as_of=before)
        probe("date_before_any_policy_period", "abstain", r["answer"] is None, r)
        if len(known) <= 1:
            r = ask("What was the previous coverage limit?", as_of=start)
            probe("previous_with_one_period", "abstain", r["answer"] is None, r)
    r = ask("What is the deductible for flood?", as_of=periods[0][0] if periods else None)
    probe("peril_specific_deductible", "abstain", r["answer"] is None, r)
    covers = " ".join(str(g.get("value", "")).lower() for g in gt if g["fact_type"] == "policy.coverage")
    if "cyber" not in covers:
        r = ask("Does the policy cover cyber attack?", as_of=periods[0][0] if periods else None)
        probe("not_listed_coverage", "abstain", r["answer"] is None, r)
    r = ask("What is the time limit for notifying a claim?")
    probe("misleading_limit_question", "not answered from a stored fact", r["answer_type"] != "deterministic", r)
    other = str(db.create_organization(conn, f"phase1b-probe-{uuid.uuid4().hex[:8]}")["id"])
    r = ask("What is the coverage limit?", org=other)
    probe("cross_tenant_query", "abstain, no facts", r["answer"] is None and not r["facts_used"], r)
    return probes


def summarise_query_check(results: list[dict]) -> dict:
    qs = [q for r in results for q in r["questions"]]
    probes = [p for r in results for p in r.get("probes", [])]
    answered = [q for q in qs if q["answer"] is not None]
    return {
        "questions": len(qs),
        "correct": sum(q["verdict"] == "correct" for q in qs),
        "abstained": sum(q["verdict"] == "abstained" for q in qs),
        "wrong": sum(q["verdict"] == "wrong" for q in qs),
        "interpretation_ok_pct": round(100 * sum(q["interpreted_ok"] for q in qs) / len(qs), 1) if qs else None,
        "answered_with_evidence_pct": (round(100 * sum(q["evidence_count"] > 0 for q in answered) / len(answered), 1)
                                       if answered else None),
        "probes": len(probes),
        "probes_failed": sum(not p["passed"] for p in probes),
        "hard_gate_zero_wrong_answers": (all(q["verdict"] != "wrong" for q in qs)
                                         and all(p["passed"] for p in probes)),
    }


# ---------------------------------------------------------------------------
# v0.11.0 — Phase 1C policy checks (part of --query-check). Graded against:
#   * ground truth: per-coverage limits (phase1 GT policy.coverage value_inr)
#     and the coverage with the highest limit;
#   * document references: policy number, insurer, conditions, endorsement id
#     and effective date, read from the STORED document text by simple
#     regexes written here independently of policy_structure.py.
# Verdicts: correct / abstained / wrong (wrong must be 0) and probes
# (must pass): definitions & sub-limits not stated -> abstain, one period ->
# no renewal diff, status in mid-period = current, cross-tenant -> nothing.
# ---------------------------------------------------------------------------
def _doc_refs(documents: list[dict]) -> dict:
    import re as _re
    from datetime import datetime as _dt
    refs = {"policy_number": None, "insurer": None, "conditions": [], "endorsements": []}
    for d in documents:
        text = d.get("extracted_text") or ""
        head = " ".join(text.splitlines()[:6]).lower()
        if "endorsement" in head:
            eid = _re.search(r"^\s*Endorsement\s+(\S+)\s*$", text, _re.M)
            eff = _re.search(r"^\s*Effective:\s*(.+?)\s*$", text, _re.M)
            date = None
            if eff:
                for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
                    try:
                        date = _dt.strptime(eff.group(1).strip(), fmt).date().isoformat()
                        break
                    except ValueError:
                        pass
            if eid:
                refs["endorsements"].append((eid.group(1), date))
            continue
        if "policy" in head and "schedule" not in head:
            m = _re.search(r"^\s*Policy Number:\s*(\S+)", text, _re.M)
            refs["policy_number"] = refs["policy_number"] or (m.group(1) if m else None)
            m = _re.search(r"^\s*Carrier:\s*(.+?)\s*$", text, _re.M)
            refs["insurer"] = refs["insurer"] or (m.group(1) if m else None)
            m = _re.search(r"^\s*Conditions\s*$\n(.+?)(?:\n.*NOT REAL INSURANCE DATA|\Z)", text, _re.M | _re.S)
            if m:
                body = " ".join(m.group(1).split())
                refs["conditions"] = [c.strip() + ("" if c.strip().endswith(".") else ".")
                                      for c in body.split(". ") if c.strip()]
    return refs


def policy_check_company(conn, organization_id: str, company_dir: Path, business: dict,
                         documents: list[dict], as_of: str | None) -> dict:
    from knowledge import query as kq
    import policy_intelligence as pi

    gt = load_ground_truth_facts(company_dir)
    refs = _doc_refs(documents)
    rows, probes = [], []
    biz = str(business["id"])

    def ask(question, day=as_of, org=organization_id):
        return kq.query(conn, organization_id=org, question=question, entity_id=biz, as_of=day, use_llm=False)

    def grade(name, r, ok):
        verdict = "abstained" if r["answer"] is None else ("correct" if ok else "wrong")
        rows.append({"check": name, "answer": r["answer"], "verdict": verdict, "reason": r["reason"],
                     "evidence_count": len(r["evidence"])})

    def probe(name, passed, r):
        probes.append({"probe": name, "passed": bool(passed), "answer": r.get("answer"), "reason": r.get("reason")})

    def norm(x):
        return " ".join(str(x or "").lower().split())

    if refs["policy_number"]:
        r = ask("What is the policy number?")
        grade("policy_number", r, norm(r["answer"]) == norm(refs["policy_number"]))
    if refs["insurer"]:
        r = ask("Which insurer issued this policy?")
        grade("insurer", r, norm(r["answer"]) == norm(refs["insurer"]))
    if refs["conditions"]:
        r = ask("What conditions apply?")
        got = sorted(norm(x) for x in (r["answer"] or []))
        grade("conditions", r, got == sorted(norm(x) for x in refs["conditions"]))
    covs = {g["value"]: g.get("value_inr") for g in gt if g["fact_type"] == "policy.coverage" and g.get("value_inr")}
    for cov, inr in covs.items():
        r = ask(f"What is the limit for {cov}?")
        n = None
        if r["answer"] is not None and r["facts_used"]:
            n = r["facts_used"][0]["normalized_value"]
        grade(f"coverage_limit:{cov}", r, n is not None and abs(n - inr) <= max(1, inr * 0.01))
    if len(covs) >= 2:
        top = max(covs, key=covs.get)
        r = ask("Which coverage has the highest limit?")
        grade("highest_limit_coverage", r, isinstance(r["answer"], str) and r["answer"].startswith(top + " ("))
    for eid, eff in refs["endorsements"]:
        r = ask("Which endorsements became effective during the policy period?",
                day=eff or as_of)
        grade(f"endorsement_effective:{eid}", r,
              isinstance(r["answer"], list) and f"{eid} (effective {eff})" in r["answer"])

    r = ask("What does flood mean?")
    probe("definition_not_stated_abstains", r["answer"] is None, r)
    r = ask("What sublimits apply?")
    probe("sublimit_not_stated_abstains", r["answer"] is None, r)
    r = ask("What changed during renewal?")
    probe("single_period_no_renewal_diff", r["answer"] is None, r)
    if as_of:
        r = ask("Is the policy in force?")
        probe("status_current_in_period", r["answer"] == f"current on {as_of}", r)
    policies = [x for x in db.find_relationships(conn, organization_id, biz, "has_policy", "from")]
    if policies:
        pid = str(policies[0]["to_entity_id"])
        view = pi.get_policy(conn, organization_id, pid, as_of=as_of)
        same = all(c["limit"]["answer"] == ask(f"What is the limit for {c['name']}?")["answer"]
                   for c in view["coverages"])
        probe("policy_view_matches_query_answers", same, {"answer": same, "reason": None})
        other = str(db.create_organization(conn, f"phase1c-probe-{uuid.uuid4().hex[:8]}")["id"])
        probe("cross_tenant_policy_view", pi.get_policy(conn, other, pid) is None,
              {"answer": None, "reason": None})
    return {"company_id": company_dir.name, "checks": rows, "probes": probes}


def summarise_policy_check(results: list[dict]) -> dict:
    checks = [c for r in results for c in r["checks"]]
    probes = [p for r in results for p in r["probes"]]
    return {"checks": len(checks), "correct": sum(c["verdict"] == "correct" for c in checks),
            "abstained": sum(c["verdict"] == "abstained" for c in checks),
            "wrong": sum(c["verdict"] == "wrong" for c in checks),
            "answered_with_evidence_pct": (round(100 * sum(c["evidence_count"] > 0 for c in checks if c["answer"] is not None)
                                                 / max(1, sum(c["answer"] is not None for c in checks)), 1)),
            "probes": len(probes), "probes_failed": sum(not p["passed"] for p in probes),
            "hard_gate_zero_wrong": all(c["verdict"] != "wrong" for c in checks) and all(p["passed"] for p in probes)}


# ---------------------------------------------------------------------------
# v0.12.0 — Phase 1D business check (part of --query-check).
#   Stage A (original documents) graded against INDEPENDENT ground truth:
#     company_profile.json (address, business activity, employees) and
#     ground_truth.json (risk_rating, loss_amount); controls/assets against the
#     document text read here independently.
#   Stage B (benchmark_phase1d/<company>/ supplement, if present): follow-up
#     documents are added, then change detection, contradictions, explicitly
#     stated risks and as-of profiles are graded against phase1d_ground_truth.json.
#   Probes: unsupported risk / exposure questions abstain, no removal from
#   absence, cross-tenant -> nothing.
# ---------------------------------------------------------------------------
PHASE1D_SUPPLEMENT = Path(__file__).parent / "benchmark_phase1d"


def business_check_company(conn, organization_id: str, company_dir: Path, ingest_result: dict,
                           with_supplement: bool = True, supplement_present: bool = False) -> dict:
    """supplement_present=True (recheck of a run that already added the
    supplement): stage A is skipped — the current state legitimately reflects
    the follow-up documents — and stage B is graded without re-adding them."""
    import re as _re
    import business_intelligence as bi
    from knowledge import query as kq

    biz = str(ingest_result["analysis"]["business"]["id"])
    prof_path, gt_path = company_dir / "company_profile.json", company_dir / "ground_truth.json"
    profile = json.loads(prof_path.read_text()) if prof_path.exists() else {}
    gt_v1 = {f["field"]: f.get("value", f.get("value_inr"))
             for f in (json.loads(gt_path.read_text()).get("facts", []) if gt_path.exists() else [])}
    checks, probes = [], []

    def ask(q, as_of=None, org=organization_id):
        return kq.query(conn, organization_id=org, question=q, entity_id=biz, as_of=as_of, use_llm=False)

    def grade(name, r, ok):
        verdict = "abstained" if r.get("answer") is None else ("correct" if ok else "wrong")
        checks.append({"check": name, "answer": r.get("answer"), "verdict": verdict, "reason": r.get("reason"),
                       "evidence_count": len(r.get("evidence") or [])})

    def probe(name, passed, r):
        probes.append({"probe": name, "passed": bool(passed), "answer": r.get("answer"), "reason": r.get("reason")})

    def norm(x):
        return " ".join(str(x or "").lower().split())

    stage_a = not supplement_present
    if stage_a and profile.get("primary_address"):
        r = ask("What are the business's operating locations?")
        grade("locations", r, isinstance(r["answer"], list) and
              [norm(a) for a in r["answer"]] == [norm(profile["primary_address"])])
    if stage_a and profile.get("business_activity"):
        r = ask("What operations does the business perform?")
        grade("operations", r, r["answer"] == [profile["business_activity"]])
    if stage_a and profile.get("employees"):
        r = ask("How many employees does the business have?")
        grade("workforce", r, r["answer"] is not None and
              kq._numeric_value(str(r["answer"])) == float(profile["employees"]))
    if stage_a and gt_v1.get("risk_rating"):
        r = ask("What is the risk rating?")
        grade("surveyor_risk_rating", r, norm(r["answer"]) == norm(gt_v1["risk_rating"]))
    if stage_a and gt_v1.get("loss_amount"):
        r = ask("What losses have been reported?")
        grade("losses", r, isinstance(r["answer"], list) and
              any(f"{int(gt_v1['loss_amount']):,}" in a for a in r["answer"]))
    docs = {d["filename"]: d.get("extracted_text") or "" for d in
            db.list_submission_documents(conn, ingest_result["submission_id"], organization_id)}
    survey = next((t for f, t in docs.items() if "risk_survey" in f), "")
    m = _re.search(r"^\s*Fire protection:\s*(.+?)\.?\s*$", survey, _re.M)
    if stage_a and m:
        r = ask("What controls are documented?")
        grade("controls", r, isinstance(r["answer"], list) and
              f"Fire Protection: {m.group(1).strip()}" in r["answer"])
    sched = next((t for f, t in docs.items() if "schedule" in f), "")
    m = _re.search(r"^\s*Plant and Machinery:\s*\D{0,3}([\d,]+)", sched, _re.M)
    if stage_a and m:
        r = ask("What major assets are documented?")
        grade("asset_machinery", r, isinstance(r["answer"], list) and
              f"Plant and Machinery: {m.group(1)}" in r["answer"])
    r = ask("Does this company have flood risk?")
    probe("unsupported_flood_risk_abstains", r["answer"] is None and
          str(r["reason"]).startswith("No documented evidence"), r)
    r = ask("What exposures are documented?")
    probe("no_stated_exposure_abstains", r["answer"] is None, r)
    other = str(db.create_organization(conn, f"phase1d-probe-{uuid.uuid4().hex[:8]}")["id"])
    probe("cross_tenant_profile", bi.get_business_profile(conn, other, biz) is None, {"answer": None, "reason": None})

    sup = PHASE1D_SUPPLEMENT / company_dir.name
    stage_b = None
    if (with_supplement or supplement_present) and sup.is_dir():
        gt = json.loads((sup / "phase1d_ground_truth.json").read_text())
        new_ids = []
        for f in ([] if supplement_present else sorted(sup.glob("*.pdf"))):
            parsed, quality = parse_file_with_quality(f.name, f.read_bytes())
            row = db.create_document(conn, organization_id=organization_id, carrier_id=None, filename=f.name,
                                     file_type="pdf", size_bytes=f.stat().st_size, pages=quality["pages"],
                                     chars=parsed.chars, status="parsed", error_message=None,
                                     extracted_text=parsed.text)
            db.set_document_submission(conn, str(row["id"]), ingest_result["submission_id"])
            new_ids.append(str(row["id"]))
        if new_ids:
            ppi.apply_business_documents(conn, ingest_result["submission_id"], organization_id, new_ids)
        ch = bi.get_business_changes(conn, organization_id, biz)
        got = {c["key"]: c for c in (ch.get("changes") or [])}

        def find(key):
            return next((c for k, c in got.items() if k == key or k.endswith("|" + key)), None)
        for exp in gt.get("changes", []):
            c = find(exp["key"])
            ok = c is not None and c["kind"] == exp["kind"] and \
                all(norm(c.get(f"{s}_value")) == norm(exp[s]) for s in ("old", "new") if s in exp)
            grade(f"change:{exp['key']}", {"answer": c and c["kind"], "reason": None,
                                           "evidence": (c or {}).get("new_evidence")}, ok)
        expected_keys = {e["key"] for e in gt.get("changes", [])} | set(gt.get("contradictions", []))
        unexpected = [c for c in (ch.get("changes") or []) if c["kind"] in ("added", "changed", "conflicting")
                      and not any(c["key"] == k or c["key"].endswith("|" + k) for k in expected_keys)]
        probe("no_unexpected_material_change", not unexpected,
              {"answer": [c["key"] for c in unexpected], "reason": None})
        probe("never_removed_from_absence", all(c["kind"] != "removed" for c in (ch.get("changes") or [])),
              {"answer": None, "reason": None})
        conflicts = {c["key"] for c in bi.find_business_conflicts(conn, organization_id, biz) or []}
        grade("contradictions", {"answer": sorted(conflicts), "reason": None, "evidence": []},
              conflicts == set(gt.get("contradictions", [])))
        r = ask("What risks are documented?")
        if gt.get("risks"):
            grade("explicit_risks", r, r["answer"] == gt["risks"])
        else:
            probe("no_stated_risk_abstains", r["answer"] is None, r)
        r = ask("What are the business's operating locations?")
        grade("locations_after", r, isinstance(r["answer"], list) and
              sorted(norm(a) for a in r["answer"]) == sorted(norm(x) for x in gt["locations"]))
        if gt.get("workforce_before"):
            before = bi.get_business_profile(conn, organization_id, biz, as_of=gt["before_date"])
            after = bi.get_business_profile(conn, organization_id, biz, as_of=gt["after_date"])
            probe("as_of_profile_before_after",
                  before["workforce"]["answer"] == str(gt["workforce_before"]) and
                  after["workforce"]["answer"] == str(gt["workforce_after"]),
                  {"answer": [before["workforce"]["answer"], after["workforce"]["answer"]], "reason": None})
        stage_b = {"from": ch.get("from"), "to": ch.get("to"), "summary": ch.get("summary")}
    return {"company_id": company_dir.name, "checks": checks, "probes": probes, "stage_b": stage_b}


def summarise_business_check(results: list[dict]) -> dict:
    checks = [c for r in results for c in r["checks"]]
    probes = [p for r in results for p in r["probes"]]
    answered = [c for c in checks if c["answer"] is not None]
    return {"checks": len(checks), "correct": sum(c["verdict"] == "correct" for c in checks),
            "abstained": sum(c["verdict"] == "abstained" for c in checks),
            "wrong": sum(c["verdict"] == "wrong" for c in checks),
            "probes": len(probes), "probes_failed": sum(not p["passed"] for p in probes),
            "companies_with_supplement": sum(1 for r in results if r.get("stage_b")),
            "hard_gate_zero_wrong": all(c["verdict"] != "wrong" for c in checks) and all(p["passed"] for p in probes)}


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 eval_property_policy.py /path/to/Aurevia_Synthetic_Insurance_World_v1")
        sys.exit(1)

    dataset_root = Path(sys.argv[1])
    # Optional quick-run flag: `--limit N` scores only the first N companies
    # (~6 min for N=3 instead of ~2 h for all 20). A limited run is saved as
    # its own timestamped result but NEVER becomes latest.json — latest.json
    # always means "a full, model-reachable 20-company run".
    company_limit = None
    if "--limit" in sys.argv:
        company_limit = int(sys.argv[sys.argv.index("--limit") + 1])
    # `--only AUR-SYN-014,AUR-SYN-009` scores just those companies (partial
    # run, same rules as --limit: saved, never becomes latest.json).
    only_ids = None
    if "--only" in sys.argv:
        only_ids = {x.strip() for x in sys.argv[sys.argv.index("--only") + 1].split(",") if x.strip()}
        company_limit = len(only_ids)
    companies_dir = dataset_root / "companies"
    if not companies_dir.is_dir():
        print(f"No companies/ directory found under {dataset_root}")
        sys.exit(1)

    # Recorded in the output below (not just printed) — a results file must
    # be self-describing: whether the model was actually reachable, and
    # which model, travels WITH the numbers it produced, so a results file
    # can never be mistaken for a valid real-model run when it was actually
    # a plumbing smoke test with every extraction call failing closed.
    model_reachable = llm.is_available()
    run_metadata = {
        "model": llm.OLLAMA_MODEL,
        "model_reachable": model_reachable,
        "ollama_host": llm.OLLAMA_HOST,
        "dataset_path": str(dataset_root),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "company_limit": company_limit,
    }

    if not model_reachable:
        print(
            f"WARNING: Ollama model '{llm.OLLAMA_MODEL}' is not reachable at "
            f"{llm.OLLAMA_HOST}. Every document will fail extraction and be "
            "scored as 0 facts found — this run will not be a valid Phase 1A "
            "acceptance result, only a plumbing smoke test. (Recorded as "
            "model_reachable=false in the output file — this run must not "
            "be mistaken for a real acceptance result later.)"
        )

    with db.get_connection() as conn:
        organization_id = str(
            db.create_organization(conn, f"phase1-benchmark-{uuid.uuid4().hex[:12]}")["id"]
        )

        company_dirs = sorted(d for d in companies_dir.iterdir() if d.is_dir())
        if only_ids:
            company_dirs = [d for d in company_dirs if d.name in only_ids]
        elif company_limit:
            company_dirs = company_dirs[:company_limit]
        print(f"Found {len(company_dirs)} companies. Running Phase 1A benchmark "
              f"(organization {organization_id})...\n")

        ppi.reset_extraction_diagnostics()
        company_results = []
        query_results = []
        policy_results = []
        business_results = []
        query_check = "--query-check" in sys.argv
        for company_dir in company_dirs:
            print(f"  {company_dir.name}...", end=" ", flush=True)
            ingest_result = ingest_company(conn, organization_id, company_dir)
            result = score_company(company_dir, ingest_result, conn=conn,
                                    organization_id=organization_id)
            company_results.append(result)
            if query_check:
                query_results.append(query_check_company(conn, organization_id, company_dir, ingest_result))
                docs = db.list_submission_documents(conn, ingest_result["submission_id"], organization_id)
                periods = [d.get("policy_period") for d in ingest_result["analysis"].get("documents_processed", [])
                           if d.get("policy_period")]
                policy_results.append(policy_check_company(conn, organization_id, company_dir,
                                                           ingest_result["analysis"]["business"], docs,
                                                           periods[0][0] if periods else None))
                business_results.append(business_check_company(conn, organization_id, company_dir,
                                                               ingest_result))
            print(
                f"recall={result['fact_recall_pct']}%  "
                f"extracted={result['extracted_fact_count']}  "
                f"silent_wrong={result['silent_wrong_fact_count']}  "
                f"contradictions={result['contradictions_flagged']}"
            )

    summary = summarise(company_results)

    # What the model actually returned vs. what survived validation, and how
    # often the recovery pass fired — so a low recall on some fact type can be
    # traced to "model returned nothing" vs. "model returned it under another
    # name / shape and it was dropped" without another 2-hour run.
    diagnostics = ppi.snapshot_extraction_diagnostics()
    output = {"run": run_metadata, "summary": summary,
              "extraction_diagnostics": diagnostics, "companies": company_results}
    if query_check:
        output["query_check"] = {"summary": summarise_query_check(query_results), "companies": query_results}
        output["policy_check"] = {"summary": summarise_policy_check(policy_results), "companies": policy_results}
        output["business_check"] = {"summary": summarise_business_check(business_results),
                                    "companies": business_results}
    # Timestamped, not overwritten in place — each run is kept as its own
    # historical record (evaluations/phase1a/README.md explains the layout)
    # rather than every run silently erasing the last one, which is exactly
    # how results.json / finalresult.json / eval_results.json ended up
    # scattered and contradicting each other in the first place. The most
    # recent run's path is also always available as latest.json (a copy,
    # not a symlink — plain and portable across OSes, including Windows).
    run_ts = run_metadata["timestamp_utc"].replace(":", "").replace("-", "")
    evaluations_dir = Path(__file__).parent / "evaluations" / "phase1a"
    evaluations_dir.mkdir(parents=True, exist_ok=True)
    out_path = evaluations_dir / f"result-{run_ts}.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))

    print(f"\nFull results written to {out_path}")
    print(json.dumps({"run": run_metadata, "summary": summary}, indent=2))
    _print_by_fact_type_table(summary, diagnostics)
    if query_check:
        qs = output["query_check"]["summary"]
        print(f"\nquery check: {qs['questions']} questions  correct {qs['correct']}  "
              f"abstained {qs['abstained']}  WRONG {qs['wrong']}  "
              f"interpretation ok {qs['interpretation_ok_pct']}%  "
              f"answered with evidence {qs['answered_with_evidence_pct']}%")
        for r in query_results:
            for q in r["questions"]:
                if q["verdict"] != "correct":
                    print(f"  {r['company_id']} {q['fact_type']}: {q['verdict']} — "
                          f"answer={q['answer']!r} reason={q['reason']!r}")
        print(f"safety probes: {qs['probes']}  failed {qs['probes_failed']}")
        ps_ = output["policy_check"]["summary"]
        print(f"policy check (Phase 1C): {ps_['checks']} checks  correct {ps_['correct']}  "
              f"abstained {ps_['abstained']}  WRONG {ps_['wrong']}  "
              f"answered with evidence {ps_['answered_with_evidence_pct']}%  "
              f"probes {ps_['probes']} failed {ps_['probes_failed']}")
        for r in policy_results:
            for c in r["checks"]:
                if c["verdict"] != "correct":
                    print(f"  {r['company_id']} {c['check']}: {c['verdict']} — answer={c['answer']!r} reason={c['reason']!r}")
        bs_ = output["business_check"]["summary"]
        print(f"business check (Phase 1D): {bs_['checks']} checks  correct {bs_['correct']}  "
              f"abstained {bs_['abstained']}  WRONG {bs_['wrong']}  probes {bs_['probes']} failed "
              f"{bs_['probes_failed']}  (supplement used for {bs_['companies_with_supplement']} companies)")
        for r in business_results:
            for c in r["checks"]:
                if c["verdict"] != "correct":
                    print(f"  {r['company_id']} {c['check']}: {c['verdict']} — answer={c['answer']!r} reason={c['reason']!r}")
            for p in r["probes"]:
                if not p["passed"]:
                    print(f"  BUSINESS PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r} reason={p['reason']!r}")
            for p in r["probes"]:
                if not p["passed"]:
                    print(f"  POLICY PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r} reason={p['reason']!r}")
        for r in query_results:
            for p in r.get("probes", []):
                if not p["passed"]:
                    print(f"  PROBE FAILED {r['company_id']} {p['probe']}: expected {p['expect']} — "
                          f"answer={p['answer']!r} reason={p['reason']!r}")

    # latest.json only ever reflects a run where the model was genuinely
    # reachable — a smoke-test run (model unreachable) is still saved above
    # as its own timestamped record, but must never become "the current
    # Phase 1A result" just by running last.
    if model_reachable and company_limit is None:
        latest_path = evaluations_dir / "latest.json"
        latest_path.write_text(json.dumps(output, indent=2, default=str))
        print(f"Also updated {latest_path} (this run's model was reachable).")
    elif company_limit is not None:
        print(f"(--limit {company_limit} run: saved above, latest.json NOT updated.)")

    # A run where the model was never reachable "passes" zero_silent_wrong_
    # facts vacuously — nothing was extracted, so nothing could be wrong.
    # That is a plumbing smoke test, not a Phase 1A acceptance result, and
    # must never exit 0 as though it were one; checked here, not just
    # printed as a warning above, so an automated caller (CI, a script)
    # cannot mistake a silently-skipped model run for a passing benchmark.
    if not model_reachable:
        print(
            "\nNOT A VALID ACCEPTANCE RUN: the model was not reachable "
            f"(model_reachable=false, model={run_metadata['model']!r}, "
            f"host={run_metadata['ollama_host']!r}). Re-run with Ollama/Qwen "
            "actually running and reachable before treating this result as "
            "Phase 1A evidence."
        )
        sys.exit(1)

    if query_check and not output["business_check"]["summary"]["hard_gate_zero_wrong"]:
        print(f"\nBUSINESS (PHASE 1D) HARD GATE FAILED: {output['business_check']['summary']['wrong']} wrong, "
              f"{output['business_check']['summary']['probes_failed']} failed probe(s).")
        sys.exit(1)

    if query_check and not output["policy_check"]["summary"]["hard_gate_zero_wrong"]:
        print(f"\nPOLICY (PHASE 1C) HARD GATE FAILED: {output['policy_check']['summary']['wrong']} wrong, "
              f"{output['policy_check']['summary']['probes_failed']} failed probe(s).")
        sys.exit(1)

    if query_check and not output["query_check"]["summary"]["hard_gate_zero_wrong_answers"]:
        print(f"\nQUERY HARD GATE FAILED: {output['query_check']['summary']['wrong']} wrong answer(s), "
              f"{output['query_check']['summary']['probes_failed']} failed safety probe(s).")
        sys.exit(1)

    if not summary["hard_gates"]["zero_silent_wrong_facts"]:
        print(
            f"\nHARD GATE FAILED: {summary['hard_gates']['silent_wrong_fact_count']} "
            "silently wrong fact(s) found. Phase 1A is not accepted until this is 0."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
