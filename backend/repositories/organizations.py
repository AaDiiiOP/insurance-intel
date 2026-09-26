"""
Organizations — the pilot-stage tenant boundary.

See repositories/connection.py's module docstring for the overall
organization-scoping approach, and connection.DEFAULT_ORGANIZATION_ID for
the pilot-stage default org id.
"""
from .connection import DEFAULT_ORGANIZATION_ID


def get_or_create_default_organization(conn) -> str:
    """
    The pilot-stage default org.

    A real onboarding flow would let a broker create their own organization;
    this vertical slice does not build that yet — it only needs to prove the
    column and the scoping work, which a single default organization already
    does. Genuinely creating a second organization (and confirming a second
    broker's data stays apart from the first's) is the actual test of this
    boundary, not a UI to manage many of them.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (DEFAULT_ORGANIZATION_ID, "Default organization"),
        )
    return DEFAULT_ORGANIZATION_ID


def create_organization(conn, name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (name) VALUES (%s) RETURNING id, name, created_at",
            (name,),
        )
        return dict(cur.fetchone())


def list_organizations(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, created_at FROM organizations ORDER BY created_at")
        return cur.fetchall()


def get_organization(conn, organization_id: str) -> dict | None:
    """Used to confirm an X-Organization-Id header actually names a real
    organization, before trusting it to scope every other query."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, created_at FROM organizations WHERE id = %s",
            (organization_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
