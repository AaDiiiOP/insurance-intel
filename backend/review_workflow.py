"""
Phase 2C — Human review & audit workflow (v0.16.0).

A WORKFLOW layer over what the engines already report. It never stores or
changes a fact itself: every decision is carried out by the existing service
(Knowledge Core verify / reject / versioned correction, Phase 1E coverage
review), and this module only records who did what, when, why, and in which
state the item now is.

Items (review_items), one per actionable thing the system really has:
    fact_conflict     an unresolved kc_conflicts row (all competing values kept)
    unverified_fact   a current needs_review fact WITH evidence
    missing_evidence  a current needs_review fact with NO evidence (cannot be verified)
    coverage_finding  a Phase 1E finding that is open or whose review went stale
    failed_document   a document whose processing failed

Lifecycle (server-side; anything else -> 409):
    assign      OPEN | REOPENED | ASSIGNED | IN_REVIEW | NEEDS_INFO -> ASSIGNED   (reviews.assign)
    unassign    ASSIGNED -> OPEN                                                  (reviews.assign)
    start       OPEN | REOPENED | ASSIGNED | NEEDS_INFO -> IN_REVIEW               (claims it if unassigned)
    needs_info  IN_REVIEW -> NEEDS_INFO                                           (reason)
    decide      IN_REVIEW -> RESOLVED | DISMISSED | NEEDS_INFO (by decision)      (reason where material)
    reopen      RESOLVED | DISMISSED -> REOPENED                                   (reason; coverage / document items)
  System (when the queue refreshes, recorded as by_system, never as a person):
    created     -> OPEN
    reopened    RESOLVED | DISMISSED -> REOPENED   (coverage evidence changed; a verified fact became conflicting)
    closed      open state -> RESOLVED             (the engine shows it was settled elsewhere)

review_events is append-only (database trigger), so history is never rewritten.
Every mutation is also written to audit_events.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import auth
import coverage_intelligence as ci
import db
import knowledge.service as kc

KINDS = ("fact_conflict", "unverified_fact", "missing_evidence", "coverage_finding", "failed_document")
STATES = ("OPEN", "ASSIGNED", "IN_REVIEW", "NEEDS_INFO", "RESOLVED", "DISMISSED", "REOPENED")
OPEN_STATES = ("OPEN", "ASSIGNED", "IN_REVIEW", "NEEDS_INFO", "REOPENED")
CLOSED_STATES = ("RESOLVED", "DISMISSED")

TRANSITIONS = {
    "assign": ({"OPEN", "REOPENED", "ASSIGNED", "IN_REVIEW", "NEEDS_INFO"}, "ASSIGNED"),
    "unassign": ({"ASSIGNED"}, "OPEN"),
    "start": ({"OPEN", "REOPENED", "ASSIGNED", "NEEDS_INFO"}, "IN_REVIEW"),
    "needs_info": ({"IN_REVIEW"}, "NEEDS_INFO"),
    "decide": ({"IN_REVIEW"}, None),            # target depends on the decision
    "reopen": ({"RESOLVED", "DISMISSED"}, "REOPENED"),
}

# decision -> (resulting state, reason required)
DECISIONS = {
    "fact_conflict": {"correct": ("RESOLVED", True)},
    "unverified_fact": {"verify": ("RESOLVED", False), "correct": ("RESOLVED", True),
                        "reject": ("DISMISSED", True)},
    "missing_evidence": {"reject": ("DISMISSED", True)},
    "coverage_finding": {"ACCEPTED": ("RESOLVED", False), "REVIEWED": ("RESOLVED", False),
                         "REJECTED": ("DISMISSED", True), "NEEDS_MORE_EVIDENCE": ("NEEDS_INFO", True)},
    "failed_document": {"resolved": ("RESOLVED", True), "dismissed": ("DISMISSED", True)},
}
MANUAL_REOPEN_KINDS = ("coverage_finding", "failed_document")

# Who may work an item of each kind (any one of these permissions).
KIND_PERMISSIONS = {
    "fact_conflict": ("facts.correct",),
    "unverified_fact": ("facts.correct",),
    "missing_evidence": ("facts.correct",),
    "coverage_finding": ("findings.review",),
    "failed_document": ("documents.upload", "findings.review"),
}
CLIENT_TAB = {"fact_conflict": "conflicts", "unverified_fact": "business", "missing_evidence": "business",
              "coverage_finding": "coverage", "failed_document": "documents"}

ENGINE_NOTE = ("System finding: produced by the document engines for human review. It is not a decision, "
               "not underwriting and not advice.")


class ReviewError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _clean(text) -> str | None:
    t = " ".join((text or "").split())[:1000]
    return t or None


def _can_work(ctx, kind: str) -> bool:
    return any(ctx.can(p) for p in KIND_PERMISSIONS[kind])


def _ev_ref(e: dict) -> dict:
    return {"document_id": str(e["document_id"]) if e.get("document_id") else None,
            "document_filename": e.get("document_filename"),
            "page": e.get("page", e.get("page_number")), "snippet": e.get("snippet")}


def _add_event(conn, org, item_id, *, actor_user_id, actor, by_system, action, from_state, to_state,
               decision=None, reason=None, evidence=None, metadata=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_events (organization_id, item_id, actor_user_id, actor, by_system, action,
                                          from_state, to_state, decision, reason, evidence, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb) RETURNING *""",
            (org, item_id, actor_user_id, actor, by_system, action, from_state, to_state, decision, reason,
             json.dumps(evidence or [], default=str), json.dumps(metadata or {}, default=str)))
        return cur.fetchone()


