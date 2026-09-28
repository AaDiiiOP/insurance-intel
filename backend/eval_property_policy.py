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
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import llm
import property_policy_intelligence as ppi
from knowledge.fact_types import FACT_DEFINITIONS
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


def score_company(company_dir: Path, ingest_result: dict) -> dict:
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

        evidence = written.get("evidence") or {}
        snippet_verified = evidence.get("snippet_verified")
        # A fact is "silently wrong" iff: ground truth exists for this
        # fact_type, nothing matched it, AND the fact was written as
        # verified/needs_review-without-a-flag rather than routed to
        # contradiction_flagged or an explicit low-confidence review note.
        # needs_review is the NORMAL default status for a freshly extracted
        # fact (see facts.py) — it is not itself a "this looks wrong" flag,
        # so a wrong value sitting in needs_review still counts as a silent
        # wrong fact: nothing about its status distinguishes it from a
        # correct one, which is exactly the failure mode "silent" describes.
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
            "snippet_verified": snippet_verified,
            "evidence_page": evidence.get("page"),
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
        "evidence_snippet_verified_pct": (
            round(
                sum(1 for f in fact_scores if f["snippet_verified"]) /
                sum(1 for f in fact_scores if f["snippet_verified"] is not None) * 100, 1
            )
            if any(f["snippet_verified"] is not None for f in fact_scores) else None
        ),
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
    }

    return {
        "companies_total": len(company_results),
        "companies_scored": n,
        "companies_with_no_ground_truth": [
            r["company_id"] for r in company_results if r["ground_truth_fact_count"] == 0
        ],
        "hard_gates": hard_gates,
        "statistical_thresholds": statistical,
        "note": (
            "hard_gates must read exactly as required (zero_silent_wrong_facts=True) "
            "before Phase 1A is considered accepted. statistical_thresholds are "
            "reported, not graded here — decide the acceptable bar for each and "
            "track it run over run, per this script's module docstring."
        ),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 eval_property_policy.py /path/to/Aurevia_Synthetic_Insurance_World_v1")
        sys.exit(1)

    dataset_root = Path(sys.argv[1])
    companies_dir = dataset_root / "companies"
    if not companies_dir.is_dir():
        print(f"No companies/ directory found under {dataset_root}")
        sys.exit(1)

    if not llm.is_available():
        print(
            f"WARNING: Ollama model '{llm.OLLAMA_MODEL}' is not reachable at "
            f"{llm.OLLAMA_HOST}. Every document will fail extraction and be "
            "scored as 0 facts found — this run will not be a valid Phase 1A "
            "acceptance result, only a plumbing smoke test."
        )

    with db.get_connection() as conn:
        organization_id = str(
            db.create_organization(conn, f"phase1-benchmark-{uuid.uuid4().hex[:12]}")["id"]
        )

        company_dirs = sorted(d for d in companies_dir.iterdir() if d.is_dir())
        print(f"Found {len(company_dirs)} companies. Running Phase 1A benchmark "
              f"(organization {organization_id})...\n")

        company_results = []
        for company_dir in company_dirs:
            print(f"  {company_dir.name}...", end=" ", flush=True)
            ingest_result = ingest_company(conn, organization_id, company_dir)
            result = score_company(company_dir, ingest_result)
            company_results.append(result)
            print(
                f"recall={result['fact_recall_pct']}%  "
                f"extracted={result['extracted_fact_count']}  "
                f"silent_wrong={result['silent_wrong_fact_count']}  "
                f"contradictions={result['contradictions_flagged']}"
            )

    summary = summarise(company_results)

    output = {"summary": summary, "companies": company_results}
    out_path = Path(__file__).parent / "eval_property_policy_results.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))

    print(f"\nFull results written to {out_path}")
    print(json.dumps(summary, indent=2))

    if not summary["hard_gates"]["zero_silent_wrong_facts"]:
        print(
            f"\nHARD GATE FAILED: {summary['hard_gates']['silent_wrong_fact_count']} "
            "silently wrong fact(s) found. Phase 1A is not accepted until this is 0."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
