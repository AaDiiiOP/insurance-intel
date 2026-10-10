"""
Phase 1E — "coverage check" gate (v0.13.0), used by
eval_property_policy.py --query-check and by recheck_query.py.

Grades the coverage map / findings / changes of one company against the
hand-written expectations in benchmark_phase1e/<company>/phase1e_ground_truth.json
and runs safety probes. Read-only: it must run AFTER the Phase 1D check (which
adds the Phase 1D supplement), on the same organization, and it writes nothing
except one empty probe organization for the tenant test.

verdicts: correct / abstained (a conflict made the system decline — allowed
only when the conflict really exists) / wrong.
"""
import json
import re
import uuid
from pathlib import Path

import db
import policy_intelligence as pi
import policy_structure as ps
import property_policy_intelligence as ppi
from knowledge import query as kq

PHASE1E = Path(__file__).parent / "benchmark_phase1e"
FORBIDDEN = re.compile(r"under-?insured|over-?insured|not insured|not covered|no insurance|is covered\b|"
                       r"breach(ed)? of|violat|should (buy|purchase)|is adequate|are adequate", re.I)


def _pages(text):
    out = {}
    for page, line in ps._lines_with_pages(text or ""):
        out.setdefault(page, []).append(line)
    return {p: "\n".join(v) for p, v in out.items()}


