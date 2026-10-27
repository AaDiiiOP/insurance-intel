"""
Phase 2D — Renewal & submission intelligence (v0.17.0).

Read-only renewal view and a submission package workflow built ONLY from the
existing engines (business profile, policy comparison, business changes,
conflicts, review queue). Nothing here extracts, infers or invents values:
missing data is listed as missing, conflicts as conflicts, and a value that
is not restated in a later document is "not re-observed", never "removed".

Package lifecycle (server-side; anything else -> 409):
    check      DRAFT | NEEDS_INFORMATION -> READY_FOR_REVIEW (all checks pass or have a recorded exception)
                                          -> NEEDS_INFORMATION (otherwise; the blockers are listed)
    approve    READY_FOR_REVIEW -> APPROVED       (drafts.approve; readiness rechecked; fingerprint recorded)
    export     APPROVED | EXPORTED -> EXPORTED    (export; content must equal what was approved)
    reopen     NEEDS_INFORMATION | READY_FOR_REVIEW | APPROVED | EXPORTED -> DRAFT (reason)
    refresh    rebuilds the content from the engines; if it changed after approval -> DRAFT (approval stale)
Nothing is sent to an insurer; export only produces a DOCX DRAFT for a person.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import auth
import business_intelligence as bi
import client_intelligence as cli
import db
import export_docx
import policy_intelligence as pi
import review_workflow as rw

NOTE = ("DRAFT prepared from the documents for review by an authorized broker. Not a completed or "
        "insurer-approved submission, not a quote, not binding and not advice.")

# Readiness rules: (id, plain description, may a person record a reviewed exception?)
RULES = [
    ("business_linked", "The client is linked to its analyzed business.", False),
    ("legal_name", "The insured's legal name is stated in the documents.", False),
    ("policy_present", "At least one policy is found in the documents.", False),
    ("policy_current", "A policy period covers today, or a renewal period is documented.", True),
    ("no_conflicts", "No unresolved conflicting values.", True),
    ("facts_verified", "No facts still awaiting verification.", True),
    ("evidence_present", "No facts without evidence.", True),
    ("reviews_closed", "No open review-queue items for this client.", True),
    ("coverage_reviews_current", "No coverage decision applies to evidence that has since changed.", True),
]
RULE_TEXT = {r[0]: r[1] for r in RULES}
WAIVABLE = {r[0] for r in RULES if r[2]}

TRANSITIONS = {
    "check": {"DRAFT", "NEEDS_INFORMATION"},
    "approve": {"READY_FOR_REVIEW"},
    "export": {"APPROVED", "EXPORTED"},
    "reopen": {"NEEDS_INFORMATION", "READY_FOR_REVIEW", "APPROVED", "EXPORTED"},
}


class PackageError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _client(conn, org, client_id):
    c = db.get_client(conn, client_id, org)
    if not c:
        raise PackageError(404, "Client not found.")
    return c


def _business_id(conn, org, client):
    bid = str(client["business_entity_id"]) if client.get("business_entity_id") else None
    if bid:
        e = db.get_entity(conn, bid, org)
        if e is None or e["entity_type"] != "business":
            bid = None
    return bid


# --------------------------------------------------------------------------
# Renewal view
# --------------------------------------------------------------------------

def _no_removed(cmp):
    """The Phase 1C comparison labels a multi-valued item missing from the newer
    version '<x>_removed' (with the note 'not stated in the newer version').
    For renewals that is shown as NOT RESTATED — absence is not removal."""
    if not cmp:
        return cmp
    for c in cmp.get("changes") or []:
        if str(c.get("change_type", "")).endswith("_removed"):
            c["change_type"] = c["change_type"][: -len("_removed")] + "_not_restated"
            c["note"] = "Not stated in the newer document. This does not mean it was removed."
    if cmp.get("summary", {}).get("types"):
        cmp["summary"]["types"] = sorted({t[: -len("_removed")] + "_not_restated" if t.endswith("_removed") else t
                                          for t in cmp["summary"]["types"]})
    return cmp


def get_renewal(conn, org, client_id) -> dict:
    client = _client(conn, org, client_id)
    bid = _business_id(conn, org, client)
    out = {"client_id": client_id, "business_id": bid, "as_of": date.today().isoformat(), "note": NOTE}
    if not bid:
        out.update(status="not_linked", reason="Link the client to its analyzed business first.")
        return out
    prof = bi.get_business_profile(conn, org, bid)
    policies = []
    for ref in prof.get("policies") or []:
        v = pi.get_policy(conn, org, ref["policy_id"])
        period = v["period"]
        known = period.get("known") or []
        # expiring vs renewal = the last two stated periods (Phase 1C comparison, unchanged)
        cmp = pi.compare_policies(conn, org, ref["policy_id"], as_of=known[-1]["from"] if len(known) >= 2 else None)
        policies.append({
            "policy_id": ref["policy_id"], "policy_number": v["policy_number"]["answer"],
            "insurer": v["insurer"]["answer"], "period_status": period.get("status"), "periods": known,
            "expiring_period": known[-2] if len(known) >= 2 else (known[-1] if known else None),
            "renewal_period": known[-1] if len(known) >= 2 else None,
            "comparison": _no_removed(cmp),
            "comparison_status": (cmp or {}).get("status", "unavailable"),
        })
    changes = bi.get_business_changes(conn, org, bid) or {}
    conflicts = bi.find_business_conflicts(conn, org, bid) or []
    renewal_known = any(p["renewal_period"] for p in policies)
    out.update(
        status="compared" if renewal_known else "pending",
        reason=None if renewal_known else "No renewal or replacement period is documented yet; the comparison is pending.",
        policies=policies,
        business_changes={"status": changes.get("status"), "from": changes.get("from"), "to": changes.get("to"),
                          "summary": changes.get("summary"), "changes": changes.get("changes") or [],
                          "not_reobserved": changes.get("not_reobserved") or [],
                          "not_reobserved_note": "Not restated in the later documents. This does NOT mean removed."},
        conflicts=[{"key": c.get("key"), "values": c.get("values"),
                    "evidence": [e for f in c.get("facts") or [] for e in f.get("evidence") or []][:4]}
                   for c in conflicts],
    )
    return out


# --------------------------------------------------------------------------
# Package content and readiness
# --------------------------------------------------------------------------

def _content(conn, org, client) -> dict:
    """The submission draft: every field is the engine's value with its
    state and evidence, or explicitly missing."""
    rw.refresh(conn, org)                  # queue state is part of the content
    ci_out = cli.get_client_intelligence(conn, org, client)
    bid = _business_id(conn, org, client)
    sections = {}
    if bid:
        prof = bi.get_business_profile(conn, org, bid)
        for k in ("locations", "operations", "assets", "controls", "claims", "risks", "exposures", "risk_factors"):
            sections[k] = [{"name": it["name"], **cli._value(it.get("value") if isinstance(it.get("value"), dict)
                                                             else {k2: it.get(k2) for k2 in ("answer", "values", "facts_used", "review_status", "contradictions", "observed_on")})}
                           for it in prof.get(k) or []]
    s = ci_out["sections"]
    return {"client": {"client_id": str(client["id"]), "business_name": client["business_name"]},
            "business": ci_out["business"],
            "profile_fields": (s.get("profile") or {}).get("fields") or {},
            "sections": sections,
            "policies": (s.get("policies") or {}).get("items") or [],
            "coverage": {k: (s.get("coverage") or {}).get(k) for k in ("mapping_status_counts", "finding_counts", "review_counts")},
            "conflicts": (s.get("conflicts") or {}).get("total"),
            "fact_review_state": {k: (s.get(k) or {}).get("provenance") for k in ("profile", "policies")},
            "open_review_items": _open_items(conn, org, str(client["id"])),
            "conflict_items": [{"key": c.get("key"), "values": c.get("values"),
                                "evidence": [cli._ev_ref(e) if hasattr(cli, "_ev_ref") else
                                             {"document_filename": e.get("document_filename"), "page": e.get("page")}
                                             for e in c.get("evidence") or []][:4]}
                               for c in ((s.get("conflicts") or {}).get("business") or [])
                               + ((s.get("conflicts") or {}).get("policies") or [])],
            "missing_sections": (s.get("profile") or {}).get("missing_sections") or []}


def _open_items(conn, org, cid):
    with conn.cursor() as cur:
        cur.execute("""SELECT kind, subject_id, state FROM review_items WHERE organization_id = %s AND client_id = %s
                       ORDER BY kind, subject_id""", (org, cid))
        return [f"{r['kind']}:{r['subject_id']}:{r['state'] in rw.OPEN_STATES}" for r in cur.fetchall()]


def _fingerprint(content) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def readiness(conn, org, client, content, exceptions) -> dict:
    rw.refresh(conn, org)
    cid = str(client["id"])
    with conn.cursor() as cur:
        cur.execute("""SELECT kind, count(*) AS n FROM review_items WHERE organization_id = %s AND client_id = %s
                       AND state = ANY(%s) GROUP BY kind""", (org, cid, list(rw.OPEN_STATES)))
        open_items = {r["kind"]: r["n"] for r in cur.fetchall()}
        cur.execute("""SELECT count(*) AS n FROM review_items WHERE organization_id = %s AND client_id = %s
                       AND kind = 'coverage_finding' AND (summary->>'review_stale') = 'true'""", (org, cid))
        stale = cur.fetchone()["n"]
    pols = content["policies"]
    results = {
        "business_linked": (bool(content["business"]), "Link the client to its analyzed business (Documents panel)."),
        "legal_name": ((content["profile_fields"].get("legal_name") or {}).get("answer") is not None,
                       "The legal name is missing or conflicting; resolve it in the review queue."),
        "policy_present": (bool(pols), "No policy found; upload and analyze the policy documents."),
        "policy_current": (any(p.get("period_status") == "current" for p in pols)
                           or any(len(p.get("periods") or []) >= 2 for p in pols),
                           "No policy period covers today and no renewal period is documented."),
        "no_conflicts": (open_items.get("fact_conflict", 0) == 0 and not content["conflicts"],
                         f"{max(open_items.get('fact_conflict', 0), content['conflicts'] or 0)} conflicting value(s) to resolve."),
        "facts_verified": (open_items.get("unverified_fact", 0) == 0,
                           f"{open_items.get('unverified_fact', 0)} fact(s) awaiting verification."),
        "evidence_present": (open_items.get("missing_evidence", 0) == 0,
                             f"{open_items.get('missing_evidence', 0)} fact(s) without evidence."),
        "reviews_closed": (sum(v for k, v in open_items.items()) == 0,
                           f"{sum(open_items.values())} open review-queue item(s)."),
        "coverage_reviews_current": (stale == 0, f"{stale} coverage decision(s) refer to changed evidence."),
    }
    fp = _fingerprint(content)
    checks = []
    for rid, text, waivable in RULES:
        ok, fix = results[rid]
        exc = next((e for e in exceptions if e["check_id"] == rid and e["fingerprint"] == fp), None)
        checks.append({"id": rid, "rule": text, "passed": ok, "fix": None if ok else fix,
                       "waivable": waivable, "exception": exc if (not ok and exc) else None})
    blockers = [c for c in checks if not c["passed"] and not c["exception"]]
    return {"ready": not blockers, "checks": checks, "blockers": [{"id": c["id"], "fix": c["fix"]} for c in blockers],
            "fingerprint": fp}


# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------

def _event(conn, org, pid, ctx, action, frm, to, reason=None, fingerprint=None, **meta):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO submission_package_events (organization_id, package_id, actor_user_id, actor, action,
                       from_state, to_state, reason, fingerprint, metadata) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                    (org, pid, ctx.user_id if ctx else None, ctx.actor if ctx else "system", action, frm, to, reason,
                     fingerprint, json.dumps(meta, default=str)))
    auth.audit(conn, ctx, f"submission.{action}", "submission_package", pid,
               {"from": frm, "to": to, "reason": reason, "fingerprint": fingerprint, **meta})


def _set(conn, org, pid, **cols):
    sets = ", ".join(f"{k} = %s" for k in cols)
    vals = [json.dumps(v, default=str) if k in ("content", "exceptions") else v for k, v in cols.items()]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE submission_packages SET {sets}, updated_at = now() WHERE id = %s AND organization_id = %s "
                    "RETURNING *", (*vals, pid, org))
        return cur.fetchone()


def _pkg(conn, org, pid):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM submission_packages WHERE id = %s AND organization_id = %s", (pid, org))
        r = cur.fetchone()
    if not r:
        raise PackageError(404, "Submission package not found.")
    return r


def create(conn, ctx, client_id):
    org = ctx.organization_id
    client = _client(conn, org, client_id)
    content = _content(conn, org, client)
    fp = _fingerprint(content)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO submission_packages (organization_id, client_id, content, fingerprint)
                       VALUES (%s, %s, %s::jsonb, %s) RETURNING *""", (org, client_id, json.dumps(content, default=str), fp))
        r = cur.fetchone()
    _event(conn, org, str(r["id"]), ctx, "created", None, "DRAFT", fingerprint=fp, client_id=client_id)
    return detail(conn, ctx, str(r["id"]))


