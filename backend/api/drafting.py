"""
Phase 4 — /drafts* (create, edit, approve/revoke, export, fill-form) and
Phase 5's house-style endpoints (which share this router because they are
drafting's own learned guidance).
"""
import logging
import os

import drafting
import export_docx
import feedback
from fastapi import APIRouter, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel

import db
import llm
from llm import LLMError
from parsers import ParseError, parse_file

from .deps import _actor, _resolve_organization, _validate_uuid
from .gaps import _carriers_from_match

logger = logging.getLogger(__name__)

router = APIRouter()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class DraftEdit(BaseModel):
    title: str
    body: str


def _draft_response(row: dict) -> dict:
    return {
        "draft_id": str(row["id"]),
        "client_id": str(row["client_id"]),
        "carrier_id": str(row["carrier_id"]) if row.get("carrier_id") else None,
        "carrier_name": row.get("carrier_name"),
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "warnings": row.get("warnings") or [],
        "stats": row.get("stats") or {},
        "model": row.get("model"),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/draft-kinds")
def draft_kinds():
    """The document kinds that can be drafted, for the UI to offer."""
    return {"kinds": [{"id": k, "label": v} for k, v in drafting.KINDS.items()]}


@router.post("/clients/{client_id}/drafts")
def create_draft(client_id: str, carrier_id: str | None = None,
                 kind: str = "submission",
                 x_organization_id: str | None = Header(None)):
    """
    Write a document for this client.

    Produces a DRAFT and nothing else — there is no send path here, by
    design. A licensed human is accountable for what reaches an insurer.

    Without carrier_id, the best-fitting carrier from the latest match is
    used, since that is the one a broker would submit to first.
    """
    client_id = _validate_uuid(client_id, "client id")

    kind = (kind or "submission").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown document kind. Choose one of: {', '.join(drafting.KINDS)}.",
        )

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        client = db.get_client(conn, client_id, organization_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        token_map = db.get_client_tokens(conn, client_id, organization_id)
        match = db.get_latest_match(conn, client_id, organization_id)

        # Guidance learned from how this team edits its own drafts, if any.
        style_row = db.get_house_style(conn, kind, organization_id)
        house_style = style_row["guidance"] if style_row else None

        target = _validate_uuid(carrier_id, "carrier id") if carrier_id else None
        if not target and match:
            # First carrier cited by the best-fitting match.
            for result in match["results"]:
                for source in result.get("sources") or []:
                    if source.get("carrier_id"):
                        target = source["carrier_id"]
                        break
                if target:
                    break

        carrier = db.get_carrier(conn, target, organization_id) if target else None
        carrier_name = carrier["name"] if carrier else None

        attachments = []
        seen = set()
        for row in db.list_client_documents(conn, client_id, organization_id):
            key = " ".join(row["requirement"].casefold().split())
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                {
                    "requirement": row["requirement"],
                    "filename": row["filename"],
                    "verdict": row["verdict"],
                    "doc_type": row.get("doc_type"),
                    "key_facts": row.get("key_facts") or [],
                }
            )

        gap_items = []
        for report in db.get_latest_gap_reports(conn, client_id, organization_id):
            # A chase email goes to the client about everything outstanding,
            # so it takes every carrier's report. The carrier-facing kinds
            # take only the carrier they are addressed to.
            if kind != "chase" and target and str(report["carrier_id"]) != str(target):
                continue
            gap_items.extend(report["items"] or [])

        if kind == "chase":
            # Deduplicate: two carriers asking for the same document should
            # produce one line in the email, not two.
            seen_requirements = set()
            deduped = []
            for item in gap_items:
                key = " ".join(str(item.get("requirement", "")).casefold().split())
                if not key or key in seen_requirements:
                    continue
                seen_requirements.add(key)
                deduped.append(item)
            gap_items = deduped

    try:
        if kind == "chase":
            # Addressed to the client, so it needs the gap report rather than
            # the carrier dossier. Every outstanding item across carriers is
            # chased at once — one email, not one per insurer.
            result = drafting.draft_chase(
                client=client, gap_items=gap_items, token_map=token_map,
                house_style=house_style,
            )
        elif kind == "proposal":
            result = drafting.draft_proposal(
                client=client,
                carrier_name=carrier_name,
                match=match,
                attachments=attachments,
                gap_items=gap_items,
                token_map=token_map,
                house_style=house_style,
            )
        else:
            result = drafting.draft_submission(
                client=client,
                carrier_name=carrier_name,
                match=match,
                attachments=attachments,
                gap_items=gap_items,
                token_map=token_map,
                house_style=house_style,
            )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Drafting failed")
        raise HTTPException(status_code=500, detail=f"Drafting failed: {exc}")

    with db.get_connection() as conn:
        row = db.save_draft(
            conn,
            client_id=client_id,
            organization_id=organization_id,
            carrier_id=None if kind == "chase" else target,
            kind=kind,
            title=result["title"],
            body=result["body"],
            body_redacted=result["body_redacted"],
            warnings=result["warnings"],
            stats=result["stats"],
            model=llm.OLLAMA_MODEL,
        )
        row["carrier_name"] = carrier_name
        db.log_draft_event(
            conn,
            draft_id=str(row["id"]),
            organization_id=organization_id,
            event="created",
            actor=llm.OLLAMA_MODEL,
            detail=(
                f"{drafting.KINDS[kind]} drafted"
                + (f" for {carrier_name}." if carrier_name and kind != "chase" else ".")
            ),
        )

    return _draft_response(row)


