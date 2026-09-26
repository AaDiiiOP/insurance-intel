"""Phase 4 — drafted paperwork and its audit trail."""
from psycopg.types.json import Jsonb


def save_draft(conn, *, client_id: str, organization_id: str, carrier_id: str | None,
               kind: str, title: str, body: str, body_redacted: str,
               warnings: list[dict], stats: dict, model: str | None) -> dict:
    """
    Insert a new draft. Always starts at status 'draft'.

    organization_id is written directly (drafts carries its own column —
    see the migration note), from the caller's already-resolved
    organization; the client itself is checked to belong to it before this
    is ever called.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO drafts (client_id, organization_id, carrier_id, kind, title, body,
                                body_redacted, warnings, stats, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, client_id, carrier_id, kind, title, body, status,
                      warnings, stats, model, created_at, updated_at
            """,
            (client_id, organization_id, carrier_id, kind, title, body, body_redacted,
             Jsonb(warnings), Jsonb(stats), model),
        )
        return dict(cur.fetchone())


def list_drafts(conn, client_id: str, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.client_id, d.carrier_id, c.name AS carrier_name,
                   d.kind, d.title, d.body, d.status, d.warnings, d.stats,
                   d.model, d.created_at, d.updated_at
            FROM drafts d
            LEFT JOIN carriers c ON c.id = d.carrier_id
            WHERE d.client_id = %s AND d.organization_id = %s
            ORDER BY d.created_at DESC
            """,
            (client_id, organization_id),
        )
        return cur.fetchall()


def get_draft(conn, draft_id: str, organization_id: str) -> dict | None:
    """
    Fetch one draft, scoped to the caller's organization — same pattern as
    get_extraction_fact.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.client_id, d.carrier_id, c.name AS carrier_name,
                   d.kind, d.title, d.body, d.status, d.warnings, d.stats,
                   d.model, d.created_at, d.updated_at
            FROM drafts d
            LEFT JOIN carriers c ON c.id = d.carrier_id
            WHERE d.id = %s AND d.organization_id = %s
            """,
            (draft_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_draft_body(conn, draft_id: str, organization_id: str, *, title: str, body: str,
                      warnings: list[dict]) -> dict | None:
    """
    Save a human edit.

    Always moves status to 'edited', including away from 'approved'. That is
    deliberate: a document that changes after sign-off has to be signed off
    again, or the approval refers to text nobody approved.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE drafts
            SET title = %s, body = %s, warnings = %s,
                status = 'edited', updated_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING id, client_id, carrier_id, kind, title, body, status,
                      warnings, stats, model, created_at, updated_at
            """,
            (title, body, Jsonb(warnings), draft_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_draft(conn, draft_id: str, organization_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM drafts WHERE id = %s AND organization_id = %s",
            (draft_id, organization_id),
        )
        return cur.rowcount > 0


def log_draft_event(conn, *, draft_id: str, organization_id: str, event: str,
                    actor: str | None = None, detail: str | None = None) -> bool:
    """
    Append one entry to a draft's audit trail. Never updates an existing row.

    Scoped via a join back to drafts in the INSERT itself: an event cannot
    be recorded against a draft_id belonging to a different organization,
    even if one were guessed or reused. Returns whether the draft was
    actually found and the event recorded.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO draft_events (draft_id, event, actor, detail)
            SELECT %s, %s, %s, %s
            FROM drafts d WHERE d.id = %s AND d.organization_id = %s
            """,
            (draft_id, event, actor, detail, draft_id, organization_id),
        )
        return cur.rowcount > 0


def get_draft_events(conn, draft_id: str, organization_id: str) -> list[dict]:
    """
    A draft's history, oldest first — the order it actually happened in.
    Scoped via a join back to drafts.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.draft_id, e.event, e.actor, e.detail, e.created_at
            FROM draft_events e
            JOIN drafts d ON d.id = e.draft_id
            WHERE e.draft_id = %s AND d.organization_id = %s
            ORDER BY e.created_at ASC
            """,
            (draft_id, organization_id),
        )
        return cur.fetchall()


def set_draft_status(conn, draft_id: str, organization_id: str, status: str) -> dict | None:
    """Move a draft between draft / edited / approved."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE drafts SET status = %s, updated_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING id
            """,
            (status, draft_id, organization_id),
        )
        return cur.fetchone()