def _set_item(conn, org, item_id, touch=True, **cols):
    if touch:
        cols["updated_at"] = _now()
    sets = ", ".join(f"{k} = %s" for k in cols)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE review_items SET {sets} WHERE id = %s AND organization_id = %s RETURNING *",
                    (*cols.values(), item_id, org))
        return cur.fetchone()


def _system_event(conn, org, item, action, to_state, reason, **extra):
    _set_item(conn, org, str(item["id"]), state=to_state, **extra)
    _add_event(conn, org, str(item["id"]), actor_user_id=None, actor="system", by_system=True, action=action,
               from_state=item["state"], to_state=to_state, reason=reason)


# --------------------------------------------------------------------------
# What the engines currently report (candidates)
# --------------------------------------------------------------------------

def _graph(conn, org):
    with conn.cursor() as cur:
        cur.execute("SELECT id, entity_type, name FROM kc_entities WHERE organization_id = %s", (org,))
        ents = {str(r["id"]): r for r in cur.fetchall()}
        cur.execute("SELECT from_entity_id, to_entity_id FROM kc_relationships WHERE organization_id = %s", (org,))
        incoming = {}
        for r in cur.fetchall():
            incoming.setdefault(str(r["to_entity_id"]), []).append(str(r["from_entity_id"]))
        cur.execute("SELECT id, business_entity_id FROM clients WHERE organization_id = %s "
                    "AND business_entity_id IS NOT NULL", (org,))
        clients = {str(r["business_entity_id"]): str(r["id"]) for r in cur.fetchall()}
    return ents, incoming, clients


def _business_of(entity_id, ents, incoming):
    """The business an entity belongs to, following stored relationships up to
    three steps (business -> policy -> coverage, business -> location ...)."""
    seen, frontier = set(), [entity_id]
    for _ in range(4):
        nxt = []
        for e in frontier:
            if e in seen:
                continue
            seen.add(e)
            if ents.get(e, {}).get("entity_type") == "business":
                return e
            nxt.extend(incoming.get(e, []))
        frontier = nxt
    return None


def _label(ent) -> str:
    return (ent or {}).get("name") or "unknown entity"


