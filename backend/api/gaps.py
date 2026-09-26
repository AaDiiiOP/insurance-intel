"""Phase 3 — /clients/{id}/documents (held/missing) and gap analysis."""
import logging
import os

import gaps
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import db
import llm
from llm import LLMError

from .deps import _resolve_organization, _validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")


class DocumentLists(BaseModel):
    """The paperwork a client holds, and what they have said they lack."""

    held: list[str] = []
    missing: list[str] = []


@router.put("/clients/{client_id}/documents")
def set_documents(client_id: str, body: DocumentLists,
                  x_organization_id: str | None = Header(None)):
    """
    Record what paperwork the client has.

    Separate from the intake form because it changes on its own schedule —
    a broker chases a document, it arrives, and the checklist moves without
    anything else about the client changing.
    """
    client_id = _validate_uuid(client_id, "client id")

    def clean(values: list[str]) -> list[str]:
        out = []
        for value in values:
            name = " ".join(str(value).split()).strip(" .-")
            if name and len(name) <= 120 and name.casefold() not in {
                o.casefold() for o in out
            }:
                out.append(name)
        return out[:40]

    held, missing = clean(body.held), clean(body.missing)

    # A document cannot be both held and missing. The held list wins: it is
    # the one a broker edits after chasing something down, so it is the more
    # recent statement of fact.
    held_keys = {h.casefold() for h in held}
    missing = [m for m in missing if m.casefold() not in held_keys]

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        if not db.get_client(conn, client_id, organization_id):
            raise HTTPException(status_code=404, detail="Client not found.")
        db.set_client_documents(conn, client_id, organization_id, held=held, missing=missing)

    return {"client_id": client_id, "documents_held": held, "documents_missing": missing}


def _carriers_from_match(conn, latest: dict | None, organization_id: str) -> list[str]:
    """
    Which carriers a match points at, in best-fit order.

    Tried in order, because each fallback is less precise than the last:

      1. carrier_id on a cited source. Exact, but only present when the model
         returned usable citation numbers — with a small model it often does
         not, and deriving the carrier from citations alone was why gap
         analysis used to fail on a perfectly good match.
      2. The carrier NAME the match itself names, resolved against the
         carriers on record. Every surviving match has one, so this works
         whenever the match does.

    Returns an empty list only when neither yields anything, leaving the
    caller to widen the net rather than refuse.
    """
    if not latest:
        return []

    targets: list[str] = []
    for match in latest.get("results") or []:
        for source in match.get("sources") or []:
            identifier = source.get("carrier_id")
            if identifier and identifier not in targets:
                targets.append(identifier)

    if targets:
        return targets

    # Fall back to matching on name.
    by_name = {
        " ".join(c["name"].casefold().split()): str(c["id"])
        for c in db.list_carriers(conn, organization_id)
        if c.get("has_summary")
    }
    for match in latest.get("results") or []:
        name = " ".join(str(match.get("carrier") or "").casefold().split())
        identifier = by_name.get(name)
        if not identifier and name:
            # Loose match: the model sometimes writes "Suvidha Trust Bank Ltd"
            # for a carrier recorded as "Suvidha Trust Bank".
            for known, known_id in by_name.items():
                if known and (known in name or name in known):
                    identifier = known_id
                    break
        if identifier and identifier not in targets:
            targets.append(identifier)

    return targets


