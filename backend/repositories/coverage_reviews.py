"""
Phase 1E — broker review decisions on coverage mappings / findings.

Append-only: a new decision is a new row; the latest row for a subject is its
current decision. Every query filters on organization_id inside the SQL.
"""
from psycopg.types.json import Jsonb

REVIEW_DECISIONS = ("OPEN", "REVIEWED", "ACCEPTED", "REJECTED", "NEEDS_MORE_EVIDENCE")


def add_coverage_review(conn, organization_id: str, business_id: str, subject_type: str, subject_id: str,
                        fingerprint: str, decision: str, reviewer: str, comment: str | None,
                        evidence: list) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO kc_coverage_reviews
                   (organization_id, business_id, subject_type, subject_id, fingerprint, decision,
                    reviewer, comment, evidence)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (organization_id, business_id, subject_type, subject_id, fingerprint, decision, reviewer,
             comment, Jsonb(evidence)),
        )
        return cur.fetchone()


def list_coverage_reviews(conn, organization_id: str, business_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM kc_coverage_reviews
               WHERE organization_id = %s AND business_id = %s
               ORDER BY created_at, id""",
            (organization_id, business_id),
        )
        return cur.fetchall()