def _candidates(conn, org) -> dict:
    ents, incoming, clients = _graph(conn, org)
    out = {}

    def put(kind, subject_id, entity_id, title, summary, engine_status, fingerprint, **extra):
        biz = _business_of(entity_id, ents, incoming) if entity_id else extra.pop("business_id", None)
        out[(kind, subject_id)] = {"kind": kind, "subject_id": subject_id, "business_id": biz,
                                   "client_id": extra.pop("client_id", None) or (clients.get(biz) if biz else None),
                                   "title": title[:300], "summary": summary, "engine_status": engine_status,
                                   "fingerprint": fingerprint, **extra}

    with conn.cursor() as cur:
        # 1. unresolved conflicts, with every competing value
        cur.execute(
            """SELECT c.id, c.entity_id, c.fact_type, f.id AS fact_id, f.value, f.effective_from
               FROM kc_conflicts c JOIN kc_conflict_facts cf ON cf.conflict_id = c.id
               JOIN kc_facts f ON f.id = cf.fact_id
               WHERE c.organization_id = %s AND c.status = 'unresolved' ORDER BY c.id, f.created_at""", (org,))
        conf = {}
        for r in cur.fetchall():
            c = conf.setdefault(str(r["id"]), {"entity_id": str(r["entity_id"]), "fact_type": r["fact_type"],
                                               "values": [], "fact_ids": []})
            c["values"].append(r["value"])
            c["fact_ids"].append(str(r["fact_id"]))
        for cid, c in conf.items():
            ent = ents.get(c["entity_id"])
            put("fact_conflict", cid, c["entity_id"],
                f"Conflicting {c['fact_type']} for {_label(ent)}: {' vs '.join(sorted(set(c['values'])))}",
                {"fact_type": c["fact_type"], "entity": _label(ent), "values": c["values"]},
                "unresolved", ",".join(sorted(c["fact_ids"])))
        # 2. current facts awaiting review (with / without evidence)
        cur.execute(
            """SELECT f.id, f.entity_id, f.fact_type, f.value,
                      (SELECT count(*) FROM kc_evidence e WHERE e.fact_id = f.id) AS n_evidence
               FROM kc_facts f
               WHERE f.organization_id = %s AND f.status = 'needs_review' AND f.valid_to IS NULL""", (org,))
        for r in cur.fetchall():
            ent = ents.get(str(r["entity_id"]))
            kind = "unverified_fact" if r["n_evidence"] else "missing_evidence"
            put(kind, str(r["id"]), str(r["entity_id"]),
                (f"Not yet verified: {r['fact_type']} = {r['value']} ({_label(ent)})" if r["n_evidence"] else
                 f"No evidence recorded: {r['fact_type']} = {r['value']} ({_label(ent)})"),
                {"fact_type": r["fact_type"], "value": r["value"], "entity": _label(ent),
                 "evidence_count": r["n_evidence"]}, "needs_review", str(r["id"]))
        # 3. facts that a reviewer verified and that are now part of a conflict
        cur.execute("""SELECT f.id FROM kc_facts f WHERE f.organization_id = %s AND f.status = 'contradiction_flagged'
                       AND f.reviewed_at IS NOT NULL""", (org,))
        reconflicted = {str(r["id"]) for r in cur.fetchall()}
        # 4. failed documents
        cur.execute(
            """SELECT d.id, d.filename, d.error_message, s.client_id
               FROM documents d LEFT JOIN submissions s ON s.id = d.submission_id AND s.organization_id = d.organization_id
               WHERE d.organization_id = %s AND d.status = 'failed'""", (org,))
        for r in cur.fetchall():
            put("failed_document", str(r["id"]), None, f"Document processing failed: {r['filename']}",
                {"filename": r["filename"], "error": r["error_message"]}, "failed", str(r["id"]),
                client_id=str(r["client_id"]) if r["client_id"] else None)
    # 5. coverage findings (Phase 1E engine, current date)
    coverage = {}
    for bid, ent in ents.items():
        if ent["entity_type"] != "business":
            continue
        audit = ci.get_coverage_audit(conn, org, bid)
        for f in (audit or {}).get("findings") or []:
            coverage[f["finding_id"]] = f
            put("coverage_finding", f["finding_id"], None, f"{f['type'].replace('_', ' ').title()}: {f['statement']}",
                {"type": f["type"], "statement": f["statement"], "evidence_sufficient": f.get("evidence_sufficient"),
                 "engine_review": f.get("review"), "review_stale": f.get("review_stale")},
                f.get("review_status"), f.get("fingerprint"), business_id=bid)
    return {"items": out, "reconflicted": reconflicted, "coverage": coverage}


# --------------------------------------------------------------------------
# Queue refresh (reconcile stored items with the engines)
# --------------------------------------------------------------------------

