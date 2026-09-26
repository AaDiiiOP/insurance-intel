"""
Phase 5 — the feedback loop: correcting a gap report item by hand, and the
/insights dashboard.
"""
from fastapi import APIRouter, Header, HTTPException
from psycopg.types.json import Jsonb as Jsonb_
from pydantic import BaseModel

import db

from .deps import _resolve_organization, _validate_uuid

router = APIRouter()


class GapOverride(BaseModel):
    """A person correcting one line of a gap report."""

    requirement: str
    status: str
    note: str | None = None


@router.put("/gap-reports/{report_id}/item")
def override_gap_item(report_id: str, body: GapOverride,
                      x_organization_id: str | None = Header(None)):
    """
    Correct one requirement's status by hand.

    The stored report is updated AND the correction is recorded. The record
    is the point: a requirement that keeps being overridden is one the tool
    reads wrong, and that only becomes visible if the corrections are kept.
    """
    report_id = _validate_uuid(report_id, "report id")

    status = (body.status or "").strip().lower()
    if status not in {"held", "missing", "unclear"}:
        raise HTTPException(
            status_code=400, detail="Status must be held, missing or unclear."
        )

    requirement = " ".join((body.requirement or "").split())
    if not requirement:
        raise HTTPException(status_code=400, detail="Which requirement?")

    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, client_id, carrier_id, items FROM gap_reports "
                "WHERE id = %s AND organization_id = %s",
                (report_id, organization_id),
            )
            report = cur.fetchone()
        if not report:
            raise HTTPException(status_code=404, detail="Gap report not found.")

        items = report["items"] or []
        previous = None
        found = False
        for item in items:
            if " ".join(str(item.get("requirement", "")).split()) == requirement:
                previous = item.get("status")
                item["status"] = status
                item["reason"] = (
                    " ".join((body.note or "").split())
                    or f"Set to {status} by hand."
                )
                item["overridden"] = True
                if status == "held":
                    item["ask"] = ""
                found = True
                break

        if not found:
            raise HTTPException(
                status_code=404, detail="That requirement is not in this report."
            )

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE gap_reports SET items = %s WHERE id = %s AND organization_id = %s",
                (Jsonb_(items), report_id, organization_id),
            )

        db.save_correction(
            conn,
            kind="gap_override",
            organization_id=organization_id,
            client_id=str(report["client_id"]),
            carrier_id=str(report["carrier_id"]),
            subject=requirement,
            before_text=previous,
            after_text=status,
            meta={"note": " ".join((body.note or "").split())[:300]},
        )

    return {"report_id": report_id, "requirement": requirement, "status": status}


@router.get("/insights")
def insights(x_organization_id: str | None = Header(None)):
    """Everything on the Review page, counted from real rows."""
    with db.get_connection() as conn:
        organization_id = _resolve_organization(conn, x_organization_id)
        return db.insight_counts(conn, organization_id)
