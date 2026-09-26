"""Phase 2 — carrier matching runs."""
from psycopg.types.json import Jsonb


def save_match(conn, *, client_id: str, organization_id: str, results: list[dict],
               sources: list[dict], model: str | None, stats: dict) -> dict | None:
    """
    Append a match run. History, not current state — see the schema note.

    Only inserts when client_id actually belongs to organization_id (checked
    first, same belt-and-braces pattern as save_summary) — returns None
    otherwise, since main.py's get_client() check should already have
    caught this.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM clients WHERE id = %s AND organization_id = %s",
            (client_id, organization_id),
        )
        if not cur.fetchone():
            return None

        cur.execute(
            """
            INSERT INTO matches (client_id, results, sources, model, stats)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, client_id, results, sources, model, stats, created_at
            """,
            (client_id, Jsonb(results), Jsonb(sources), model, Jsonb(stats)),
        )
        return dict(cur.fetchone())


def get_latest_match(conn, client_id: str, organization_id: str) -> dict | None:
    """Scoped to the caller's organization via a join back to clients."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.client_id, m.results, m.sources, m.model, m.stats, m.created_at
            FROM matches m
            JOIN clients c ON c.id = m.client_id
            WHERE m.client_id = %s AND c.organization_id = %s
            ORDER BY m.created_at DESC
            LIMIT 1
            """,
            (client_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None