def coverage_check_company(conn, org: str, company_dir: Path, business_id: str) -> dict:
    import coverage_intelligence as ci
    gt_path = PHASE1E / company_dir.name / "phase1e_ground_truth.json"
    if not gt_path.exists():
        return {"company_id": company_dir.name, "checks": [], "probes": [], "evidence": {}}
    gt = json.loads(gt_path.read_text())
    checks, probes = [], []

    def grade(name, verdict, answer, reason=None):
        checks.append({"check": name, "verdict": verdict, "answer": answer, "reason": reason})

    def probe(name, ok, answer=None):
        probes.append({"probe": name, "passed": bool(ok), "answer": answer})

    m = ci.get_coverage_map(conn, org, business_id)
    f = ci.get_coverage_findings(conn, org, business_id, _map=m)["findings"]
    prop = [r for r in m["mappings"] if r["unit"]["kind"] == "property_exposure"]

    # 1. exposure -> coverage mapping
    r = prop[0] if len(prop) == 1 else None
    ok = (r is not None and r["coverage"] and r["coverage"]["name"] == gt["coverage"]
          and r["policy"]["policy_number"] == gt["policy_number"] and r["mapping_type"] == gt["mapping_type"])
    grade("property_mapping", "correct" if ok else "wrong",
          r and [(r["coverage"] or {}).get("name"), (r["policy"] or {}).get("policy_number"), r["mapping_type"]])

    # 2. exposure vs limit
    si_conflict = any(x["type"] == "COVERAGE_CONFLICT" and x["key"].endswith("|property.sum_insured") for x in f)
    lim_conflict = any(x["type"] == "COVERAGE_CONFLICT" and x["coverage"] == gt["coverage"] for x in f)
    if r is None or not r.get("limit_comparison"):
        grade("limit_comparison", "wrong", None, "no comparison")
    else:
        lc = r["limit_comparison"]
        if lc["state"] == "NEEDS_REVIEW":
            grade("limit_comparison", "abstained" if (si_conflict or lim_conflict) else "wrong", lc["state"],
                  "documents disagree on the limit / sum insured" if (si_conflict or lim_conflict) else "unexplained")
        else:
            ok = (lc["state"] == gt["limit_state"] and lc["limit_amount"] == gt["limit"]
                  and lc["exposure_amount"] == gt["exposure"] and not si_conflict and not lim_conflict)
            grade("limit_comparison", "correct" if ok else "wrong",
                  [lc["state"], lc["exposure_amount"], lc["limit_amount"]])

    # 3. exclusion reviews: exactly the expected ones
    got = sorted((x["subject"], x["key"].split("|")[0].lower()) for x in f if x["type"] == "EXCLUSION_REVIEW")
    want = gt["exclusion_reviews"]
    ok = len(got) == len(want) and all(any(s == w["subject"] and w["contains"] in t for s, t in got) for w in want)
    grade("exclusion_reviews", "correct" if ok else "wrong", got)

    # 4. condition reviews (never "breached")
    cond = [x for x in f if x["type"] == "CONDITION_REVIEW"]
    ok = all(any(x["subject"] == c for x in cond) for c in gt["condition_reviews"]) and all(
        "not a finding that the condition is breached" in x["statement"] or "is not a breach" in x["statement"]
        or "not evidenced" in x["statement"] for x in cond)
    grade("condition_reviews", "correct" if ok else "wrong", [x["subject"] for x in cond])

    # 5. unmapped risks: exactly the documented ones nothing maps to
    got = sorted(x["subject"] for x in f if x["type"] == "UNMAPPED_RISK")
    grade("unmapped_risks", "correct" if got == sorted(gt["unmapped_risks"]) else "wrong", got)

    # 6. conflicts: every unresolved policy contradiction is a finding, and nothing else is
    real = set()
    biz = db.get_entity(conn, business_id, org)
    for p in [e for e in __import__("business_intelligence")._children(conn, org, biz, "has_policy", "policy")]:
        for c in pi.find_policy_conflicts(conn, org, str(p["id"])) or []:
            real.add(("DEDUCTIBLE_REVIEW" if c["fact_type"] == "policy.deductible" else "COVERAGE_CONFLICT",
                      c["fact_type"], c["coverage"]))
    flagged = set()
    for x in f:
        k = x["key"].split("|")
        if x["type"] == "COVERAGE_CONFLICT" and len(k) == 3:
            flagged.add(("COVERAGE_CONFLICT", k[1], None if k[2] == "None" else k[2]))
        elif x["type"] == "DEDUCTIBLE_REVIEW" and x["key"].endswith("|contradiction"):
            flagged.add(("DEDUCTIBLE_REVIEW", "policy.deductible", None))
    real_n, flagged_n = real, flagged
    grade("conflicts_surfaced_not_resolved", "correct" if real_n == flagged_n else "wrong",
          sorted(map(str, flagged_n)), f"knowledge-core conflicts: {sorted(map(str, real_n))}")

    # 7. temporal: before the policy period nothing is in force; mid-period state
    before = ci.get_coverage_map(conn, org, business_id, as_of=_minus_one(gt["no_policy_before"]))
    ok = before["policies_in_force"] == [] and all(x["status"] not in ("MAPPED", "PARTIALLY_MAPPED")
                                                   for x in before["mappings"])
    grade("no_policy_before_period", "correct" if ok else "wrong",
          [before["policies_in_force"], [x["status"] for x in before["mappings"]]])
    for day, state in gt["as_of"].items():
        mm = ci.get_coverage_map(conn, org, business_id, as_of=day)
        pr = [x for x in mm["mappings"] if x["unit"]["kind"] == "property_exposure"]
        st = pr[0]["limit_comparison"]["state"] if pr and pr[0].get("limit_comparison") else None
        if st == "NEEDS_REVIEW" and si_conflict:
            grade(f"as_of:{day}", "abstained", st, "conflict")
        else:
            grade(f"as_of:{day}", "correct" if st == state else "wrong", st)

    # 8. change detection (default window)
    ch = ci.get_coverage_changes(conn, org, business_id)
    pc = [c for c in ch.get("changes") or [] if c["unit"].startswith("Insured property")]
    want = gt["window_change"]
    if pc and pc[0].get("old_limit_state") == "NEEDS_REVIEW" and si_conflict:
        grade("coverage_change_window", "abstained", [pc[0].get("old_limit_state"), pc[0].get("new_limit_state")], "conflict")
    else:
        ok = bool(pc) and [pc[0].get("old_limit_state"), pc[0].get("new_limit_state")] == want and \
            pc[0]["kind"] == ("status_changed" if want[0] != want[1] else pc[0]["kind"])
        grade("coverage_change_window", "correct" if ok else "wrong",
              pc and [pc[0]["kind"], pc[0].get("old_limit_state"), pc[0].get("new_limit_state")])
    probe("never_removed", all("removed" not in c["kind"] for c in ch.get("changes") or []))

    # 9. evidence of every finding: present, verified in the document, on the stated page
    texts, n, verified, on_page, paged = {}, 0, 0, 0, 0
    for x in f:
        for e in x["evidence"]:
            n += 1
            did = e.get("document_id")
            if did and did not in texts:
                d = db.get_document_with_text(conn, did, org)
                texts[did] = (d or {}).get("extracted_text") or ""
            t = texts.get(did, "")
            verified += bool(e.get("snippet") and ppi.verify_snippet(e["snippet"], t))
            if e.get("page") is not None:
                paged += 1
                on_page += bool(e.get("snippet") and ppi.verify_snippet(e["snippet"], _pages(t).get(int(e["page"]), "")))
    probe("every_finding_has_evidence", all(x["evidence"] for x in f), [x["type"] for x in f if not x["evidence"]])

    # Safety probes
    for q in ("Is this company adequately insured?", "Should the company buy more insurance?",
              "Is the business underinsured?"):
        a = kq.query(conn, organization_id=org, question=q, entity_id=business_id, use_llm=False)
        probe(f"declines:{q}", a["answer"] is None and a["answer_type"] == "declined_judgment", a["answer"])
    texts_out = [x["statement"] for x in f] + [x["rationale"] for x in m["mappings"]]
    for q in ("Which exposures exceed documented policy limits?", "What risks have no clearly mapped coverage?",
              "Which findings require broker review?", "What exclusions affect documented risks?"):
        a = kq.query(conn, organization_id=org, question=q, entity_id=business_id, use_llm=False)
        texts_out += a["answer"] or []
        probe(f"evidence_or_abstain:{q}", a["answer"] is None or bool(a["evidence"]), a["answer"])
    bad = [t for t in texts_out if FORBIDDEN.search(t or "")]
    probe("no_fabricated_insurance_conclusion", not bad, bad[:3])
    probe("findings_controlled_types", all(x["type"] in ci.FINDING_TYPES and x["needs_review"] for x in f))
    if not gt["unmapped_risks"]:
        a = kq.query(conn, organization_id=org, question="What risks have relevant coverage?",
                     entity_id=business_id, use_llm=False)
        probe("no_documented_risk_abstains", a["answer"] is None, a["answer"])
    other = str(db.create_organization(conn, f"phase1e-probe-{uuid.uuid4().hex[:8]}")["id"])
    a = kq.query(conn, organization_id=other, question="Which findings require broker review?",
                 entity_id=business_id, use_llm=False)
    probe("cross_tenant", ci.get_coverage_map(conn, other, business_id) is None and a["answer"] is None
          and not a["evidence"])
    return {"company_id": company_dir.name, "checks": checks, "probes": probes,
            "evidence": {"items": n, "verified": verified, "with_page": paged, "on_page": on_page},
            "findings": [{"type": x["type"], "statement": x["statement"]} for x in f]}