def refresh(conn, org) -> dict:
    """Bring review_items in line with what the engines report. Idempotent;
    serialized per organization."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("review_queue:" + org,))
    cand = _candidates(conn, org)
    items = cand["items"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review_items WHERE organization_id = %s", (org,))
        existing = {(r["kind"], r["subject_id"]): r for r in cur.fetchall()}
    stats = {"created": 0, "reopened": 0, "closed": 0}
    for key, c in items.items():
        it = existing.get(key)
        kind = c["kind"]
        if kind == "coverage_finding":
            decided = c["engine_status"] not in (None, "OPEN") and not c["summary"].get("review_stale")
            if it is None:
                if decided:
                    continue                      # already decided in the Coverage panel; nothing to do
                _create(conn, org, c)
                stats["created"] += 1
                continue
            _sync_content(conn, org, it, c)
            if it["state"] in CLOSED_STATES and c["summary"].get("review_stale"):
                _system_event(conn, org, it, "reopened", "REOPENED",
                              "The finding's evidence changed after the last decision; it needs a new review.")
                stats["reopened"] += 1
            elif it["state"] in OPEN_STATES and decided:
                rev = c["summary"].get("engine_review") or {}
                at = rev.get("at")
                if at and datetime.fromisoformat(at) > it["updated_at"]:
                    _system_event(conn, org, it, "closed", "RESOLVED",
                                  f"Decided outside the queue: {rev.get('decision')} by {rev.get('reviewer')}.")
                    stats["closed"] += 1
            continue
        if it is None:
            _create(conn, org, c)
            stats["created"] += 1
        else:
            _sync_content(conn, org, it, c)
    # items whose subject is no longer reported
    for key, it in existing.items():
        if key in items:
            continue
        kind = it["kind"]
        if kind == "unverified_fact" and it["state"] in CLOSED_STATES and it["subject_id"] in cand["reconflicted"]:
            _system_event(conn, org, it, "reopened", "REOPENED",
                          "A later document conflicts with this verified value; see the conflict item.")
            stats["reopened"] += 1
        elif it["state"] in OPEN_STATES and kind != "coverage_finding":
            _system_event(conn, org, it, "closed", "RESOLVED",
                          "No longer reported by the engines (settled outside the queue or the document changed).")
            stats["closed"] += 1
    return stats


def _sync_content(conn, org, it, c):
    """Keep title / summary / links current without touching updated_at."""
    new = {"title": c["title"], "summary": c["summary"], "engine_status": c["engine_status"],
           "fingerprint": c["fingerprint"], "client_id": c["client_id"], "business_id": c["business_id"]}
    old = {"title": it["title"], "summary": it["summary"], "engine_status": it["engine_status"],
           "fingerprint": it["fingerprint"], "client_id": str(it["client_id"]) if it["client_id"] else None,
           "business_id": str(it["business_id"]) if it["business_id"] else None}
    if json.dumps(new, default=str, sort_keys=True) != json.dumps(old, default=str, sort_keys=True):
        new["summary"] = json.dumps(new["summary"], default=str)
        _set_item(conn, org, str(it["id"]), touch=False, **new)


def _create(conn, org, c):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_items (organization_id, kind, subject_id, business_id, client_id, title, summary,
                                         engine_status, fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (organization_id, kind, subject_id) DO NOTHING RETURNING *""",
            (org, c["kind"], c["subject_id"], c["business_id"], c["client_id"], c["title"],
             json.dumps(c["summary"], default=str), c["engine_status"], c["fingerprint"]))
        row = cur.fetchone()
    if row:
        _add_event(conn, org, str(row["id"]), actor_user_id=None, actor="system", by_system=True, action="created",
                   from_state=None, to_state="OPEN", reason="Reported by the engines.")
    return row


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _public_item(r, users) -> dict:
    a = users.get(str(r["assignee_user_id"])) if r["assignee_user_id"] else None
    return {"item_id": str(r["id"]), "kind": r["kind"], "subject_id": r["subject_id"],
            "business_id": str(r["business_id"]) if r["business_id"] else None,
            "client_id": str(r["client_id"]) if r["client_id"] else None,
            "client_name": r.get("client_name"), "client_tab": CLIENT_TAB[r["kind"]],
            "title": r["title"], "summary": r["summary"], "engine_status": r["engine_status"],
            "state": r["state"], "assignee": a, "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])}


def _users(conn, org):
    with conn.cursor() as cur:
        cur.execute("""SELECT u.id, u.display_name, u.email, m.role, m.status FROM memberships m
                       JOIN users u ON u.id = m.user_id WHERE m.organization_id = %s""", (org,))
        return {str(r["id"]): {"user_id": str(r["id"]), "display_name": r["display_name"], "email": r["email"],
                               "role": r["role"], "active": r["status"] == "active"} for r in cur.fetchall()}


def list_queue(conn, ctx, *, kind=None, state=None, assigned=None, client_id=None, page=1, page_size=50) -> dict:
    org = ctx.organization_id
    clauses, params = ["i.organization_id = %s"], [org]
    if kind:
        clauses.append("i.kind = %s"); params.append(kind)
    if state == "open":
        clauses.append("i.state = ANY(%s)"); params.append(list(OPEN_STATES))
    elif state == "closed":
        clauses.append("i.state = ANY(%s)"); params.append(list(CLOSED_STATES))
    elif state:
        clauses.append("i.state = %s"); params.append(state)
    if assigned == "me":
        clauses.append("i.assignee_user_id = %s"); params.append(ctx.user_id)
    elif assigned == "unassigned":
        clauses.append("i.assignee_user_id IS NULL")
    if client_id:
        clauses.append("i.client_id = %s"); params.append(client_id)
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM review_items i WHERE {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"""SELECT i.*, c.business_name AS client_name FROM review_items i
                LEFT JOIN clients c ON c.id = i.client_id AND c.organization_id = i.organization_id
                WHERE {where}
                ORDER BY CASE i.kind WHEN 'fact_conflict' THEN 0 WHEN 'coverage_finding' THEN 1
                         WHEN 'failed_document' THEN 2 WHEN 'missing_evidence' THEN 3 ELSE 4 END,
                         i.updated_at DESC, i.id
                LIMIT %s OFFSET %s""", (*params, page_size, (page - 1) * page_size))
        rows = cur.fetchall()
        cur.execute("""SELECT kind, state, count(*) AS n FROM review_items WHERE organization_id = %s
                       GROUP BY kind, state""", (org,))
        counts = {}
        for r in cur.fetchall():
            counts.setdefault(r["kind"], {})[r["state"]] = r["n"]
    users = _users(conn, org)
    return {"items": [_public_item(r, users) for r in rows], "total": total, "page": page,
            "page_size": page_size, "counts": counts, "note": ENGINE_NOTE}


def _item(conn, org, item_id) -> dict:
    with conn.cursor() as cur:
        cur.execute("""SELECT i.*, c.business_name AS client_name FROM review_items i
                       LEFT JOIN clients c ON c.id = i.client_id AND c.organization_id = i.organization_id
                       WHERE i.id = %s AND i.organization_id = %s""", (item_id, org))
        row = cur.fetchone()
    if row is None:
        raise ReviewError(404, "Review item not found.")
    return row


def _fact_view(conn, org, fact_id):
    f = db.get_fact(conn, fact_id, org)
    if not f:
        return None
    return {"fact_id": str(f["id"]), "fact_type": f["fact_type"], "value": f["value"], "status": f["status"],
            "effective_from": _iso(f.get("effective_from")), "effective_to": _iso(f.get("effective_to")),
            "current": f.get("valid_to") is None, "reviewed_by": f.get("reviewed_by"),
            "evidence": [_ev_ref(e) for e in kc.get_evidence_for_fact(conn, fact_id, org)]}


def _subject(conn, org, r) -> dict:
    """The engine's current view of the item (read live; nothing cached)."""
    kind, sid = r["kind"], r["subject_id"]
    if kind == "fact_conflict":
        c = db.get_conflict(conn, sid, org)
        if not c:
            return {"available": False}
        facts = [_fact_view(conn, org, fid) for fid in db.get_conflict_fact_ids(conn, sid)]
        return {"available": True, "conflict_status": c["status"], "fact_type": c["fact_type"],
                "competing_values": [f for f in facts if f],
                "rule": "A person chooses the right value; the system never picks the newest one."}
    if kind in ("unverified_fact", "missing_evidence"):
        f = _fact_view(conn, org, sid)
        return {"available": f is not None, "fact": f}
    if kind == "coverage_finding":
        if not r["business_id"]:
            return {"available": False}
        f = next((x for x in (ci.get_coverage_findings(conn, org, str(r["business_id"])) or {}).get("findings", [])
                  if x["finding_id"] == sid), None)
        if f is None:
            return {"available": False}
        ci._attach_reviews(conn, org, str(r["business_id"]), [f], "finding")
        return {"available": True, "finding": {k: f.get(k) for k in (
            "finding_id", "type", "statement", "rationale", "conclusion", "evidence_sufficient", "needs_review",
            "review_status", "review", "review_stale", "review_history", "policy", "coverage", "subject")},
            "evidence": [_ev_ref(e) for e in f.get("evidence") or []]}
    if kind == "failed_document":
        with conn.cursor() as cur:
            cur.execute("SELECT id, filename, status, error_message FROM documents WHERE id = %s "
                        "AND organization_id = %s", (sid, org))
            d = cur.fetchone()
        return {"available": d is not None,
                "document": d and {"document_id": str(d["id"]), "filename": d["filename"], "status": d["status"],
                                   "error": d.get("error_message")}}
    return {"available": False}


def allowed_actions(ctx, r) -> list[str]:
    kind, state = r["kind"], r["state"]
    mine = r["assignee_user_id"] is None or str(r["assignee_user_id"]) == ctx.user_id
    boss = ctx.can("reviews.assign")
    out = []
    if boss and state in TRANSITIONS["assign"][0]:
        out.append("assign")
    if boss and state in TRANSITIONS["unassign"][0]:
        out.append("unassign")
    if not _can_work(ctx, kind) or not (mine or boss):
        return out
    for a in ("start", "needs_info", "reopen"):
        if state in TRANSITIONS[a][0] and (a != "reopen" or kind in MANUAL_REOPEN_KINDS):
            out.append(a)
    if state in TRANSITIONS["decide"][0]:
        out += [f"decide:{d}" for d in DECISIONS[kind]]
    return out


def get_detail(conn, ctx, item_id) -> dict:
    org = ctx.organization_id
    r = _item(conn, org, item_id)
    users = _users(conn, org)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review_events WHERE item_id = %s AND organization_id = %s ORDER BY id",
                    (item_id, org))
        events = [{"at": _iso(e["at"]), "actor": e["actor"], "by_system": e["by_system"], "action": e["action"],
                   "from_state": e["from_state"], "to_state": e["to_state"], "decision": e["decision"],
                   "reason": e["reason"], "evidence": e["evidence"], "metadata": e["metadata"]}
                  for e in cur.fetchall()]
    return {"item": _public_item(r, users), "subject": _subject(conn, org, r), "history": events,
            "allowed_actions": allowed_actions(ctx, r),
            "decisions": {d: {"result_state": s, "reason_required": need} for d, (s, need) in DECISIONS[r["kind"]].items()},
            "assignable": [u for u in users.values() if u["active"] and _role_can_work(u["role"], r["kind"])]
            if ctx.can("reviews.assign") else [],
            "note": ENGINE_NOTE}


