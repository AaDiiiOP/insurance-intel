"""
Document Intelligence vertical slice — extraction facts, their append-only
review events, and per-document OCR/quality outcome.
"""
from psycopg.types.json import Jsonb


def save_extraction_fact(conn, *, organization_id: str, document_id: str,
                         fact_type: str, label: str, value: str,
                         evidence: dict, confidence: dict,
                         status: str, produced_by: str) -> dict:
    """
    Insert one extracted fact with its evidence and typed confidence.

    `evidence` is {page, section, snippet, source}. `confidence` is
    {value, type, method, note} — see the schema comment on
    extraction_facts for what each confidence_type actually claims.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_facts (
                organization_id, document_id, fact_type, label, value,
                evidence_page, evidence_section, evidence_snippet, evidence_source,
                confidence_value, confidence_type, confidence_method, confidence_note,
                status, original_value, produced_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                organization_id, document_id, fact_type, label, value,
                evidence.get("page"), evidence.get("section"),
                evidence.get("snippet"), evidence.get("source", "text_layer"),
                confidence["value"], confidence["type"], confidence["method"],
                confidence.get("note"),
                status, value, produced_by,
            ),
        )
        return dict(cur.fetchone())


def list_extraction_facts(conn, document_id: str, organization_id: str) -> list[dict]:
    """
    Every fact for one document, scoped to the caller's organization.

    Previously unscoped — this is the gap behind
    GET /doc-intelligence/documents/{document_id}/facts not applying the
    same ownership check that get_extraction_fact already does: any caller
    could list another organization's facts by document_id even though
    fetching one of those same facts by fact_id was correctly rejected.
    Filtered here in the query itself, same pattern as everywhere else in
    this vertical slice.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM extraction_facts WHERE document_id = %s AND organization_id = %s "
            "ORDER BY created_at",
            (document_id, organization_id),
        )
        return cur.fetchall()


def get_extraction_fact(conn, fact_id: str, organization_id: str) -> dict | None:
    """
    Fetch one fact, scoped to the caller's organization.

    Same reasoning as get_submission: filtered in the query itself, so a
    fact belonging to a different organization is indistinguishable from
    one that does not exist, rather than fetched and rejected afterward.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM extraction_facts WHERE id = %s AND organization_id = %s",
            (fact_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def review_extraction_fact(conn, fact_id: str, *, organization_id: str, event: str,
                           reviewer: str, new_value: str | None,
                           reason: str | None) -> dict | None:
    """
    Record a human review action and update the fact's current state.

    Requires organization_id and checks it before writing anything — a
    review action changes data, so the ownership check here matters even
    more than on a read: without it, a caller who obtained or guessed
    another organization's fact_id could not just see the fact but alter
    it. The extraction_events row is the permanent record of what
    happened; the extraction_facts row is updated to reflect current
    status so the UI can show "corrected" without replaying the event log
    every time.
    """
    fact = get_extraction_fact(conn, fact_id, organization_id)
    if not fact:
        return None

    before_value = fact["value"]
    after_value = new_value if event == "corrected" else before_value
    new_status = {"verified": "verified", "corrected": "corrected", "flagged": "needs_review"}[event]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_events (fact_id, event, reviewer, before_value, after_value, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (fact_id, event, reviewer, before_value, after_value, reason),
        )
        cur.execute(
            """
            UPDATE extraction_facts
            SET value = %s, status = %s, updated_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING *
            """,
            (after_value, new_status, fact_id, organization_id),
        )
        return dict(cur.fetchone())


def get_extraction_events(conn, fact_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM extraction_events WHERE fact_id = %s ORDER BY created_at ASC",
            (fact_id,),
        )
        return cur.fetchall()


def save_document_quality(conn, *, document_id: str, used_ocr: bool,
                          pages_total: int, pages_ocr_low_confidence: int,
                          avg_ocr_confidence: float | None, notes: list[str],
                          page_confidences: dict | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_quality (
                document_id, used_ocr, pages_total, pages_ocr_low_confidence,
                avg_ocr_confidence, page_confidences, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_id) DO UPDATE SET
                used_ocr = EXCLUDED.used_ocr,
                pages_total = EXCLUDED.pages_total,
                pages_ocr_low_confidence = EXCLUDED.pages_ocr_low_confidence,
                avg_ocr_confidence = EXCLUDED.avg_ocr_confidence,
                page_confidences = EXCLUDED.page_confidences,
                notes = EXCLUDED.notes
            """,
            (document_id, used_ocr, pages_total, pages_ocr_low_confidence,
             avg_ocr_confidence,
             Jsonb(page_confidences) if page_confidences is not None else None,
             Jsonb(notes)),
        )


def get_document_quality(conn, document_id: str, organization_id: str) -> dict | None:
    """Scoped to the caller's organization via a join back to documents."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.* FROM document_quality q
            JOIN documents d ON d.id = q.document_id
            WHERE q.document_id = %s AND d.organization_id = %s
            """,
            (document_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def flag_contradiction(conn, *, fact_id: str, contradicts_fact_id: str,
                       note: str) -> dict | None:
    """
    Mark a fact as disagreeing with another fact in the same submission.

    Both facts are flagged, symmetrically — a reviewer opening either one
    should see that it is in dispute, not just the one that happened to be
    extracted second. Status moves to 'contradiction_flagged' regardless of
    whatever it was before, since a disagreement between two documents is a
    more specific and more urgent reason for review than the generic
    'needs_review' state it may have already been in.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extraction_facts
            SET status = 'contradiction_flagged',
                contradicts_fact_id = %s,
                contradiction_note = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (contradicts_fact_id, note, fact_id),
        )
        return dict(cur.fetchone()) if cur.rowcount else None