def _refresh(conn, ctx, r):
    """Rebuild content; an approval on different content is stale."""
    org = ctx.organization_id
    client = _client(conn, org, str(r["client_id"]))
    content = _content(conn, org, client)
    fp = _fingerprint(content)
    if fp != r["fingerprint"]:
        to = "DRAFT" if r["state"] in ("APPROVED", "EXPORTED", "READY_FOR_REVIEW") else r["state"]
        r = _set(conn, org, str(r["id"]), content=content, fingerprint=fp, state=to)
        _event(conn, org, str(r["id"]), ctx, "content_changed", None, to, fingerprint=fp,
               reason="Information changed since the last check; approval (if any) is no longer valid.")
    return r, client, content


def detail(conn, ctx, pid, refresh=True):
    org = ctx.organization_id
    r = _pkg(conn, org, pid)
    if refresh:
        r, client, content = _refresh(conn, ctx, r)
    else:
        client, content = _client(conn, org, str(r["client_id"])), r["content"]
    ready = readiness(conn, org, client, content, r["exceptions"] or [])
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM submission_package_events WHERE package_id = %s AND organization_id = %s ORDER BY id",
                    (pid, org))
        hist = [{k: _iso(e[k]) for k in ("at", "actor", "action", "from_state", "to_state", "reason", "fingerprint")}
                for e in cur.fetchall()]
    approval_current = bool(r["approved_fingerprint"]) and r["approved_fingerprint"] == r["fingerprint"]
    return {"package_id": pid, "client_id": str(r["client_id"]), "state": r["state"], "content": r["content"],
            "fingerprint": r["fingerprint"], "readiness": ready, "exceptions": r["exceptions"],
            "approval": {"by": r["approved_by"], "at": _iso(r["approved_at"]), "fingerprint": r["approved_fingerprint"],
                         "current": approval_current},
            "history": hist, "note": NOTE}


