"""
Phase 1D final acceptance audit — READ-ONLY, no model calls, takes seconds.

    python3 phase1d_acceptance_audit.py /app/benchmark_data <organization_id> [AUR-SYN-001,...]

<organization_id> is the one printed at the top of the real-Qwen run
("Running Phase 1A benchmark (organization ...)"). That run already wrote
every business fact and added the Phase 1D supplement; this script audits
what was PERSISTED there, item by item, against independent expectations
(company_profile.json, ground_truth.json, phase1_ground_truth.json and
benchmark_phase1d/<company>/phase1d_ground_truth.json) — it never re-uses the
reader's own output as the answer key.

It changes nothing in that organization. The only write is one empty probe
organization (named "phase1d-acceptance-probe-…") used to prove another
tenant sees nothing; it holds no data.

Sections (each item PASS/FAIL, any FAIL → exit 1):
  A  persisted business facts: evidence exists, snippet verified in the
     document text, snippet on the stated page, value stated in the snippet
  B  inventory — nothing invented: locations, operations, assets, controls,
     claims/losses, risk factors, risks, exposures, legal name, workforce,
     rating vs. independent expectations
  C  relationships and graph: every edge an allowed type pair, endpoints
     present, no document nodes, no inferred mitigated_by
  D  temporal: values as of before/after the follow-up equal the expected
     old/new values; history kept
  E  contradictions: expected set exactly; contradicted values never answered
  F  change detection: expected kinds; never "removed"; no unexpected change
  G  APIs over HTTP: every /businesses endpoint 200 for the owner, 404 for
     another tenant, 400 for a bad date; no risk score anywhere
  H  query safety: unsupported risks / exposures / controls / scores abstain;
     another tenant's question gets nothing
"""
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import business_intelligence as bi
import db
import policy_structure as ps
import property_policy_intelligence as ppi
from knowledge import query as kq
from knowledge.fact_types import OBSERVED_FACT_TYPES
from knowledge.relationships import ALLOWED_RELATIONSHIPS

SUPPLEMENT = Path(__file__).parent / "benchmark_phase1d"


def norm(x):
    return " ".join(str(x or "").lower().split())


def digits(x):
    return re.sub(r"[^\d]", "", str(x or ""))


def _pages(text):
    out = {}
    for page, line in ps._lines_with_pages(text or ""):
        out.setdefault(page, []).append(line)
    return {p: "\n".join(v) for p, v in out.items()}


def _walk_keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k
            yield from _walk_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


