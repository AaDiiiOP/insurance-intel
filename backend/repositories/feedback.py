"""Phase 5 — corrections, learned house-style guidance, and review insights."""


def save_correction(conn, *, kind: str, organization_id: str, client_id: str | None,
                    carrier_id: str | None, subject: str | None,
                    before_text: str | None, after_text: str | None,
                    meta: dict) -> None:
    """Record one human correction. Append-only, like the audit trail."""
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corrections (kind, organization_id, client_id, carrier_id, subject,
                                     before_text, after_text, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (kind, organization_id, client_id, carrier_id, subject, before_text, after_text,
             Jsonb(meta)),
        )


def recent_corrections(conn, kind: str, organization_id: str, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, client_id, carrier_id, subject, before_text,
                   after_text, meta, created_at
            FROM corrections
            WHERE kind = %s AND organization_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (kind, organization_id, limit),
        )
        return cur.fetchall()


def get_house_style(conn, kind: str, organization_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM house_style WHERE kind = %s AND organization_id = %s",
            (kind, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def save_house_style(conn, *, kind: str, organization_id: str, guidance: str,
                     derived_from: int, edited_by_hand: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO house_style (kind, organization_id, guidance, derived_from, edited_by_hand)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kind, organization_id) DO UPDATE SET
                guidance = EXCLUDED.guidance,
                derived_from = EXCLUDED.derived_from,
                edited_by_hand = EXCLUDED.edited_by_hand,
                updated_at = now()
            RETURNING *
            """,
            (kind, organization_id, guidance, derived_from, edited_by_hand),
        )
        return dict(cur.fetchone())


def insight_counts(conn, organization_id: str) -> dict:
    """
    The numbers behind the Review page, computed from real rows — scoped to
    the caller's organization throughout. Previously unscoped: every count
    here was a cross-tenant total, which is exactly the kind of leak this
    task exists to close — a broker at one organization could read another
    organization's volumes, thin briefings and override patterns off this
    one dashboard even with every other endpoint correctly scoped.
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM carriers WHERE organization_id = %s", (organization_id,))
        out["carriers"] = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT count(*) AS n FROM summaries s
            JOIN carriers c ON c.id = s.carrier_id
            WHERE c.organization_id = %s
            """,
            (organization_id,),
        )
        out["carriers_briefed"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM clients WHERE organization_id = %s", (organization_id,))
        out["clients"] = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT count(*) AS n FROM matches m
            JOIN clients c ON c.id = m.client_id
            WHERE c.organization_id = %s
            """,
            (organization_id,),
        )
        out["matches_run"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM gap_reports WHERE organization_id = %s", (organization_id,))
        out["gap_reports"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM drafts WHERE organization_id = %s", (organization_id,))
        out["drafts"] = cur.fetchone()["n"]
        cur.execute(
            "SELECT count(*) AS n FROM drafts WHERE organization_id = %s AND status = 'approved'",
            (organization_id,),
        )
        out["drafts_approved"] = cur.fetchone()["n"]

        # How often an attached document turned out to be wrong. The point of
        # verification, stated as a number.
        cur.execute(
            """
            SELECT verdict, count(*) AS n
            FROM client_documents WHERE organization_id = %s
            GROUP BY verdict ORDER BY n DESC
            """,
            (organization_id,),
        )
        out["verdicts"] = {r["verdict"]: r["n"] for r in cur.fetchall()}

        # Carriers whose briefing found few or no requirements — the most
        # actionable signal in here, because gap analysis is worthless
        # against a carrier with an empty checklist.
        cur.execute(
            """
            SELECT c.name, jsonb_array_length(s.requirements) AS requirements
            FROM summaries s JOIN carriers c ON c.id = s.carrier_id
            WHERE c.organization_id = %s
            ORDER BY requirements ASC
            LIMIT 10
            """,
            (organization_id,),
        )
        out["thin_briefings"] = [
            {"carrier": r["name"], "requirements": r["requirements"]}
            for r in cur.fetchall()
        ]

        # Requirements a person kept overriding: the tool judged them wrong
        # often enough to be worth looking at.
        cur.execute(
            """
            SELECT subject, count(*) AS n
            FROM corrections
            WHERE kind = 'gap_override' AND subject IS NOT NULL
              AND organization_id = %s
            GROUP BY subject ORDER BY n DESC LIMIT 8
            """,
            (organization_id,),
        )
        out["overridden_requirements"] = [
            {"requirement": r["subject"], "times": r["n"]} for r in cur.fetchall()
        ]

        # How heavily drafts get rewritten before sign-off.
        cur.execute(
            """
            SELECT
                count(*) AS edits,
                avg((meta->>'changed_pct')::float) AS avg_changed
            FROM corrections WHERE kind = 'draft_edit' AND organization_id = %s
            """,
            (organization_id,),
        )
        row = cur.fetchone()
        out["draft_edits"] = row["n"] if "n" in row else row["edits"]
        out["avg_draft_change_pct"] = (
            round(row["avg_changed"], 1) if row["avg_changed"] is not None else None
        )
    return out