def list_for_client(conn, ctx, client_id):
    org = ctx.organization_id
    _client(conn, org, client_id)
    with conn.cursor() as cur:
        cur.execute("""SELECT id, state, created_at, approved_by FROM submission_packages WHERE organization_id = %s
                       AND client_id = %s ORDER BY created_at DESC""", (org, client_id))
        return {"packages": [{"package_id": str(r["id"]), "state": r["state"], "created_at": _iso(r["created_at"]),
                              "approved_by": r["approved_by"]} for r in cur.fetchall()]}


def add_exception(conn, ctx, pid, check_id, reason):
    org = ctx.organization_id
    if not (ctx.can("facts.correct") or ctx.can("findings.review")):
        raise PackageError(403, "Your role cannot record readiness exceptions.")
    if check_id not in WAIVABLE:
        raise PackageError(400, "This check cannot be waived." if check_id in RULE_TEXT else "Unknown check.")
    reason = " ".join((reason or "").split())[:1000]
    if not reason:
        raise PackageError(400, "A reason is required for an exception.")
    r, _, _ = _refresh(conn, ctx, _pkg(conn, org, pid))
    exc = list(r["exceptions"] or []) + [{"check_id": check_id, "reason": reason, "by": ctx.actor,
                                         "at": date.today().isoformat(), "fingerprint": r["fingerprint"]}]
    _set(conn, org, pid, exceptions=exc)
    _event(conn, org, pid, ctx, "exception_recorded", r["state"], r["state"], reason=reason,
           fingerprint=r["fingerprint"], check_id=check_id)
    return detail(conn, ctx, pid)


