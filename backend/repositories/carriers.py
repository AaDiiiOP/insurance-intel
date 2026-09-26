"""
Carriers, their documents, summaries and chunks — the carrier-briefing
feature's data access.
"""
from psycopg.types.json import Jsonb

from .connection import DEFAULT_ORGANIZATION_ID


def create_carrier(conn, name: str, organization_id: str = DEFAULT_ORGANIZATION_ID) -> str:
    """
    Insert a carrier and return its generated id.

    Defaults to the pilot-stage default organization when no caller passes
    one, so a call site that has not yet been threaded through with the
    real resolved organization id keeps working exactly as before.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO carriers (name, organization_id) VALUES (%s, %s) RETURNING id",
            (name, organization_id),
        )
        return str(cur.fetchone()["id"])


def create_document(
    conn,
    *,
    organization_id: str,
    carrier_id: str | None,
    filename: str,
    file_type: str,
    size_bytes: int,
    pages: int,
    chars: int,
    status: str,
    error_message: str | None,
    extracted_text: str | None,
) -> dict:
    """
    Insert one document row and return the fields the API responds with.

    Keyword-only (the bare * above) because this has several parameters of
    similar type — positional calls would be easy to get subtly wrong.
    carrier_id is optional: a Document Intelligence upload is not filed
    against any carrier-briefing carrier at all.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (
                organization_id, carrier_id, filename, file_type, size_bytes,
                pages, chars, status, error_message, extracted_text
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, filename, status, pages, chars, error_message
            """,
            (
                organization_id,
                carrier_id,
                filename,
                file_type,
                size_bytes,
                pages,
                chars,
                status,
                error_message,
                extracted_text,
            ),
        )
        return dict(cur.fetchone())


def list_carriers(conn, organization_id: str) -> list[dict]:
    """
    Every carrier for this organization, newest first, with a document count
    and whether it has a summary yet — enough for a dashboard list without a
    second query per row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.id, c.name, c.created_at,
                COUNT(d.id) AS document_count,
                COUNT(d.id) FILTER (WHERE d.status = 'failed') AS failed_count,
                (s.id IS NOT NULL) AS has_summary
            FROM carriers c
            LEFT JOIN documents d ON d.carrier_id = c.id
            LEFT JOIN summaries s ON s.carrier_id = c.id
            WHERE c.organization_id = %s
            GROUP BY c.id, c.name, c.created_at, s.id
            ORDER BY c.created_at DESC
            """,
            (organization_id,),
        )
        return cur.fetchall()


def get_carrier(conn, carrier_id: str, organization_id: str) -> dict | None:
    """
    Fetch one carrier, scoped to the caller's organization — same pattern as
    get_extraction_fact: filtered in the query itself, so a carrier
    belonging to a different organization is indistinguishable from one
    that does not exist.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, created_at FROM carriers WHERE id = %s AND organization_id = %s",
            (carrier_id, organization_id),
        )
        return cur.fetchone()


def list_documents_for_carrier(conn, carrier_id: str, organization_id: str) -> list[dict]:
    """
    Lighter query for a list view: excludes extracted_text, which can be
    large and isn't needed until something specifically reads a document's
    full text. Scoped to the caller's organization directly (documents now
    carries its own organization_id — see the migration note), not via a
    join back to carriers, since carrier_id can be NULL for a
    doc-intelligence upload.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, carrier_id, filename, file_type, size_bytes, pages,
                   chars, status, error_message, uploaded_at
            FROM documents
            WHERE carrier_id = %s AND organization_id = %s
            ORDER BY uploaded_at ASC
            """,
            (carrier_id, organization_id),
        )
        return cur.fetchall()


def get_document_with_text(conn, document_id: str, organization_id: str) -> dict | None:
    """
    Full row, including extracted_text — for the one place that needs it.
    Scoped to the caller's organization: a document belonging to a
    different organization is treated as not found.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, carrier_id, filename, file_type, size_bytes, pages,
                   chars, status, error_message, extracted_text, uploaded_at
            FROM documents
            WHERE id = %s AND organization_id = %s
            """,
            (document_id, organization_id),
        )
        return cur.fetchone()