@router.get("/clients/{client_id}/drafts")
def list_client_drafts(client_id: str, x_organization_id: str | None = Header(None)):
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.list_drafts(conn, client_id, organization_id)
    return {"client_id": client_id, "drafts": [_draft_response(r) for r in rows]}


@router.put("/drafts/{draft_id}")
def edit_draft(draft_id: str, body: DraftEdit,
               x_organization_id: str | None = Header(None)):
    """
    Save a human edit.

    Warnings are recomputed on the edited text rather than carried over: an
    edit that fixes a stray placeholder should clear its warning, and one
    that introduces a bracket placeholder should raise a new one.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    title = " ".join((body.title or "").split())
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="A draft cannot be empty.")

    warnings = drafting._warnings_for(text)

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        before = db.get_draft(conn, draft_id, organization_id)
        if not before:
            raise HTTPException(status_code=404, detail="Draft not found.")

        row = db.update_draft_body(
            conn, draft_id, organization_id, title=title[:200] or "Untitled draft",
            body=text, warnings=warnings,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")

        # An edit after sign-off is worth calling out in the trail: the
        # approval that came before it no longer covers this text.
        detail = "Edited by hand."
        if before["status"] == "approved":
            detail = "Edited after approval — the earlier sign-off no longer applies."
        db.log_draft_event(conn, draft_id=draft_id, organization_id=organization_id,
                           event="edited", actor=_actor()[:120], detail=detail)

        # The edit itself is the feedback signal — what was drafted against
        # what a person actually wanted. Stored whole so guidance can be
        # re-derived later from better examples than we have today.
        stats = feedback.summarise_edit(before["body"], text)
        db.save_correction(
            conn,
            kind="draft_edit",
            organization_id=organization_id,
            client_id=str(before["client_id"]),
            carrier_id=str(before["carrier_id"]) if before.get("carrier_id") else None,
            subject=before["kind"],
            before_text=before["body"],
            after_text=text,
            meta=stats,
        )

        full = db.get_draft(conn, draft_id, organization_id)

    return _draft_response(full or row)


@router.delete("/drafts/{draft_id}")
def remove_draft(draft_id: str, x_organization_id: str | None = Header(None)):
    draft_id = _validate_uuid(draft_id, "draft id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if not db.delete_draft(conn, draft_id, organization_id):
            raise HTTPException(status_code=404, detail="Draft not found.")
    return {"deleted": draft_id}


# --------------------------------------------------------------------------
# Phase 4, part 2 — review and approval
# --------------------------------------------------------------------------


class Approval(BaseModel):
    """Phase 2A: the approver is the signed-in user (approved_by is ignored,
    kept only so older clients do not break)."""

    approved_by: str | None = None
    note: str | None = None


class Revocation(BaseModel):
    reason: str | None = None


def _blocking(warnings: list[dict]) -> list[dict]:
    """Warnings severe enough to stop a sign-off."""
    return [w for w in warnings or [] if w.get("level") == "error"]


@router.get("/drafts/{draft_id}")
def read_draft(draft_id: str, x_organization_id: str | None = Header(None)):
    """One draft, with its full history."""
    draft_id = _validate_uuid(draft_id, "draft id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_draft(conn, draft_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")
        events = db.get_draft_events(conn, draft_id, organization_id)

    payload = _draft_response(row)
    payload["history"] = [
        {
            "event": e["event"],
            "actor": e["actor"],
            "detail": e["detail"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: str, body: Approval,
                  x_organization_id: str | None = Header(None)):
    """
    Sign a draft off.

    Approval is a human act and is recorded as one. Two guards:

      * A name is required. An approval with nobody attached to it is not an
        approval, and months later "who agreed this went out" is the question
        that actually gets asked.
      * Error-level warnings block it. An unreplaced placeholder means the
        text is wrong, and signing off wrong text is exactly what a review
        step exists to prevent. Lesser warnings are shown but do not block —
        the reviewer can see them and judge.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    approver = _actor()

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_draft(conn, draft_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")

        blocking = _blocking(row.get("warnings"))
        if blocking:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This draft cannot be approved yet: "
                    + " ".join(w["text"] for w in blocking)
                ),
            )

        if row["status"] == "approved":
            raise HTTPException(
                status_code=409, detail="This draft is already approved."
            )

        db.set_draft_status(conn, draft_id, organization_id, "approved")
        db.log_draft_event(
            conn,
            draft_id=draft_id,
            organization_id=organization_id,
            event="approved",
            actor=approver[:120],
            detail=" ".join((body.note or "").split())[:300] or None,
        )
        import auth
        auth.audit(conn, auth.current(), "draft.approved", "draft", draft_id)
        updated = db.get_draft(conn, draft_id, organization_id)
        events = db.get_draft_events(conn, draft_id, organization_id)

    payload = _draft_response(updated)
    payload["history"] = [
        {
            "event": e["event"],
            "actor": e["actor"],
            "detail": e["detail"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


@router.post("/drafts/{draft_id}/revoke")
def revoke_draft(draft_id: str, body: Revocation,
                 x_organization_id: str | None = Header(None)):
    """
    Withdraw a sign-off.

    The approval event stays in the trail — revoking records a second event
    rather than erasing the first. A history that can be rewritten is not a
    history.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_draft(conn, draft_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")
        if row["status"] != "approved":
            raise HTTPException(
                status_code=409, detail="This draft is not approved."
            )

        db.set_draft_status(conn, draft_id, organization_id, "edited")
        db.log_draft_event(
            conn,
            draft_id=draft_id,
            organization_id=organization_id,
            event="revoked",
            actor=_actor()[:120],
            detail=" ".join((body.reason or "").split())[:300] or None,
        )
        import auth
        auth.audit(conn, auth.current(), "draft.revoked", "draft", draft_id)
        updated = db.get_draft(conn, draft_id, organization_id)
        events = db.get_draft_events(conn, draft_id, organization_id)

    payload = _draft_response(updated)
    payload["history"] = [
        {
            "event": e["event"],
            "actor": e["actor"],
            "detail": e["detail"],
            "at": e["created_at"].isoformat(),
        }
        for e in events
    ]
    return payload


# --------------------------------------------------------------------------
# Phase 4, part 3 — export
# --------------------------------------------------------------------------


@router.get("/drafts/{draft_id}/export")
def export_draft(draft_id: str, x_organization_id: str | None = Header(None)):
    """
    Download a draft as a .docx.

    Unapproved drafts export too — a broker often wants to read one in Word,
    or send it to a colleague, before signing it off. They are stamped
    "DRAFT — NOT YET APPROVED FOR SENDING" inside the document, because the
    file travels on its own and the status badge in the UI does not go with
    it.
    """
    draft_id = _validate_uuid(draft_id, "draft id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_draft(conn, draft_id, organization_id)
        if not row:
            raise HTTPException(status_code=404, detail="Draft not found.")
        client = db.get_client(conn, str(row["client_id"]), organization_id)
        events = db.get_draft_events(conn, draft_id, organization_id)

    # The trail is the record of who signed off, not a column on the draft.
    approved_by = approved_at = None
    for event in events:
        if event["event"] == "approved":
            approved_by = event["actor"]
            approved_at = event["created_at"].isoformat()

    try:
        data = export_docx.build(
            title=row["title"],
            body=row["body"],
            status=row["status"],
            client_name=(client or {}).get("business_name"),
            carrier_name=row.get("carrier_name"),
            approved_by=approved_by,
            approved_at=approved_at,
            kind=row["kind"],
        )
    except Exception as exc:
        logger.exception("Export failed")
        raise HTTPException(status_code=500, detail=f"Could not build the file: {exc}")

    filename = export_docx.filename_for(
        client_name=(client or {}).get("business_name"),
        carrier_name=row.get("carrier_name"),
        status=row["status"],
        kind=row["kind"],
    )
    import auth
    with db.get_connection() as conn:
        db.log_draft_event(conn, draft_id=draft_id, organization_id=organization_id, event="exported",
                           actor=_actor()[:120], detail=f"{row['status']} draft exported as .docx")
        auth.audit(conn, auth.current(), "draft.exported", "draft", draft_id, {"status": row["status"]})

    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Without this the browser cannot read the filename off a fetch()
            # response, and the download lands as "export" with no extension.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


# --------------------------------------------------------------------------
# Phase 5 — house style (drafting's own learned guidance)
# --------------------------------------------------------------------------


class StyleEdit(BaseModel):
    guidance: str


@router.get("/house-style/{kind}")
def read_house_style(kind: str, x_organization_id: str | None = Header(None)):
    kind = (kind or "").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(status_code=400, detail="Unknown document kind.")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.get_house_style(conn, kind, organization_id)
    if not row:
        return {"kind": kind, "guidance": "", "derived_from": 0,
                "edited_by_hand": False, "updated_at": None}
    return {
        "kind": kind,
        "guidance": row["guidance"],
        "derived_from": row["derived_from"],
        "edited_by_hand": row["edited_by_hand"],
        "updated_at": row["updated_at"].isoformat(),
    }


@router.post("/house-style/{kind}/learn")
def learn_house_style(kind: str, x_organization_id: str | None = Header(None)):
    """
    Re-derive guidance from recent edits to this kind of document.

    Deliberately on request rather than continuously: a rule that changes
    under you between one draft and the next is impossible to reason about,
    and the broker should choose when the tool updates its habits.
    """
    kind = (kind or "").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(status_code=400, detail="Unknown document kind.")

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"Ollama is not reachable at {OLLAMA_HOST}.",
        )

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        edits = [
            e for e in db.recent_corrections(conn, "draft_edit", organization_id, limit=40)
            if e["subject"] == kind
        ]

    try:
        result = feedback.distil(edits)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with db.get_connection() as conn:
        db.save_house_style(
            conn,
            kind=kind,
            organization_id=organization_id,
            guidance=result["guidance"],
            derived_from=result["derived_from"],
            edited_by_hand=False,
        )

    return {
        "kind": kind,
        "guidance": result["guidance"],
        "observations": result["observations"],
        "derived_from": result["derived_from"],
        "note": result["note"],
    }


@router.put("/house-style/{kind}")
def edit_house_style(kind: str, body: StyleEdit,
                     x_organization_id: str | None = Header(None)):
    """
    Edit the guidance by hand.

    A broker who disagrees with what the tool concluded should be able to
    just fix it, rather than wait for enough new edits to outvote it.
    """
    kind = (kind or "").strip().lower()
    if kind not in drafting.KINDS:
        raise HTTPException(status_code=400, detail="Unknown document kind.")

    guidance = (body.guidance or "").strip()

    # The same guardrail as the distiller. A rule typed by hand is no safer
    # than one a model suggested, and this one would be applied to every
    # future document without anybody seeing it again.
    kept, dropped = [], []
    for line in guidance.splitlines():
        line = line.strip()
        if not line:
            continue
        (dropped if feedback._is_unsafe(line) else kept).append(line)

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        row = db.save_house_style(
            conn, kind=kind, organization_id=organization_id,
            guidance="\n".join(kept[:6]),
            derived_from=0, edited_by_hand=True,
        )

    return {
        "kind": kind,
        "guidance": row["guidance"],
        "derived_from": row["derived_from"],
        "edited_by_hand": True,
        "updated_at": row["updated_at"].isoformat(),
        "dropped": dropped,
    }


# --------------------------------------------------------------------------
# Fill an insurer's own proposal form
# --------------------------------------------------------------------------


@router.post("/clients/{client_id}/drafts/fill-form")
async def fill_proposal_form(
    client_id: str,
    file: UploadFile = File(...),
    carrier_id: str | None = Form(None),
    x_organization_id: str | None = Header(None),
):
    """
    Fill in a blank proposal form from the client's file.

    Takes the form itself as an upload, because a proposal form is the
    insurer's document, not ours — every carrier's is different, and reading
    theirs is the only way to answer the fields they actually ask for.

    Fields the file cannot answer come back blank and listed, never guessed.
    """
    client_id = _validate_uuid(client_id, "client id")

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"Ollama is not reachable at {OLLAMA_HOST}.",
        )

    data = await file.read()
    filename = file.filename or "form"
    try:
        parsed = parse_file(filename, data)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        client = db.get_client(conn, client_id, organization_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")
        token_map = db.get_client_tokens(conn, client_id, organization_id)
        match = db.get_latest_match(conn, client_id, organization_id)

        target = _validate_uuid(carrier_id, "carrier id") if carrier_id else None
        if not target and match:
            target = (_carriers_from_match(conn, match, organization_id) or [None])[0]
        carrier = db.get_carrier(conn, target, organization_id) if target else None

        attachments = []
        seen = set()
        for row in db.list_client_documents(conn, client_id, organization_id):
            key = " ".join(row["requirement"].casefold().split())
            if key in seen:
                continue
            seen.add(key)
            attachments.append({
                "requirement": row["requirement"], "filename": row["filename"],
                "verdict": row["verdict"], "doc_type": row.get("doc_type"),
                "key_facts": row.get("key_facts") or [],
            })

        gap_items = []
        for report in db.get_latest_gap_reports(conn, client_id, organization_id):
            if target and str(report["carrier_id"]) != str(target):
                continue
            gap_items.extend(report["items"] or [])

    try:
        result = drafting.fill_form(
            form_text=parsed.text,
            client=client,
            carrier_name=carrier["name"] if carrier else None,
            match=match,
            attachments=attachments,
            gap_items=gap_items,
            token_map=token_map,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Form filling failed")
        raise HTTPException(status_code=500, detail=f"Could not fill that form: {exc}")

    with db.get_connection() as conn:
        row = db.save_draft(
            conn, client_id=client_id, organization_id=organization_id,
            carrier_id=target, kind="form",
            title=result["title"], body=result["body"],
            body_redacted=result["body_redacted"], warnings=result["warnings"],
            stats=result["stats"], model=llm.OLLAMA_MODEL,
        )
        row["carrier_name"] = carrier["name"] if carrier else None
        db.log_draft_event(
            conn, draft_id=str(row["id"]), organization_id=organization_id,
            event="created", actor=llm.OLLAMA_MODEL,
            detail=(
                f"Filled {result['stats']['answered']} of "
                f"{result['stats']['fields']} fields from {filename}."
            ),
        )

    payload = _draft_response(row)
    payload["fields"] = result["fields"]
    return payload
