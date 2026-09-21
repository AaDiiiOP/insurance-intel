"""
Phase 0 evaluation harness — run against Aurevia Synthetic Insurance World v1.

Not part of the running app. A standalone script: point it at the dataset,
it drives the real pipeline (parsers.parse_file_with_quality ->
doc_intelligence.extract_coverage_limit, the same functions main.py's
/doc-intelligence/upload endpoint calls) against every document, compares
the result to ground_truth.json, and writes one report.

This exists because "it works on my test PDF" and "it works on 20 real
documents with known answers" are different claims, and only the second one
is worth showing a broker. Every number below is computed from an actual
pipeline run, not estimated.

Usage:
    cd backend
    python3 eval_harness.py /path/to/Aurevia_Synthetic_Insurance_World_v1

Requires the same Python environment as the backend (Tesseract installed,
Ollama reachable) — it calls the real functions, not a mock of them.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import doc_intelligence
import llm
from parsers import ParseError, parse_file_with_quality

# The one field this harness scores, per the dataset's own README: "Use
# property_sum_insured as the primary extraction target because the current
# Phase 0 vertical slice is intentionally narrow." Scoring more than the
# slice extracts would be grading a test the code was never given.
TARGET_FIELD = "property_sum_insured"

# Which document in each company is the ground-truth source for that field.
# Read from ground_truth.json per-company rather than assumed, since the
# dataset states it explicitly and a hardcoded guess could silently drift
# from what the dataset author intended.


def parse_inr(value_str: str) -> int | None:
    """
    Turn an extracted value string into a comparable integer.

    Ground truth is stored as a bare integer (600000000). Extracted values
    are whatever the pipeline actually produced as text — "n600,000,000",
    "₹60,00,00,000", "Rs 600000000" — so both sides need normalising to the
    same representation before they can be compared at all. Returns None
    when no digits can be recovered, which is scored as "no comparable
    value" rather than crashing the whole run over one bad string.
    """
    if not value_str:
        return None
    digits = re.sub(r"[^\d]", "", value_str)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def load_company(company_dir: Path) -> dict:
    ground_truth = json.loads((company_dir / "ground_truth.json").read_text())
    return ground_truth


def find_target_fact(ground_truth: dict) -> dict | None:
    for fact in ground_truth.get("facts", []):
        if fact.get("field") == TARGET_FIELD:
            return fact
    return None


def find_schedule_fact(ground_truth: dict) -> dict | None:
    """The coverage-schedule figure, used to check contradiction detection."""
    for fact in ground_truth.get("facts", []):
        if fact.get("field") == "schedule_property_sum_insured":
            return fact
    return None


def run_one_document(pdf_path: Path) -> dict:
    """
    Push one document through the real pipeline and return what happened.

    Mirrors exactly what /doc-intelligence/upload does in main.py, minus the
    database writes — this harness scores the pipeline's output, not
    whether Postgres can store it (that path is already covered by the
    manual API tests during development).
    """
    data = pdf_path.read_bytes()

    try:
        parsed, quality = parse_file_with_quality(pdf_path.name, data)
    except ParseError as exc:
        return {"error": str(exc), "used_ocr": None, "fact": None}

    ocr_confidence = quality.get("avg_ocr_confidence")
    fact = doc_intelligence.extract_coverage_limit(
        text=parsed.text, ocr_confidence=ocr_confidence
    )

    return {"error": None, "quality": quality, "fact": fact}


def evaluate_company(company_dir: Path) -> dict:
    company_id = company_dir.name
    ground_truth = load_company(company_dir)
    target = find_target_fact(ground_truth)
    schedule_target = find_schedule_fact(ground_truth)
    tests = ground_truth.get("tests", {})

    result = {
        "company_id": company_id,
        "company_name": ground_truth.get("company_name"),
        "intentional_contradiction": tests.get("intentional_contradiction", False),
        "ocr_variant": tests.get("ocr_variant", False),
        "documents": {},
    }

    if not target:
        result["skipped"] = f"No {TARGET_FIELD} fact in ground truth."
        return result

    expected_value = target["value_inr"]
    expected_document = target["source_document"]
    expected_page = target["page"]

    # The primary document: whatever ground truth names as the source for
    # property_sum_insured. Its filename in the dataset is prefixed with the
    # company id (AUR-SYN-001_01_property_policy.pdf) but the file on disk
    # is not (01_property_policy.pdf) -- strip the prefix to find it.
    primary_filename = expected_document.replace(f"{company_id}_", "")
    primary_path = company_dir / primary_filename

    # If this company has a scanned variant, that is the one actually meant
    # to exercise OCR -- run it INSTEAD of the digital original when present,
    # since running both would double-count the company and the digital
    # original is not the interesting case for an ocr_variant company.
    scanned_path = company_dir / primary_filename.replace(".pdf", "_SCANNED.pdf")
    target_path = scanned_path if scanned_path.exists() else primary_path

    if not target_path.exists():
        result["skipped"] = f"Expected document not found: {target_path.name}"
        return result

    outcome = run_one_document(target_path)
    result["documents"][target_path.name] = outcome

    fact = outcome.get("fact")
    quality = outcome.get("quality") or {}

    # ---- P0-01 / P0-02: value + evidence accuracy, OCR trigger ----
    extracted_value = parse_inr(fact["value"]) if fact else None
    result["value_correct"] = extracted_value == expected_value
    result["extracted_value_raw"] = fact["value"] if fact else None
    result["expected_value"] = expected_value

    if fact:
        result["evidence_page_correct"] = fact["evidence"]["page"] == expected_page
        result["confidence_type"] = fact["confidence"]["type"]
        result["confidence_value"] = fact["confidence"]["value"]
    else:
        result["evidence_page_correct"] = None
        result["confidence_type"] = None
        result["confidence_value"] = None

    result["ocr_should_trigger"] = tests.get("ocr_variant", False)
    result["ocr_did_trigger"] = quality.get("used_ocr", False)
    result["ocr_trigger_correct"] = (
        result["ocr_should_trigger"] == result["ocr_did_trigger"]
    )

    # ---- P0-05: no fact extracted where there should be none, or a fact
    # was found with a value -- "found nothing" is a valid, scored outcome,
    # not a harness error. ----
    result["found_a_fact"] = fact is not None

    # ---- P0-03: cross-document contradiction ----
    # Fixed: main.py's /doc-intelligence/upload now compares a newly
    # extracted fact against every other fact already on record for the
    # same submission (doc_intelligence.find_contradiction), and flags both
    # sides as 'contradiction_flagged' when they disagree beyond a 1%
    # tolerance. This harness still runs each document through the bare
    # extraction function directly (not the API endpoint), so it does not
    # exercise that comparison step itself — see
    # backend/main.py's doc_intelligence_upload and the manual verification
    # in Results.md for the actual end-to-end proof against all 4
    # deliberate-contradiction companies in this dataset.
    if schedule_target and tests.get("intentional_contradiction"):
        result["contradiction_test"] = "fixed_not_covered_by_this_harness"
        result["contradiction_note"] = (
            f"Ground truth has a deliberate mismatch (policy: {expected_value}, "
            f"schedule: {schedule_target['value_inr']}). Contradiction "
            "detection now exists (doc_intelligence.find_contradiction, "
            "wired into the /doc-intelligence/upload endpoint) and was "
            "verified manually against this exact case — see Results.md. "
            "This harness calls extraction directly and does not go through "
            "the endpoint, so it does not itself exercise the check."
        )
    else:
        result["contradiction_test"] = "not_applicable"

    # ---- Silent guessing check ----
    # A wrong, confident, non-null value on a case the dataset does not flag
    # as ambiguous IS the failure mode "silent guessing" describes.
    if fact and not result["value_correct"] and tests.get("silent_guessing_must_be_false"):
        result["silent_guess_suspected"] = True
    else:
        result["silent_guess_suspected"] = False

    return result


def summarise(results: list[dict]) -> dict:
    scored = [r for r in results if "skipped" not in r]
    n = len(scored)

    def rate(key, denom=None):
        denom = denom if denom is not None else n
        if denom == 0:
            return None
        return round(sum(1 for r in scored if r.get(key)) / denom * 100, 1)

    ocr_cases = [r for r in scored if r["ocr_should_trigger"]]
    contradiction_cases = [r for r in scored if r["intentional_contradiction"]]

    return {
        "companies_total": len(results),
        "companies_scored": n,
        "companies_skipped": [r["company_id"] for r in results if "skipped" in r],
        "value_accuracy_pct": rate("value_correct"),
        "evidence_page_accuracy_pct": rate("evidence_page_correct"),
        "found_a_fact_pct": rate("found_a_fact"),
        "ocr_trigger_accuracy_pct": (
            round(
                sum(1 for r in ocr_cases if r["ocr_trigger_correct"])
                / len(ocr_cases)
                * 100,
                1,
            )
            if ocr_cases
            else None
        ),
        "confidence_type_distribution": {
            t: sum(1 for r in scored if r.get("confidence_type") == t)
            for t in ("deterministic", "ocr_engine", "model_self_reported", None)
        },
        "contradiction_cases_total": len(contradiction_cases),
        "contradiction_detection_implemented": True,
        "silent_guess_suspected_count": sum(
            1 for r in scored if r.get("silent_guess_suspected")
        ),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 eval_harness.py /path/to/Aurevia_Synthetic_Insurance_World_v1")
        sys.exit(1)

    dataset_root = Path(sys.argv[1])
    companies_dir = dataset_root / "companies"

    if not companies_dir.is_dir():
        print(f"No companies/ directory found under {dataset_root}")
        sys.exit(1)

    if not llm.is_available():
        print(
            f"WARNING: Ollama model '{llm.OLLAMA_MODEL}' is not reachable at "
            f"{llm.OLLAMA_HOST}. Deterministic-rule extractions will still run; "
            "any document that needs the model will fail extraction and be "
            "scored as 'found_a_fact: false'."
        )

    company_dirs = sorted(d for d in companies_dir.iterdir() if d.is_dir())
    print(f"Found {len(company_dirs)} companies. Running evaluation...\n")

    results = []
    for company_dir in company_dirs:
        print(f"  {company_dir.name}...", end=" ", flush=True)
        result = evaluate_company(company_dir)
        results.append(result)
        if "skipped" in result:
            print(f"SKIPPED ({result['skipped']})")
        else:
            mark = "OK" if result["value_correct"] else "WRONG"
            print(
                f"{mark}  expected={result['expected_value']}  "
                f"got={result['extracted_value_raw']!r}  "
                f"confidence={result['confidence_type']}"
            )

    summary = summarise(results)

    output = {"summary": summary, "companies": results}
    out_path = Path(__file__).parent / "eval_results.json"
    out_path.write_text(json.dumps(output, indent=2))

    print(f"\nFull results written to {out_path}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