def _role_can_work(role, kind) -> bool:
    perms = auth.ROLE_PERMISSIONS.get(role, set())
    return any(p in perms for p in KIND_PERMISSIONS[kind])


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------

def _check(ctx, r, action):
    allowed_from, _ = TRANSITIONS[action]
    if action in ("assign", "unassign"):
        if not ctx.can("reviews.assign"):
            raise ReviewError(403, "Only an Owner or Admin can assign review items.")
    else:
        if not _can_work(ctx, r["kind"]):
            raise ReviewError(403, "Your role cannot work on this kind of review item.")
        if r["assignee_user_id"] and str(r["assignee_user_id"]) != ctx.user_id and not ctx.can("reviews.assign"):
            raise ReviewError(403, "This item is assigned to another reviewer.")
    if r["state"] not in allowed_from:
        raise ReviewError(409, f"'{action}' is not allowed while the item is {r['state']}.")


def _audit(conn, ctx, r, action, from_state, to_state, **meta):
    auth.audit(conn, ctx, f"review.{action}", "review_item", str(r["id"]),
               {"kind": r["kind"], "subject_id": r["subject_id"], "from": from_state, "to": to_state,
                **{k: v for k, v in meta.items() if v is not None}})


def assign(conn, ctx, item_id, user_id):
    org = ctx.organization_id
    r = _item(conn, org, item_id)
    if user_id is None:
        _check(ctx, r, "unassign")
        to, action = "OPEN", "unassign"
    else:
        _check(ctx, r, "assign")
        u = _users(conn, org).get(user_id)
        if not u or not u["active"]:
            raise ReviewError(400, "That person is not an active member of this organization.")
        if not _role_can_work(u["role"], r["kind"]):
            raise ReviewError(400, f"A {u['role']} cannot work on this kind of item.")
        to, action = "ASSIGNED", "assign"
    _set_item(conn, org, item_id, state=to, assignee_user_id=user_id)
    _add_event(conn, org, item_id, actor_user_id=ctx.user_id, actor=ctx.actor, by_system=False, action=action,
               from_state=r["state"], to_state=to, metadata={"assignee_user_id": user_id})
    _audit(conn, ctx, r, action, r["state"], to, assignee_user_id=user_id)
    return get_detail(conn, ctx, item_id)


def start(conn, ctx, item_id):
    org = ctx.organization_id
    r = _item(conn, org, item_id)
    _check(ctx, r, "start")
    assignee = str(r["assignee_user_id"]) if r["assignee_user_id"] else ctx.user_id
    _set_item(conn, org, item_id, state="IN_REVIEW", assignee_user_id=assignee)
    _add_event(conn, org, item_id, actor_user_id=ctx.user_id, actor=ctx.actor, by_system=False, action="start",
               from_state=r["state"], to_state="IN_REVIEW", metadata={"assignee_user_id": assignee})
    _audit(conn, ctx, r, "start", r["state"], "IN_REVIEW")
    return get_detail(conn, ctx, item_id)


def needs_info(conn, ctx, item_id, reason):
    org = ctx.organization_id
    r = _item(conn, org, item_id)
    _check(ctx, r, "needs_info")
    reason = _clean(reason)
    if not reason:
        raise ReviewError(400, "Say what information is needed.")
    _set_item(conn, org, item_id, state="NEEDS_INFO")
    _add_event(conn, org, item_id, actor_user_id=ctx.user_id, actor=ctx.actor, by_system=False, action="needs_info",
               from_state=r["state"], to_state="NEEDS_INFO", reason=reason)
    _audit(conn, ctx, r, "needs_info", r["state"], "NEEDS_INFO", reason=reason)
    return get_detail(conn, ctx, item_id)