def _minus_one(iso):
    from datetime import date, timedelta
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def summarise_coverage_check(results: list[dict]) -> dict:
    checks = [c for r in results for c in r["checks"]]
    probes = [p for r in results for p in r["probes"]]
    n = sum(r["evidence"].get("items", 0) for r in results)
    v = sum(r["evidence"].get("verified", 0) for r in results)
    pg = sum(r["evidence"].get("on_page", 0) for r in results)
    wp = sum(r["evidence"].get("with_page", 0) for r in results)
    return {"checks": len(checks), "correct": sum(c["verdict"] == "correct" for c in checks),
            "abstained": sum(c["verdict"] == "abstained" for c in checks),
            "wrong": sum(c["verdict"] == "wrong" for c in checks),
            "probes": len(probes), "probes_failed": sum(not p["passed"] for p in probes),
            "evidence_items": n, "evidence_verified_pct": round(100 * v / n, 1) if n else None,
            "page_accuracy_pct": round(100 * pg / wp, 1) if wp else None,
            "evidence_items_without_page": n - wp,
            "hard_gate_zero_wrong": all(c["verdict"] != "wrong" for c in checks)
            and all(p["passed"] for p in probes) and v == n and pg == wp}


def print_coverage_summary(summary, results):
    print(f"coverage check (Phase 1E): {summary['checks']} checks  correct {summary['correct']}  "
          f"abstained {summary['abstained']}  WRONG {summary['wrong']}  probes {summary['probes']} failed "
          f"{summary['probes_failed']}  evidence verified {summary['evidence_verified_pct']}%  "
          f"page accuracy {summary['page_accuracy_pct']}% ({summary['evidence_items_without_page']} without a page)")
    for r in results:
        for c in r["checks"]:
            if c["verdict"] != "correct":
                print(f"  {r['company_id']} {c['check']}: {c['verdict']} — answer={c['answer']!r} reason={c['reason']!r}")
        for p in r["probes"]:
            if not p["passed"]:
                print(f"  COVERAGE PROBE FAILED {r['company_id']} {p['probe']}: answer={p['answer']!r}")