@router.post("/clients/{client_id}/gaps")
def run_gap_analysis(client_id: str, carrier_id: str | None = None,
                     x_organization_id: str | None = Header(None)):
    """
    Check a client's file against what each carrier requires.

    Runs against the carriers the latest match recommended, since those are
    the ones actually in play. Pass carrier_id to check one specific carrier
    instead — useful before a match has been run, or to re-check after
    chasing a document.
    """
    client_id = _validate_uuid(client_id, "client id")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        client = db.get_client(conn, client_id, organization_id)
        if not client:
            raise HTTPException(status_code=404, detail="Client not found.")

        # Attached documents are the strongest evidence a requirement is met,
        # so they travel with the client record into the analysis. Newest per
        # requirement wins: re-uploading after a rejection should replace the
        # earlier verdict, not sit behind it.
        attachments = []
        seen_requirements = set()
        for row in db.list_client_documents(conn, client_id, organization_id):
            key = " ".join(row["requirement"].casefold().split())
            if key in seen_requirements:
                continue
            seen_requirements.add(key)
            attachments.append(
                {
                    "requirement": row["requirement"],
                    "filename": row["filename"],
                    "verdict": row["verdict"],
                }
            )
        client["_attachments"] = attachments

        if carrier_id:
            targets = [_validate_uuid(carrier_id, "carrier id")]
        else:
            latest = db.get_latest_match(conn, client_id, organization_id)
            targets = _carriers_from_match(conn, latest, organization_id)

        if not targets:
            # Nothing could be resolved from the match — usually because the
            # model returned recommendations without usable citations. Rather
            # than refuse, check against every briefed carrier: a broker who
            # asked what paperwork is needed is better served by a slightly
            # wider answer than by an error.
            with_summaries = [
                str(c["id"])
                for c in db.list_carriers(conn, organization_id)
                if c.get("has_summary")
            ]
            targets = with_summaries[:5]

        if not targets:
            raise HTTPException(
                status_code=409,
                detail=(
                    "No carrier has a briefing yet, so there are no "
                    "requirements to check against. Add a carrier and "
                    "generate its briefing first."
                ),
            )

        carriers = []
        for identifier in targets:
            carrier = db.get_carrier(conn, identifier, organization_id)
            summary = db.get_summary(conn, identifier, organization_id)
            if carrier and summary:
                carriers.append((carrier, summary))

    if not carriers:
        raise HTTPException(
            status_code=409,
            detail=(
                "None of those carriers has a briefing yet. Generate one so "
                "its requirements are on record."
            ),
        )

    if not llm.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ollama is not reachable at {OLLAMA_HOST}, or "
                f"'{llm.OLLAMA_MODEL}' is not pulled."
            ),
        )

    reports = []
    failures = []
    for carrier, summary in carriers:
        name = carrier["name"]
        try:
            result = gaps.analyse(
                client=client,
                carrier_name=name,
                requirements=summary.get("requirements") or [],
            )
        except LLMError as exc:
            # One carrier failing should not lose the others.
            failures.append({"carrier_name": name, "error": str(exc)})
            logger.warning("Gap analysis failed for %s: %s", name, exc)
            continue
        except Exception as exc:
            failures.append({"carrier_name": name, "error": str(exc)})
            logger.exception("Gap analysis failed for %s", name)
            continue

        with db.get_connection() as conn:
            saved = db.save_gap_report(
                conn,
                client_id=client_id,
                carrier_id=str(carrier["id"]),
                organization_id=organization_id,
                items=result["items"],
                summary=result["summary"],
                stats=result["stats"],
                model=llm.OLLAMA_MODEL,
            )

        reports.append(
            {
                "report_id": str(saved["id"]),
                "carrier_id": str(carrier["id"]),
                "carrier_name": name,
                "items": result["items"],
                "summary": result["summary"],
                "stats": result["stats"],
                "created_at": saved["created_at"].isoformat(),
            }
        )

    if not reports:
        # Every carrier failed. Report all of them rather than the first: with
        # several carriers in play, one message leaves the user fixing one
        # problem at a time without knowing how many remain.
        if failures:
            detail = " · ".join(f"{f['carrier_name']}: {f['error']}" for f in failures)
        else:
            detail = "Gap analysis produced nothing."
        raise HTTPException(status_code=502, detail=detail)

    return {"client_id": client_id, "reports": reports, "failures": failures}


@router.get("/clients/{client_id}/gaps")
def read_gap_analysis(client_id: str, x_organization_id: str | None = Header(None)):
    """The most recent report per carrier for this client."""
    client_id = _validate_uuid(client_id, "client id")
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        rows = db.get_latest_gap_reports(conn, client_id, organization_id)

    return {
        "client_id": client_id,
        "reports": [
            {
                "report_id": str(r["id"]),
                "carrier_id": str(r["carrier_id"]),
                "carrier_name": r["carrier_name"],
                "items": r["items"],
                "summary": r["summary"],
                "stats": r["stats"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }
