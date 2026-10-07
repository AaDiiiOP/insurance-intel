"""
Re-grade the Knowledge query layer on facts ALREADY extracted by an earlier
eval_property_policy.py run — no model calls, takes seconds.

    python3 recheck_query.py /app/benchmark_data <organization_id> [AUR-SYN-001,AUR-SYN-002,...]

<organization_id> is printed at the top of every benchmark run
("Running Phase 1A benchmark (organization ...)"). Each company's business
entity is the one the benchmark created (named after the company id); the
policy period is re-read deterministically from that submission's documents.
Writes evaluations/phase1a/query-recheck-<timestamp>.json and exits 1 on any
wrong answer or failed safety probe.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import db
import eval_property_policy as ev
import property_policy_intelligence as ppi


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    dataset, org = Path(sys.argv[1]), sys.argv[2]
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    results = []
    with db.get_connection() as conn:
        for company_dir in sorted((dataset / "companies").iterdir()):
            if not company_dir.is_dir() or (only and company_dir.name not in only):
                continue
            biz = db.find_entity_by_name(conn, org, "business", company_dir.name)
            if biz is None:
                continue
            periods = []
            for s in db.list_submissions(conn, org):
                if s["label"] == company_dir.name:
                    for d in db.list_submission_documents(conn, str(s["id"]), org):
                        p = ppi.extract_policy_period(d.get("extracted_text") or "")
                        if p:
                            periods.append([p[0].isoformat(), p[1].isoformat()])
            ingest = {"analysis": {"business": biz,
                                   "documents_processed": [{"policy_period": p} for p in periods]}}
            results.append(ev.query_check_company(conn, org, company_dir, ingest))
            print(f"  {company_dir.name}: rechecked")
        conn.rollback()      # probes create a throwaway organization; keep nothing
    if not results:
        print("No benchmark companies found in that organization.")
        sys.exit(2)
    summary = ev.summarise_query_check(results)
    out = Path(__file__).parent / "evaluations" / "phase1a" / (
        "query-recheck-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    out.write_text(json.dumps({"organization_id": org, "summary": summary, "companies": results},
                              indent=2, default=str))
    print(json.dumps(summary, indent=2))
    for r in results:
        for q in r["questions"]:
            if q["verdict"] != "correct":
                print(f"  {r['company_id']} {q['fact_type']}: {q['verdict']} — {q['reason']}")
        for p in r["probes"]:
            if not p["passed"]:
                print(f"  PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r}")
    print(f"written to {out}")
    sys.exit(0 if summary["hard_gate_zero_wrong_answers"] else 1)


if __name__ == "__main__":
    main()
