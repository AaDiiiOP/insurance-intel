"""
Clients, client tokens, and the documents a client actually sent
(Phase 2 client intake + Phase 3.5 attached-document verification).
"""
from psycopg.types.json import Jsonb

from .connection import DEFAULT_ORGANIZATION_ID

# Columns a client write accepts. Listed once so the insert, the update and
# the API layer cannot drift apart.
CLIENT_FIELDS = (
    "business_name", "entity_type", "industry", "city", "state",
    "turnover", "years_trading", "employee_count",
    "pan", "gstin", "contact_name", "contact_phone", "contact_email",
    "sum_insured", "risk_notes",
)


def create_client(conn, *, fields: dict, categories: list[str],
                  redacted_profile: str,
                  organization_id: str = DEFAULT_ORGANIZATION_ID) -> dict:
    """
    Insert a client and return the full row.

    Same pilot-stage default-organization fallback as create_carrier:
    defaults when no caller passes a real resolved organization id yet.
    """
    columns = list(CLIENT_FIELDS)
    values = [fields.get(c) for c in columns]

    placeholders = ", ".join(["%s"] * (len(columns) + 3))
    column_sql = ", ".join(columns + ["categories", "redacted_profile", "organization_id"])

    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO clients ({column_sql}) VALUES ({placeholders}) "
            f"RETURNING *",
            (*values, Jsonb(categories), redacted_profile, organization_id),
        )
        return dict(cur.fetchone())


def update_client(conn, client_id: str, organization_id: str, *, fields: dict,
                  categories: list[str], redacted_profile: str) -> dict | None:
    """
    Replace a client's details. Returns None if the client is gone, or
    belongs to a different organization.
    """
    columns = list(CLIENT_FIELDS)
    assignments = ", ".join(f"{c} = %s" for c in columns)
    values = [fields.get(c) for c in columns]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE clients
            SET {assignments},
                categories = %s,
                redacted_profile = %s,
                updated_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING *
            """,
            (*values, Jsonb(categories), redacted_profile, client_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_clients(conn, organization_id: str) -> list[dict]:
    """
    Every client for this organization, newest first, with whether a match
    has been run.

    Excludes risk_notes and redacted_profile — a list view does not need
    them, and they are the longest columns on the table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.business_name, c.industry, c.city, c.state,
                   c.categories, c.turnover, c.created_at,
                   (SELECT max(created_at) FROM matches m WHERE m.client_id = c.id)
                       AS last_matched_at
            FROM clients c
            WHERE c.organization_id = %s
            ORDER BY c.created_at DESC
            """,
            (organization_id,),
        )
        return cur.fetchall()


def get_client(conn, client_id: str, organization_id: str) -> dict | None:
    """
    Fetch one client, scoped to the caller's organization — same pattern as
    get_extraction_fact: a client belonging to a different organization is
    indistinguishable from one that does not exist.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM clients WHERE id = %s AND organization_id = %s",
            (client_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_client(conn, client_id: str, organization_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM clients WHERE id = %s AND organization_id = %s",
            (client_id, organization_id),
        )
        return cur.rowcount > 0


def save_client_tokens(conn, client_id: str, organization_id: str, mapping: dict) -> None:
    """
    Upsert the placeholder map for one client.

    Scoped via a join back to clients: an UPDATE ... FROM matching the
    client and its organization at once, so this cannot write against a
    client_id belonging to a different organization even if one were
    guessed or reused.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO client_tokens (client_id, mapping)
            SELECT %s, %s
            FROM clients c WHERE c.id = %s AND c.organization_id = %s
            ON CONFLICT (client_id) DO UPDATE
                SET mapping = EXCLUDED.mapping, updated_at = now()
            """,
            (client_id, Jsonb(mapping), client_id, organization_id),
        )


def get_client_tokens(conn, client_id: str, organization_id: str) -> dict:
    """The placeholder map, or an empty dict if none stored yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ct.mapping FROM client_tokens ct
            JOIN clients c ON c.id = ct.client_id
            WHERE ct.client_id = %s AND c.organization_id = %s
            """,
            (client_id, organization_id),
        )
        row = cur.fetchone()
        return row["mapping"] if row else {}


# --------------------------------------------------------------------------
# Phase 3 — documents held
# --------------------------------------------------------------------------


def set_client_documents(conn, client_id: str, organization_id: str, *, held: list[str],
                         missing: list[str]) -> bool:
    """Replace both document lists for a client. Returns whether a row was updated."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE clients
            SET documents_held = %s, documents_missing = %s, updated_at = now()
            WHERE id = %s AND organization_id = %s
            """,
            (Jsonb(held), Jsonb(missing), client_id, organization_id),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Phase 3.5 — documents the client actually sent
# --------------------------------------------------------------------------


def save_client_document(conn, *, client_id: str, organization_id: str, requirement: str,
                         filename: str, file_type: str, size_bytes: int, chars: int,
                         extracted_text: str | None, verification: dict) -> dict:
    """
    Store one uploaded document and its verification result.

    organization_id is written directly (client_documents carries its own
    column — see the migration note), from the caller's already-resolved
    organization, since the client itself was checked to belong to it
    before this is called.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO client_documents (
                client_id, organization_id, requirement, filename, file_type, size_bytes, chars,
                extracted_text, verdict, doc_type, reason, identifiers, notes,
                key_facts, score, score_parts, improvements
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, client_id, requirement, filename, file_type, size_bytes,
                      chars, verdict, doc_type, reason, identifiers, notes,
                      key_facts, score, score_parts, improvements, uploaded_at
            """,
            (
                client_id, organization_id, requirement, filename, file_type, size_bytes, chars,
                extracted_text,
                verification.get("verdict", "unchecked"),
                verification.get("doc_type"),
                verification.get("reason"),
                Jsonb(verification.get("identifiers") or {}),
                Jsonb(verification.get("notes") or []),
                Jsonb(verification.get("key_facts") or []),
                verification.get("score"),
                Jsonb(verification.get("score_parts") or {}),
                Jsonb(verification.get("improvements") or []),
            ),
        )
        return dict(cur.fetchone())


def list_client_documents(conn, client_id: str, organization_id: str) -> list[dict]:
    """
    Every document attached for a client, newest first per requirement.

    Excludes extracted_text — a list view never needs it and it is the
    largest column on the table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, client_id, requirement, filename, file_type, size_bytes,
                   chars, verdict, doc_type, reason, identifiers, notes,
                   key_facts, score, score_parts, improvements, uploaded_at
            FROM client_documents
            WHERE client_id = %s AND organization_id = %s
            ORDER BY uploaded_at DESC
            """,
            (client_id, organization_id),
        )
        return cur.fetchall()


def delete_client_document(conn, document_id: str, organization_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM client_documents WHERE id = %s AND organization_id = %s",
            (document_id, organization_id),
        )
        return cur.rowcount > 0