def transition(conn, ctx, pid, action, reason=None):
    org = ctx.organization_id
    if action not in TRANSITIONS:
        raise PackageError(400, "Unknown action.")
    perm = {"approve": "drafts.approve", "export": "export"}.get(action, "drafts.create")
    if action == "reopen" and (ctx.can("drafts.approve") or ctx.can("drafts.create")):
        perm = None
    if perm and not ctx.can(perm):
        raise PackageError(403, "Your role cannot do that.")
    r, client, content = _refresh(conn, ctx, _pkg(conn, org, pid))
    if r["state"] not in TRANSITIONS[action]:
        raise PackageError(409, f"'{action}' is not allowed while the package is {r['state']}.")
    ready = readiness(conn, org, client, content, r["exceptions"] or [])
    if action == "check":
        to = "READY_FOR_REVIEW" if ready["ready"] else "NEEDS_INFORMATION"
        _set(conn, org, pid, state=to)
        _event(conn, org, pid, ctx, "checked", r["state"], to, fingerprint=r["fingerprint"],
               blockers=[b["id"] for b in ready["blockers"]])
    elif action == "approve":
        if not ready["ready"]:
            _set(conn, org, pid, state="NEEDS_INFORMATION")
            _event(conn, org, pid, ctx, "approval_refused", r["state"], "NEEDS_INFORMATION",
                   fingerprint=r["fingerprint"], blockers=[b["id"] for b in ready["blockers"]])
            raise PackageError(409, "Readiness checks no longer pass: " + "; ".join(b["fix"] for b in ready["blockers"]))
        _set(conn, org, pid, state="APPROVED", approved_by=ctx.actor, approved_user_id=ctx.user_id,
             approved_at=date.today(), approved_fingerprint=r["fingerprint"])
        with conn.cursor() as cur:
            cur.execute("UPDATE submission_packages SET approved_at = now() WHERE id = %s", (pid,))
        _event(conn, org, pid, ctx, "approved", r["state"], "APPROVED", fingerprint=r["fingerprint"])
    elif action == "export":
        if r["approved_fingerprint"] != r["fingerprint"] or not ready["ready"]:
            raise PackageError(409, "The content changed since approval or a readiness check fails; reapproval is needed.")
        _set(conn, org, pid, state="EXPORTED")
        _event(conn, org, pid, ctx, "exported", r["state"], "EXPORTED", fingerprint=r["fingerprint"])
    elif action == "reopen":
        reason = " ".join((reason or "").split())[:1000]
        if not reason:
            raise PackageError(400, "A reason is required to reopen.")
        _set(conn, org, pid, state="DRAFT", approved_fingerprint=None)
        _event(conn, org, pid, ctx, "reopened", r["state"], "DRAFT", reason=reason, fingerprint=r["fingerprint"])
    return detail(conn, ctx, pid, refresh=False)


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