def reopen(conn, ctx, item_id, reason):
    org = ctx.organization_id
    r = _item(conn, org, item_id)
    _check(ctx, r, "reopen")
    if r["kind"] not in MANUAL_REOPEN_KINDS:
        raise ReviewError(409, "Fact items reopen automatically when new evidence changes them; correct the fact "
                               "instead.")
    reason = _clean(reason)
    if not reason:
        raise ReviewError(400, "A reason is required to reopen.")
    _set_item(conn, org, item_id, state="REOPENED")
    _add_event(conn, org, item_id, actor_user_id=ctx.user_id, actor=ctx.actor, by_system=False, action="reopen",
               from_state=r["state"], to_state="REOPENED", reason=reason)
    _audit(conn, ctx, r, "reopen", r["state"], "REOPENED", reason=reason)
    return get_detail(conn, ctx, item_id)


def decide(conn, ctx, item_id, decision, reason=None, value=None, fact_id=None):
    org = ctx.organization_id
    r = _item(conn, org, item_id)
    _check(ctx, r, "decide")
    kind = r["kind"]
    if decision not in DECISIONS[kind]:
        raise ReviewError(400, f"Decision must be one of: {', '.join(DECISIONS[kind])}.")
    to, need_reason = DECISIONS[kind][decision]
    reason = _clean(reason)
    if need_reason and not reason:
        raise ReviewError(400, "A reason is required for this decision.")
    evidence, meta = [], {}
    try:
        if kind == "fact_conflict":
            c = db.get_conflict(conn, r["subject_id"], org)
            if not c or c["status"] != "unresolved":
                raise ReviewError(409, "This conflict is no longer open; refresh the queue.")
            ids = db.get_conflict_fact_ids(conn, r["subject_id"])
            if fact_id not in ids:
                raise ReviewError(400, "Choose one of the competing facts to correct.")
            chosen = db.get_fact(conn, fact_id, org)
            new_value = (value or "").strip() or chosen["value"]
            for fid in ids:
                evidence += [_ev_ref(e) for e in kc.get_evidence_for_fact(conn, fid, org)]
            new = kc.update_fact(conn, fact_id, org, new_value=new_value, reviewed_by=ctx.actor)
            after = db.get_conflict(conn, r["subject_id"], org)
            if not new or after["status"] != "resolved":
                raise ReviewError(409, "The correction did not settle the conflict; nothing was changed.")
            meta = {"corrected_fact_id": fact_id, "new_fact_id": str(new["id"]), "competing_fact_ids": ids}
            audit_fact(conn, ctx, "fact.corrected", fact_id, reason, new_fact_id=str(new["id"]),
                       resolved_conflict_id=r["subject_id"], via="review_queue")
        elif kind in ("unverified_fact", "missing_evidence"):
            fact = db.get_fact(conn, r["subject_id"], org)
            if not fact or fact["status"] != "needs_review" or fact.get("valid_to") is not None:
                raise ReviewError(409, "This fact is no longer awaiting review; refresh the queue.")
            evidence = [_ev_ref(e) for e in kc.get_evidence_for_fact(conn, r["subject_id"], org)]
            if decision == "correct":
                if not (value or "").strip():
                    raise ReviewError(400, "Provide the corrected value.")
                new = kc.update_fact(conn, r["subject_id"], org, new_value=value.strip(), reviewed_by=ctx.actor)
                meta = {"new_fact_id": str(new["id"])}
                audit_fact(conn, ctx, "fact.corrected", r["subject_id"], reason, new_fact_id=str(new["id"]),
                           via="review_queue")
            else:
                kc.review_fact(conn, r["subject_id"], org, action=decision, reviewed_by=ctx.actor)
                audit_fact(conn, ctx, f"fact.{'verified' if decision == 'verify' else 'rejected'}",
                           r["subject_id"], reason, via="review_queue")
        elif kind == "coverage_finding":
            if not r["business_id"]:
                raise ReviewError(409, "This finding is no longer linked to a business.")
            out = ci.review(conn, org, str(r["business_id"]), "finding", r["subject_id"], decision, ctx.actor, reason)
            if out is None:
                raise ReviewError(409, "This finding is no longer current; refresh the queue.")
            evidence = [_ev_ref(e) for e in out.get("evidence") or []]
            audit_coverage(conn, ctx, "finding", r["subject_id"], str(r["business_id"]), decision, reason,
                           via="review_queue")
        elif kind == "failed_document":
            pass                                   # acknowledgement only; the document itself is unchanged
    except ValueError as e:                        # the existing services refused it
        raise ReviewError(409, str(e))
    _set_item(conn, org, item_id, state=to, decided_fingerprint=r["fingerprint"],
              assignee_user_id=str(r["assignee_user_id"]) if r["assignee_user_id"] else ctx.user_id)
    _add_event(conn, org, item_id, actor_user_id=ctx.user_id, actor=ctx.actor, by_system=False, action="decide",
               from_state=r["state"], to_state=to, decision=decision, reason=reason, evidence=evidence[:20],
               metadata=meta)
    _audit(conn, ctx, r, "decide", r["state"], to, decision=decision, reason=reason)
    return get_detail(conn, ctx, item_id)


# --------------------------------------------------------------------------
# Audit helpers shared with the existing direct endpoints (v0.16.0)
# --------------------------------------------------------------------------

def audit_fact(conn, ctx, action, fact_id, reason, **meta):
    auth.audit(conn, ctx, action, "kc_fact", fact_id, {"reason": reason, **{k: v for k, v in meta.items() if v}})


def audit_coverage(conn, ctx, subject_type, subject_id, business_id, decision, reason, **meta):
    auth.audit(conn, ctx, "coverage.review_recorded", f"coverage_{subject_type}", subject_id,
               {"business_id": business_id, "decision": decision, "reason": reason,
                **{k: v for k, v in meta.items() if v}})
