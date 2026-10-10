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
import eval_coverage
import eval_property_policy as ev
import property_policy_intelligence as ppi


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    dataset, org = Path(sys.argv[1]), sys.argv[2]
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    results, policy_results, business_results, coverage_results = [], [], [], []
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
            docs = [d for s in db.list_submissions(conn, org) if s["label"] == company_dir.name
                    for d in db.list_submission_documents(conn, str(s["id"]), org)]
            policy_results.append(ev.policy_check_company(conn, org, company_dir, biz, docs,
                                                          periods[0][0] if periods else None))
            subs = [s_ for s_ in db.list_submissions(conn, org) if s_["label"] == company_dir.name]
            if subs:
                # Stage A only: re-grading must not add the Phase 1D supplement documents.
                sub_docs = {d["filename"] for d in db.list_submission_documents(conn, str(subs[0]["id"]), org)}
                sup_dir = ev.PHASE1D_SUPPLEMENT / company_dir.name
                present = sup_dir.is_dir() and any(f.name in sub_docs for f in sup_dir.glob("*.pdf"))
                coverage_results.append(eval_coverage.coverage_check_company(conn, org, company_dir, str(biz["id"])))
                business_results.append(ev.business_check_company(
                    conn, org, company_dir, {"submission_id": str(subs[0]["id"]),
                                             "analysis": {"business": biz}},
                    with_supplement=False, supplement_present=present))
            print(f"  {company_dir.name}: rechecked")
        conn.rollback()      # probes create a throwaway organization; keep nothing
    if not results:
        print("No benchmark companies found in that organization.")
        sys.exit(2)
    summary = ev.summarise_query_check(results)
    policy_summary = ev.summarise_policy_check(policy_results)
    business_summary = ev.summarise_business_check(business_results)
    coverage_summary = eval_coverage.summarise_coverage_check(coverage_results)
    out = Path(__file__).parent / "evaluations" / "phase1a" / (
        "query-recheck-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    out.write_text(json.dumps({"organization_id": org, "summary": summary, "companies": results,
                               "policy_summary": policy_summary, "policy_companies": policy_results,
                               "business_summary": business_summary, "business_companies": business_results,
                               "coverage_summary": coverage_summary, "coverage_companies": coverage_results},
                              indent=2, default=str))
    print(json.dumps(summary, indent=2))
    print("policy check (Phase 1C):", json.dumps(policy_summary, indent=2))
    print("business check (Phase 1D):", json.dumps(business_summary, indent=2))
    for r in results:
        for q in r["questions"]:
            if q["verdict"] != "correct":
                print(f"  {r['company_id']} {q['fact_type']}: {q['verdict']} — {q['reason']}")
        for p in r["probes"]:
            if not p["passed"]:
                print(f"  PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r}")
    for r in policy_results:
        for c in r["checks"]:
            if c["verdict"] != "correct":
                print(f"  {r['company_id']} {c['check']}: {c['verdict']} — {c['reason']}")
        for p in r["probes"]:
            if not p["passed"]:
                print(f"  POLICY PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r}")
    print(f"written to {out}")
    for r in business_results:
        for c in r["checks"]:
            if c["verdict"] != "correct":
                print(f"  {r['company_id']} {c['check']}: {c['verdict']} — {c['reason']}")
        for p in r["probes"]:
            if not p["passed"]:
                print(f"  BUSINESS PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r}")
    eval_coverage.print_coverage_summary(coverage_summary, coverage_results)
    ok = (summary["hard_gate_zero_wrong_answers"] and policy_summary["hard_gate_zero_wrong"]
          and business_summary["hard_gate_zero_wrong"] and coverage_summary["hard_gate_zero_wrong"])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