def _line(label, v):
    if not isinstance(v, dict):
        return f"{label}: {v}"
    ans = v.get("answer")
    shown = " / ".join(map(str, ans)) if isinstance(ans, list) else ans
    if v.get("state") == "conflicting":
        txt = f"CONFLICTING — {' vs '.join(map(str, v.get('values') or []))}"
    elif ans is None:
        txt = "NOT FOUND IN THE DOCUMENTS"
    else:
        txt = f"{shown} [{(v.get('state') or '').replace('_', ' ')}]"
    ev = v.get("evidence") or []
    src = "; ".join(f"{e.get('document_filename')} p{e.get('page') or '?'}" for e in ev[:3])
    return f"{label}: {txt}" + (f" (source: {src})" if src else " (no evidence recorded)" if ans is not None else "")


def docx_bytes(conn, ctx, pid) -> tuple[bytes, str]:
    d = transition(conn, ctx, pid, "export")
    c = d["content"]
    lines = [NOTE, "", "CLIENT AND BUSINESS"]
    lines += [_line(k.replace("_", " ").title(), v) for k, v in c["profile_fields"].items()]
    for sec, items in c["sections"].items():
        lines += ["", sec.replace("_", " ").upper()]
        lines += [_line(i["name"], i) for i in items] or ["None documented."]
    lines += ["", "EXISTING POLICIES"]
    for p in c["policies"]:
        lines.append(f"Policy {(p['fields'].get('policy_number') or {}).get('answer') or p.get('name')} — period "
                     f"{', '.join(x['from'] + ' to ' + x['to'] for x in p.get('periods') or []) or 'not stated'}")
        lines += ["  " + _line(k.replace("_", " ").title(), v) for k, v in p["fields"].items() if k != "policy_number"]
    lines += ["", "CONFLICTING VALUES (kept as stated; a person must resolve them)"]
    lines += [f"{x['key']}: CONFLICTING — {' vs '.join(map(str, x['values'] or []))} (source: "
              + "; ".join(f"{e.get('document_filename')} p{e.get('page') or '?'}" for e in x["evidence"]) + ")"
              for x in c.get("conflict_items") or []] or ["None."]
    lines += ["", "COVERAGE REVIEW (system findings, for broker review)",
              f"Mapping: {c['coverage'].get('mapping_status_counts')}", f"Findings: {c['coverage'].get('finding_counts')}",
              f"Reviews: {c['coverage'].get('review_counts')}", "", "READINESS"]
    for ch in d["readiness"]["checks"]:
        state = "passed" if ch["passed"] else f"EXCEPTION by {ch['exception']['by']}: {ch['exception']['reason']}"
        lines.append(f"{ch['rule']} — {state}")
    data = export_docx.build(title=f"Submission draft — {c['client']['business_name']}", body="\n".join(lines),
                             status="approved", client_name=c["client"]["business_name"], carrier_name=None,
                             approved_by=d["approval"]["by"], approved_at=d["approval"]["at"], kind="submission")
    name = "".join(ch if ch.isalnum() else "_" for ch in c["client"]["business_name"])[:60]
    return data, f"submission_draft_{name}.docx"
