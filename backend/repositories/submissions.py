"""Submissions and cross-document contradiction detection."""


def create_submission(conn, *, organization_id: str, label: str) -> dict:
    """A new group of documents believed to belong to the same case/company."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO submissions (organization_id, label) VALUES (%s, %s) "
            "RETURNING id, organization_id, label, created_at",
            (organization_id, label),
        )
        return dict(cur.fetchone())


def get_submission(conn, submission_id: str, organization_id: str) -> dict | None:
    """
    Fetch a submission, scoped to the caller's organization.

    Deliberately requires organization_id and filters on it, rather than
    fetching by id alone and letting the caller check afterward: a
    submission that belongs to a different organization must not exist as
    far as this query is concerned, not exist-but-get-rejected one layer up
    where a future call site could forget the check. Returns None for a
    submission that exists but belongs to someone else, same as one that
    does not exist at all — a 404 either way, so a caller ownership probe
    cannot even distinguish "not found" from "not yours".
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM submissions WHERE id = %s AND organization_id = %s",
            (submission_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_submissions(conn, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM submissions WHERE organization_id = %s ORDER BY created_at DESC",
            (organization_id,),
        )
        return cur.fetchall()


def set_document_submission(conn, document_id: str, submission_id: str) -> None:
    """
    Attach an already-created document to a submission.

    Kept separate from create_document rather than added as a new parameter
    to it: create_document is also called by the carrier-briefing flow,
    which has no concept of a submission at all, and every call site would
    otherwise need updating for a feature it doesn't use.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET submission_id = %s WHERE id = %s",
            (submission_id, document_id),
        )


def list_submission_documents(conn, submission_id: str, organization_id: str) -> list[dict]:
    """
    Documents in a submission, scoped to the caller's organization via a
    join back to submissions — belt-and-braces alongside the caller
    checking get_submission() first, so this function is safe to call even
    if a future call site forgets that check.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.* FROM documents d
            JOIN submissions s ON s.id = d.submission_id
            WHERE d.submission_id = %s AND s.organization_id = %s
            ORDER BY d.uploaded_at ASC
            """,
            (submission_id, organization_id),
        )
        return cur.fetchall()


def list_submission_facts(conn, submission_id: str, organization_id: str) -> list[dict]:
    """
    Every extraction_facts row for every document in one submission, scoped
    to the caller's organization.

    This is what a contradiction check compares — every fact currently on
    record for the submission, joined back to which document it came from
    so the comparison can name both sources when it flags a mismatch.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.*, d.filename AS document_filename
            FROM extraction_facts f
            JOIN documents d ON d.id = f.document_id
            JOIN submissions s ON s.id = d.submission_id
            WHERE d.submission_id = %s AND s.organization_id = %s
            ORDER BY f.created_at ASC
            """,
            (submission_id, organization_id),
        )
        return cur.fetchall()
