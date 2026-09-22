"""
Phase 0 Test 3 — hard OCR stress test.

Runs every degraded variant built by build_hard_ocr_set.py through the REAL
pipeline (parsers.parse_file_with_quality -> doc_intelligence, the same
functions the running app calls) and reports, per degradation:

  - did OCR even manage to produce readable text
  - what the OCR engine's own confidence score was
  - was the correct figure (INR 500,000,000, known from the clean source)
    still extracted correctly
  - did the system correctly say "I don't know" rather than guess, on the
    cases where extraction genuinely could not find a reliable answer

The known-correct answer is fixed because every variant is built from the
same source page (build_hard_ocr_set.py) — this script is not re-deriving
ground truth, it already knows what the source document says.
"""
import json
import sys
from pathlib import Path

# (this script already lives in backend/, no path insert needed)

import doc_intelligence  # noqa: E402
from parsers import ParseError, parse_file_with_quality  # noqa: E402

VARIANT_DIR = Path(__file__).parent / "hard_ocr_test"
KNOWN_CORRECT_VALUE = "500,000,000"  # from the source document, INR500,000,000


def value_matches(extracted: str | None) -> bool:
    if not extracted:
        return False
    digits = "".join(c for c in extracted if c.isdigit())
    return digits == "500000000"


def run_variant(pdf_path: Path) -> dict:
    result = {"variant": pdf_path.stem}

    try:
        parsed, quality = parse_file_with_quality(pdf_path.name, pdf_path.read_bytes())
    except ParseError as exc:
        result.update({"parse_failed": True, "error": str(exc)})
        return result

    result["used_ocr"] = quality["used_ocr"]
    result["ocr_engine_confidence"] = quality.get("avg_ocr_confidence")
    result["ocr_notes"] = quality.get("notes", [])
    result["chars_extracted"] = len(parsed.text.strip())

    fact = doc_intelligence.extract_coverage_limit(
        text=parsed.text, ocr_confidence=quality.get("avg_ocr_confidence")
    )

    if fact is None:
        result["fact_found"] = False
        result["correctly_abstained"] = True  # no fact is the honest outcome when unsure
        return result

    result["fact_found"] = True
    result["extracted_value"] = fact["value"]
    result["confidence_type"] = fact["confidence"]["type"]
    result["confidence_value"] = fact["confidence"]["value"]
    result["value_correct"] = value_matches(fact["value"])
    # A wrong value presented with high confidence is the actual failure
    # mode this test exists to catch — not "OCR struggled" but "OCR
    # struggled AND the system did not notice."
    result["silent_wrong_answer"] = (
        not result["value_correct"] and fact["confidence"]["value"] >= 0.7
    )

    return result


def main():
    variants = sorted(VARIANT_DIR.glob("*.pdf"))
    if not variants:
        print(f"No variants found in {VARIANT_DIR}. Run build_hard_ocr_set.py first.")
        sys.exit(1)

    print(f"Running {len(variants)} degraded variants through the real pipeline...\n")
    results = []
    for pdf_path in variants:
        print(f"  {pdf_path.stem}...", end=" ", flush=True)
        r = run_variant(pdf_path)
        results.append(r)

        if r.get("parse_failed"):
            print(f"PARSE FAILED ({r['error'][:60]})")
        elif not r["fact_found"]:
            print("NO FACT EXTRACTED (correctly abstained rather than guess)")
        else:
            mark = "CORRECT" if r["value_correct"] else "WRONG"
            danger = " <<< SILENT WRONG ANSWER" if r.get("silent_wrong_answer") else ""
            ocr_conf = r.get("ocr_engine_confidence")
            print(
                f"{mark}  extracted={r['extracted_value']!r}  "
                f"ocr_conf={ocr_conf}  model_conf_type={r['confidence_type']}{danger}"
            )

    out_path = Path(__file__).parent / "hard_ocr_results.json"
    out_path.write_text(json.dumps(results, indent=2))

    total = len(results)
    correct = sum(1 for r in results if r.get("value_correct"))
    abstained = sum(1 for r in results if r.get("correctly_abstained"))
    silent_wrong = sum(1 for r in results if r.get("silent_wrong_answer"))
    parse_failed = sum(1 for r in results if r.get("parse_failed"))

    print(f"\n{'='*60}")
    print(f"SUMMARY ({total} variants)")
    print(f"  Correct extraction:        {correct}")
    print(f"  Correctly abstained:       {abstained}")
    print(f"  SILENT WRONG ANSWERS:      {silent_wrong}  <-- the number that matters most")
    print(f"  Parse failures:            {parse_failed}")
    print(f"\nFull results: {out_path}")


if __name__ == "__main__":
    main()