def delete_carrier(conn, carrier_id: str, organization_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM carriers WHERE id = %s AND organization_id = %s",
            (carrier_id, organization_id),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------


def save_summary(
    conn,
    *,
    carrier_id: str,
    organization_id: str,
    summary: str,
    company_overview: dict,
    requirements: list[dict],
    plans: list[dict],
    exclusions: list[dict],
    key_points: list[dict],
    stats: dict,
    model: str | None,
) -> dict | None:
    """
    Upsert the one summary row for a carrier.

    A carrier can be re-summarised (new documents added, or a re-run after
    a prompt change) — this replaces the previous summary rather than
    versioning it, same "current state, not history" choice the reference
    project makes for its own summaries table.

    Scoped via a join back to carriers: the UPDATE branch of the upsert
    only fires for a carrier actually owned by this organization, and the
    INSERT branch only runs at all when that carrier exists — belt-and-
    braces alongside the caller checking get_carrier() first. Returns None
    if the carrier does not belong to this organization.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM carriers WHERE id = %s AND organization_id = %s",
            (carrier_id, organization_id),
        )
        if not cur.fetchone():
            return None

        cur.execute(
            """
            INSERT INTO summaries (
                carrier_id, summary, summary_original, company_overview,
                requirements, plans, exclusions, key_points, stats, model
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (carrier_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                summary_original = EXCLUDED.summary_original,
                company_overview = EXCLUDED.company_overview,
                requirements = EXCLUDED.requirements,
                plans = EXCLUDED.plans,
                exclusions = EXCLUDED.exclusions,
                key_points = EXCLUDED.key_points,
                stats = EXCLUDED.stats,
                model = EXCLUDED.model,
                generated_at = now(),
                edited_at = NULL
            RETURNING id, carrier_id, summary, company_overview, requirements,
                      plans, exclusions, key_points, stats, model, generated_at,
                      edited_at
            """,
            (
                carrier_id,
                summary,
                summary,
                Jsonb(company_overview),
                Jsonb(requirements),
                Jsonb(plans),
                Jsonb(exclusions),
                Jsonb(key_points),
                Jsonb(stats),
                model,
            ),
        )
        return dict(cur.fetchone())


def get_summary(conn, carrier_id: str, organization_id: str) -> dict | None:
    """Scoped to the caller's organization via a join back to carriers."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.carrier_id, s.summary, s.company_overview, s.requirements,
                   s.plans, s.exclusions, s.key_points, s.stats, s.model, s.generated_at,
                   s.edited_at
            FROM summaries s
            JOIN carriers c ON c.id = s.carrier_id
            WHERE s.carrier_id = %s AND c.organization_id = %s
            """,
            (carrier_id, organization_id),
        )
        return cur.fetchone()


def update_summary_text(conn, carrier_id: str, organization_id: str, summary: str) -> dict | None:
    """User-edited summary text. Leaves the structured fields untouched."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries
            SET summary = %s, edited_at = now()
            FROM carriers c
            WHERE summaries.carrier_id = %s
              AND c.id = summaries.carrier_id
              AND c.organization_id = %s
            RETURNING summaries.id, summaries.carrier_id, summaries.summary,
                      summaries.company_overview, summaries.requirements,
                      summaries.plans, summaries.exclusions, summaries.key_points,
                      summaries.stats, summaries.model, summaries.generated_at,
                      summaries.edited_at
            """,
            (summary, carrier_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------


def save_chunks(conn, document_id: str, chunks: list[dict]) -> None:
    """
    Replace all stored chunks for a document.

    Delete-then-insert rather than upsert: a re-summarise can produce a
    different chunk count than before (different boundaries after an edit
    to the parser or a document re-upload), and stale extra rows from a
    previous run would otherwise survive alongside the new ones.

    Not itself organization-scoped: document_id is only ever passed in here
    by callers (create_summary in main.py) that already resolved and
    validated the document/carrier under the caller's organization, and a
    chunk has no independent identity a cross-organization caller could
    target directly.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        for chunk in chunks:
            cur.execute(
                """
                INSERT INTO chunks (document_id, chunk_index, section_title,
                                     page_start, text, summary)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    chunk["index"],
                    chunk.get("section_title"),
                    chunk.get("page_start"),
                    chunk["text"],
                    chunk.get("summary"),
                ),
            )


def get_chunks_for_document_unscoped(conn, document_id: str) -> list[dict]:
    """
    Every chunk for one document, WITHOUT an organization filter.

    Deliberately unscoped, and named to make that impossible to miss: the
    only caller is /reindex, a maintenance operation that rebuilds Chroma
    from Postgres for every carrier across every organization at once by
    design (see the /reindex docstring in main.py) — it already resolves
    document ids from all_indexable_documents(), which is itself
    cross-organization for the same reason. Never call this from a
    request path that serves one caller's own data.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, document_id, chunk_index, section_title, page_start,
                   text, summary, created_at
            FROM chunks
            WHERE document_id = %s
            ORDER BY chunk_index ASC
            """,
            (document_id,),
        )
        return cur.fetchall()


def get_chunks_for_document(conn, document_id: str, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ch.id, ch.document_id, ch.chunk_index, ch.section_title,
                   ch.page_start, ch.text, ch.summary, ch.created_at
            FROM chunks ch
            JOIN documents d ON d.id = ch.document_id
            WHERE ch.document_id = %s AND d.organization_id = %s
            ORDER BY ch.chunk_index ASC
            """,
            (document_id, organization_id),
        )
        return cur.fetchall()


def get_chunks_for_carrier(conn, carrier_id: str, organization_id: str) -> list[dict]:
    """Every chunk across every document belonging to one carrier."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ch.id, ch.document_id, ch.chunk_index, ch.section_title,
                   ch.page_start, ch.text, ch.summary, d.filename
            FROM chunks ch
            JOIN documents d ON d.id = ch.document_id
            WHERE d.carrier_id = %s AND d.organization_id = %s
            ORDER BY d.uploaded_at ASC, ch.chunk_index ASC
            """,
            (carrier_id, organization_id),
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# Reindex support
# --------------------------------------------------------------------------


def all_indexable_documents(conn) -> list[dict]:
    """
    Every parsed document that has stored chunks, with its carrier.

    Used to rebuild the vector index from Postgres. This is what makes the
    Chroma volume disposable: nothing here needs a model call to regenerate,
    only re-embedding.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT d.id AS document_id, d.filename,
                   c.id AS carrier_id, c.name AS carrier_name
            FROM documents d
            JOIN carriers c ON c.id = d.carrier_id
            JOIN chunks ch ON ch.document_id = d.id
            WHERE d.status = 'parsed'
            ORDER BY c.name
            """
        )
        return cur.fetchall()
