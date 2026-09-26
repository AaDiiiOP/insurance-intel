"""Phase 3 — gap-analysis reports."""
from psycopg.types.json import Jsonb


def save_gap_report(conn, *, client_id: str, carrier_id: str, organization_id: str,
                    items: list[dict], summary: str, stats: dict,
                    model: str | None) -> dict:
    """
    Scoped by organization_id directly (gap_reports carries its own column
    — see the migration note), so a report cannot be filed against a
    client_id/carrier_id pair straddling two organizations even in error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO gap_reports (client_id, carrier_id, organization_id, items, summary, model, stats)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, client_id, carrier_id, items, summary, stats, model, created_at
            """,
            (client_id, carrier_id, organization_id, Jsonb(items), summary, model, Jsonb(stats)),
        )
        return dict(cur.fetchone())


def get_latest_gap_reports(conn, client_id: str, organization_id: str) -> list[dict]:
    """
    The most recent report per carrier for one client, scoped to the
    caller's organization.

    DISTINCT ON keeps one row per carrier — the newest — so re-running for
    one carrier does not hide the others.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (g.carrier_id)
                   g.id, g.client_id, g.carrier_id, c.name AS carrier_name,
                   g.items, g.summary, g.stats, g.model, g.created_at
            FROM gap_reports g
            JOIN carriers c ON c.id = g.carrier_id
            WHERE g.client_id = %s AND g.organization_id = %s
            ORDER BY g.carrier_id, g.created_at DESC
            """,
            (client_id, organization_id),
        )
        return cur.fetchall()