def audit_company(conn, org, company_dir, probe_org, client):
    cid = company_dir.name
    items = []

    def check(section, name, ok, detail=None):
        items.append({"section": section, "check": name, "passed": bool(ok), "detail": detail})

    biz = db.find_entity_by_name(conn, org, "business", cid)
    if biz is None:
        check("setup", "business_exists", False, "no business entity in this organization")
        return {"company_id": cid, "items": items}
    bid = str(biz["id"])
    profile = json.loads((company_dir / "company_profile.json").read_text())
    gt1 = {f["field"]: f.get("value", f.get("value_inr"))
           for f in json.loads((company_dir / "ground_truth.json").read_text()).get("facts", [])}
    p1 = json.loads((company_dir / "phase1_ground_truth.json").read_text()).get("phase1_facts", [])
    sup = json.loads((SUPPLEMENT / cid / "phase1d_ground_truth.json").read_text())
    exp_changes = {c["key"]: c for c in sup.get("changes", [])}

    # ---------------- A. persisted business facts ----------------
    doc_text, n_facts, ev_ok, page_ok, page_n, val_ok, bad = {}, 0, 0, 0, 0, 0, []
    for ent, _prefix, types in bi._all_entities(conn, org, biz):
        for f in db.list_facts_for_entity(conn, str(ent["id"]), org):
            if f["fact_type"] not in OBSERVED_FACT_TYPES:
                continue
            n_facts += 1
            evs = db.get_evidence_for_fact(conn, str(f["id"]), org)
            e = evs[0] if evs else {}
            did = str(e.get("document_id") or "")
            if did and did not in doc_text:
                d = db.get_document_with_text(conn, did, org)
                doc_text[did] = (d or {}).get("extracted_text") or ""
            snip = e.get("snippet") or ""
            verified = bool(did and snip and ppi.verify_snippet(snip, doc_text.get(did, "")))
            ev_ok += verified
            if e.get("page") is not None and did:
                page_n += 1
                on_page = ppi.verify_snippet(snip, _pages(doc_text[did]).get(int(e["page"]), ""))
                page_ok += bool(on_page)
            else:
                on_page = None
            v = f["value"]
            stated = norm(v) in norm(snip) or (digits(v) and digits(v) in digits(snip)
                                                and f["fact_type"] in ("business.workforce", "asset.declared_value",
                                                                       "loss.claims_count", "loss.total_incurred"))
            val_ok += bool(stated)
            if not (verified and stated and on_page is not False):
                bad.append({"fact_type": f["fact_type"], "value": v, "document": e.get("document_filename"),
                            "page": e.get("page"), "verified": verified, "on_page": on_page, "value_in_snippet": stated})
    check("A", "facts_have_verified_evidence", n_facts and ev_ok == n_facts, f"{ev_ok}/{n_facts}")
    check("A", "evidence_on_stated_page", page_ok == page_n, f"{page_ok}/{page_n}")
    check("A", "value_stated_in_evidence", val_ok == n_facts, f"{val_ok}/{n_facts}")
    if bad:
        check("A", "unsupported_facts_listed", False, bad)

    # ---------------- B. inventory: nothing invented ----------------
    cur = bi.get_business_profile(conn, org, bid)
    names = lambda sec: sorted(norm(i["name"]) for i in cur[sec])
    check("B", "legal_name", norm(cur["legal_name"]["answer"]) == norm(profile["legal_name"]),
          cur["legal_name"]["answer"])
    check("B", "locations_exact", names("locations") == sorted(norm(x) for x in sup["locations"]), names("locations"))
    check("B", "operations_exact", names("operations") == [norm(profile["business_activity"])], names("operations"))
    exp_controls = {"fire protection"} | {norm(k.split(":", 1)[1]) for k in exp_changes if k.startswith("control:")}
    check("B", "controls_exact_no_invented", set(names("controls")) == exp_controls, names("controls"))
    exp_rf = {norm(f["value"]) for f in p1 if f["fact_type"] == "property.risk_factors"}
    check("B", "risk_factors_only_stated", set(names("risk_factors")) <= exp_rf, names("risk_factors"))
    all_risks = [norm(e["name"].split(" — ", 1)[-1]) for e in bi._children(conn, org, biz, "has_risk", "risk")]
    check("B", "risks_only_explicit_none_inferred", sorted(all_risks) == sorted(norm(r) for r in sup.get("risks", [])),
          all_risks)
    all_exp = bi._children(conn, org, biz, "has_exposure", "exposure")
    check("B", "no_exposures_invented", len(all_exp) == 0, [e["name"] for e in all_exp])
    assets = bi._children(conn, org, biz, "owns", "asset")
    declared = [a for a in assets if db.list_facts_for_entity(conn, str(a["id"]), org, "asset.declared_value")]
    # Declared values must equal the schedule's own item lines (read here with a
    # separate strict pattern). Not compared with the schedule's printed total:
    # AUR-SYN-004's schedule prints a total (370,000,000) that its own items do
    # not add up to (350,000,000) — a deliberate benchmark inconsistency the
    # Phase 1B gate already treats as a contradiction.
    persisted = {}
    for a in declared:
        for f in db.list_facts_for_entity(conn, str(a["id"]), org, "asset.declared_value"):
            ev = db.get_evidence_for_fact(conn, str(f["id"]), org)
            if ev and "schedule" in (ev[0].get("document_filename") or ""):
                persisted[norm(bi._label(a))] = digits(f["value"])
    sched_docs = [d for s_ in db.list_submissions(conn, org) if s_["label"] == cid
                  for d in db.list_submission_documents(conn, str(s_["id"]), org) if "schedule" in d["filename"]]
    sched_text = (sched_docs[0].get("extracted_text") or "") if sched_docs else ""
    block = sched_text.split("Declared Values", 1)[-1].split("Total", 1)[0] if "Declared Values" in sched_text else ""
    lines = {norm(m.group(1)): digits(m.group(2))
             for m in re.finditer(r"^\s*([A-Za-z][A-Za-z &]+?):\s*\D{0,3}([\d,]{3,})\s*$", block, re.M)}
    check("B", "assets_three_declared", len(declared) == 3 and len(lines) == 3, [bi._label(a) for a in declared])
    check("B", "declared_values_equal_schedule_lines", lines and persisted == lines,
          {"persisted": persisted, "schedule": lines})
    claim_rows = [c for c in cur["claims"]]
    check("B", "one_loss_record_no_invented_claims", len(claim_rows) == 1, len(claim_rows))
    if claim_rows:
        exp_inc = exp_changes.get("loss.total_incurred", {}).get("new") or f"{int(gt1['loss_amount']):,}"
        check("B", "loss_total_incurred", digits(claim_rows[0]["total_incurred"]["answer"]) == digits(exp_inc),
              claim_rows[0]["total_incurred"]["answer"])
        exp_cnt = exp_changes.get("loss.claims_count", {}).get("new")
        if exp_cnt:
            check("B", "loss_claims_count", claim_rows[0]["claims_count"]["answer"] == exp_cnt,
                  claim_rows[0]["claims_count"]["answer"])
    if "business.workforce" in sup.get("contradictions", []):
        check("B", "workforce_contradiction_not_answered", cur["workforce"]["answer"] is None and
              cur["workforce"]["review_status"] == "contradiction", cur["workforce"]["values"])
    else:
        exp_w = sup.get("workforce_after") or profile["employees"]
        check("B", "workforce", digits(cur["workforce"]["answer"]) == digits(exp_w), cur["workforce"]["answer"])
    exp_rating = exp_changes.get("business.survey_risk_rating", {}).get("new") or gt1["risk_rating"]
    check("B", "surveyor_rating", norm(cur["survey_risk_rating"]["answer"]) == norm(exp_rating),
          cur["survey_risk_rating"]["answer"])

    # ---------------- C. relationships and graph ----------------
    g = bi.get_business_graph(conn, org, bid)
    ids = {n["id"]: n for n in g["nodes"]}
    bad_edges = []
    for e in g["edges"]:
        rule = ALLOWED_RELATIONSHIPS.get(e["type"])
        ft, tt = ids.get(e["from"], {}).get("type"), ids.get(e["to"], {}).get("type")
        if rule is None or ft not in rule[0] or tt not in rule[1]:
            bad_edges.append(e)
    check("C", "graph_edges_allowed_and_typed", not bad_edges and g["edges"], f"{len(g['edges'])} edges, bad {bad_edges}")
    check("C", "graph_no_document_nodes", all(n["type"] != "document" for n in g["nodes"]))
    check("C", "graph_no_inferred_mitigated_by", all(e["type"] != "mitigated_by" for e in g["edges"]))
    want = {"location": "has_location", "operation": "performs_operation", "control": "has_control",
            "asset": "owns", "claim": "has_claim", "risk_factor": "has_risk_factor"}
    from_biz = {(ids[e["to"]]["type"], e["type"]) for e in g["edges"] if e["from"] == bid and e["to"] in ids}
    check("C", "business_relationships_present", all((k, v) in from_biz for k, v in want.items()), sorted(from_biz))
    graph_facts = [f for n in g["nodes"] for f in n["facts"]]
    check("C", "graph_facts_all_evidenced", all(f["evidence"] for f in graph_facts), len(graph_facts))

    # ---------------- D. temporal ----------------
    before = bi.get_business_profile(conn, org, bid, as_of=sup["before_date"])
    after = bi.get_business_profile(conn, org, bid, as_of=sup["after_date"])
    simple = {"business.workforce": "workforce", "business.survey_risk_rating": "survey_risk_rating"}
    for key, exp in exp_changes.items():
        if "old" not in exp:
            continue
        if key in simple:
            b, a = before[simple[key]]["answer"], after[simple[key]]["answer"]
        elif key.startswith("asset:"):
            nm = norm(key.split(":", 1)[1])
            b = next((i["value"]["answer"] for i in before["assets"] if norm(i["name"]) == nm), None)
            a = next((i["value"]["answer"] for i in after["assets"] if norm(i["name"]) == nm), None)
        elif key.startswith("loss."):
            fld = key.split(".", 1)[1]
            b = (before["claims"] or [{}])[0].get(fld, {}).get("answer")
            a = (after["claims"] or [{}])[0].get(fld, {}).get("answer")
        else:
            continue
        check("D", f"as_of_before_after:{key}", norm(b) == norm(exp["old"]) and norm(a) == norm(exp["new"]), [b, a])
    if sup.get("changes") and any(c["kind"] == "added" and c["key"].startswith("location:") for c in sup["changes"]):
        check("D", "added_location_absent_before", len(before["locations"]) < len(after["locations"]),
              [len(before["locations"]), len(after["locations"])])
    hist_ok = True
    for key, exp in exp_changes.items():
        if exp["kind"] == "changed" and key in simple:
            rows = db.list_facts_for_entity(conn, bid, org, key)
            vals = {norm(r["value"]) for r in rows}
            hist_ok &= norm(exp["old"]) in vals and norm(exp["new"]) in vals
    check("D", "history_keeps_old_and_new_values", hist_ok)

    # ---------------- E. contradictions ----------------
    conflicts = bi.find_business_conflicts(conn, org, bid) or []
    keys = {c["key"] for c in conflicts}
    check("E", "contradictions_exact", keys == set(sup.get("contradictions", [])), sorted(keys))
    for c in conflicts:
        r = kq.query(conn, organization_id=org, question="How many employees does the business have?",
                     entity_id=bid, use_llm=False) if c["key"] == "business.workforce" else None
        if r is not None:
            check("E", "contradicted_value_not_answered", r["answer"] is None, r.get("reason"))
        check("E", f"contradiction_keeps_both_values:{c['key']}", len(set(c["values"])) >= 2, c["values"])

    # ---------------- F. change detection ----------------
    ch = bi.get_business_changes(conn, org, bid)
    got = {c["key"]: c for c in ch.get("changes") or []}
    for key, exp in exp_changes.items():
        c = next((v for k, v in got.items() if k == key or k.endswith("|" + key)), None)
        ok = c is not None and c["kind"] == exp["kind"] and all(
            norm(c.get(f"{s}_value")) == norm(exp[s]) for s in ("old", "new") if s in exp)
        check("F", f"change:{key}", ok, c and c["kind"])
    for k in sup.get("contradictions", []):
        c = next((v for kk, v in got.items() if kk == k), None)
        check("F", f"change_conflicting:{k}", c is not None and c["kind"] == "conflicting", c and c["kind"])
    expected_keys = set(exp_changes) | set(sup.get("contradictions", []))
    unexpected = [k for k, c in got.items() if c["kind"] in ("added", "changed", "conflicting")
                  and not any(k == e or k.endswith("|" + e) for e in expected_keys)]
    check("F", "no_unexpected_material_change", not unexpected, unexpected)
    check("F", "never_removed", all(c["kind"] in ("added", "changed", "unchanged", "conflicting")
                                    for c in ch.get("changes") or []))
    nr = {n["key"] for n in ch.get("not_reobserved") or []}
    check("F", "not_reobserved_not_reported_as_change", not (nr & set(got)), sorted(nr & set(got)))

    # ---------------- G. APIs over HTTP ----------------
    h, hb = {}, {}        # v0.14.0: the organization comes from each client's own session
    urls = ["", "/profile", f"/profile?as_of={sup['before_date']}", "/locations", "/assets", "/operations",
            "/exposures", "/risks", "/risk_factors", "/controls", "/claims", "/changes", "/conflicts", "/graph"]
    own = {u: client["own"].get(f"/businesses/{bid}{u}", headers=h) for u in urls}
    check("G", "owner_all_endpoints_200", all(r.status_code == 200 for r in own.values()),
          {u: r.status_code for u, r in own.items() if r.status_code != 200})
    other = {u: client["other"].get(f"/businesses/{bid}{u}", headers=hb) for u in urls}
    leak = [u for u, r in other.items() if r.status_code != 404 or cid in r.text
            or norm(profile["legal_name"]) in norm(r.text)]
    check("G", "other_tenant_all_endpoints_404_no_data", not leak, leak)
    listed = client["other"].get("/businesses", headers=hb).json().get("businesses", [])
    check("G", "other_tenant_list_excludes_business", all(b["business_id"] != bid for b in listed))
    check("G", "bad_date_400", client["own"].get(f"/businesses/{bid}/profile?as_of=not-a-date", headers=h).status_code == 400)
    check("G", "risk_api_matches_explicit_risks",
          sorted(norm(i["name"]) for i in own["/risks"].json().get("risks", [])) ==
          sorted(norm(r) for r in sup.get("risks", [])))
    scored = [p for u in ("/profile", "/graph", "/changes", "/risks") for p, k in _walk_keys(own[u].json())
              if "score" in str(k).lower()]
    check("G", "no_risk_score_anywhere", not scored, scored)
    q = client["other"].post("/knowledge/query", headers=hb,
                    json={"question": "What risks are documented?", "entity_id": bid, "use_llm": False})
    check("G", "other_tenant_query_api_nothing", q.status_code in (404, 400) or
          (q.json().get("answer") is None and not q.json().get("evidence")), q.status_code)

    # ---------------- H. query safety ----------------
    def ask(question, o=org):
        return kq.query(conn, organization_id=o, question=question, entity_id=bid, use_llm=False)
    stated_risks = " ".join(norm(r) for r in sup.get("risks", []))
    for question in ("Does this company have flood risk?", "Is there earthquake risk?", "Is there a theft risk?",
                     "Is there a cyber risk?", "Is there a product contamination risk?"):
        r = ask(question)
        check("H", f"abstains:{question}", r["answer"] is None, r.get("reason"))
    r = ask("What exposures are documented?")
    check("H", "abstains:no stated exposure", r["answer"] is None, r.get("reason"))
    r = ask("What risks are documented?")
    check("H", "risks_answer_only_explicit",
          (r["answer"] is None and not stated_risks) or (r["answer"] == sup.get("risks")), r["answer"])
    r = ask("What is the business's risk score?")
    check("H", "no_risk_score_answer", r["answer"] is None or not re.search(r"\d", str(r["answer"])), r["answer"])
    r = ask("Does the business have a burglar alarm?")
    check("H", "unstated_control_not_confirmed", r["answer"] is None or "burglar" not in norm(r["answer"]),
          r["answer"])
    r = ask("What are the business's operating locations?", o=probe_org)
    check("H", "other_tenant_query_nothing", r["answer"] is None and not r.get("evidence"))
    return {"company_id": cid, "facts_audited": n_facts, "evidence_verified": ev_ok,
            "page_checked": page_n, "page_correct": page_ok, "items": items}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    import secrets
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    import auth
    from api import auth as auth_api, businesses as api_businesses, knowledge as api_knowledge

    dataset, org = Path(sys.argv[1]), sys.argv[2]
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else {f"AUR-SYN-00{i}" for i in range(1, 5)}
    app = FastAPI(dependencies=[Depends(auth.guard)])
    for r in (auth_api.router, api_businesses.router, api_knowledge.router):
        app.include_router(r)
    # v0.14.0: API checks run through real sign-in. Two temporary read-only
    # auditor accounts (REVIEWER) are created and deleted again at the end.
    temp_users = []

    def signed_in_client(conn, organization_id):
        pw = secrets.token_urlsafe(24)
        u = db.create_user(conn, f"phase1d-auditor-{uuid.uuid4().hex[:8]}@audit.invalid", "Phase 1D auditor",
                           auth.hash_password(pw))
        db.add_membership(conn, organization_id, str(u["id"]), "REVIEWER")
        conn.commit()
        temp_users.append(str(u["id"]))
        c = TestClient(app)
        r = c.post("/auth/login", json={"email": u["email"], "password": pw, "organization_id": organization_id})
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return c
    results = []
    with db.get_connection() as conn:
        probe_org = str(db.create_organization(conn, f"phase1d-acceptance-probe-{uuid.uuid4().hex[:8]}")["id"])
        conn.commit()
        client = {"own": signed_in_client(conn, org), "other": signed_in_client(conn, probe_org)}
        for company_dir in sorted((dataset / "companies").iterdir()):
            if company_dir.is_dir() and company_dir.name in only:
                results.append(audit_company(conn, org, company_dir, probe_org, client))
                print(f"  {company_dir.name}: audited")
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = ANY(%s::uuid[])", (temp_users,))
    items = [i for r in results for i in r["items"]]
    failed = [dict(i, company=r["company_id"]) for r in results for i in r["items"] if not i["passed"]]
    by_section = {}
    for i in items:
        s = by_section.setdefault(i["section"], {"checks": 0, "passed": 0})
        s["checks"] += 1
        s["passed"] += i["passed"]
    facts = sum(r.get("facts_audited", 0) for r in results)
    summary = {
        "organization_id": org, "companies": [r["company_id"] for r in results],
        "checks": len(items), "passed": len(items) - len(failed), "failed": len(failed),
        "business_facts_audited": facts,
        "evidence_verified_pct": round(100 * sum(r.get("evidence_verified", 0) for r in results) / facts, 1)
        if facts else None,
        "page_accuracy_pct": round(100 * sum(r.get("page_correct", 0) for r in results) /
                                   max(1, sum(r.get("page_checked", 0) for r in results)), 1),
        "by_section": by_section,
        "gate_passed": not failed and len(results) == len(only),
    }
    out = Path(__file__).parent / "evaluations" / "phase1a" / (
        "phase1d-acceptance-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    out.write_text(json.dumps({"summary": summary, "companies": results}, indent=2, default=str))
    print(json.dumps(summary, indent=2))
    for f in failed:
        print(f"  FAIL {f['company']} {f['section']} {f['check']}: {json.dumps(f['detail'], default=str)[:300]}")
    print(f"phase 1D acceptance audit: {summary['checks']} checks  passed {summary['passed']}  "
          f"FAILED {summary['failed']}  evidence verified {summary['evidence_verified_pct']}%  "
          f"page accuracy {summary['page_accuracy_pct']}%  -> {'PASS' if summary['gate_passed'] else 'FAIL'}")
    print(f"written to {out}")
    sys.exit(0 if summary["gate_passed"] else 1)


if __name__ == "__main__":
    main()
