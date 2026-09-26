"""/organizations — create and list the pilot-stage tenant boundary."""
from fastapi import APIRouter, Form, HTTPException

import db

router = APIRouter()


@router.post("/organizations")
def create_organization_endpoint(name: str = Form(...)):
    """
    Create a new organization — a separate tenant boundary for testing or
    onboarding a second broker. Returns its id, which the caller then sends
    as the X-Organization-Id header on every subsequent doc-intelligence
    request to act as that organization.
    """
    name = " ".join((name or "").split())
    if not name:
        raise HTTPException(status_code=400, detail="Give the organization a name.")
    with db.get_connection() as conn:
        row = db.create_organization(conn, name)
    return {
        "organization_id": str(row["id"]),
        "name": row["name"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/organizations")
def list_organizations_endpoint():
    with db.get_connection() as conn:
        rows = db.list_organizations(conn)
    return {
        "organizations": [
            {
                "organization_id": str(r["id"]),
                "name": r["name"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }
