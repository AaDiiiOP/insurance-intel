"""
v0.14.1 regression: an older database must be upgraded by the startup
migrations, not only a fresh one.

Found in the Phase 2A Docker check: the developer's existing database had a
`client_documents` table created before `score`, `score_parts`,
`improvements` (and other columns) were added to SCHEMA. CREATE TABLE IF NOT
EXISTS skipped it, no ALTER existed, and every paperwork upload failed with
'column "score" of relation "client_documents" does not exist'.

Every test here runs inside one transaction that is rolled back, so the shared
test database is left exactly as it was.
"""
import uuid

import pytest

from repositories.connection import (
    MIGRATIONS,
    find_schema_drift,
    get_connection,
)

# Columns added to client_documents after the table first shipped.
LATE_COLUMNS = [
    "file_type", "size_bytes", "chars", "extracted_text", "verdict", "doc_type",
    "reason", "score", "score_parts", "improvements", "identifiers", "notes",
    "key_facts", "uploaded_at",
]


@pytest.fixture
def tx():
    """One connection, one transaction, always rolled back."""
    with get_connection() as c:
        try:
            yield c
        finally:
            c.rollback()


def _columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {r["column_name"] for r in cur.fetchall()}


def test_current_database_has_no_schema_drift(tx):
    drift = find_schema_drift(tx)
    assert drift["missing_tables"] == []
    assert drift["missing_columns"] == []


def test_old_client_documents_table_is_upgraded_and_keeps_its_rows(tx):
    org_id, client_id, doc_id = (str(uuid.uuid4()) for _ in range(3))
    with tx.cursor() as cur:
        # An existing row, written before the table is made "old".
        cur.execute("INSERT INTO organizations (id, name) VALUES (%s, %s)",
                    (org_id, f"schema-upgrade-{org_id[:8]}"))
        cur.execute("INSERT INTO clients (id, business_name, organization_id) VALUES (%s, %s, %s)",
                    (client_id, "Synthetic Upgrade Client", org_id))
        cur.execute(
            "INSERT INTO client_documents (id, client_id, organization_id, requirement, filename) "
            "VALUES (%s, %s, %s, %s, %s)",
            (doc_id, client_id, org_id, "Fire policy schedule", "old.pdf"),
        )
        # Make the table look like the developer's older one.
        cur.execute("ALTER TABLE client_documents "
                    + ", ".join(f"DROP COLUMN {c}" for c in LATE_COLUMNS))
        assert not set(LATE_COLUMNS) & _columns(cur, "client_documents")

        drift = find_schema_drift(tx)
        assert "client_documents.score" in drift["missing_columns"]

        # The same statements the backend runs on every startup.
        cur.execute(MIGRATIONS)

        assert set(LATE_COLUMNS) <= _columns(cur, "client_documents")
        assert find_schema_drift(tx)["missing_columns"] == []

        # The old row survived and got the SCHEMA defaults.
        cur.execute("SELECT filename, verdict, score, score_parts, improvements, key_facts "
                    "FROM client_documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
        assert row["filename"] == "old.pdf"
        assert row["verdict"] == "unchecked"
        assert row["score"] is None
        assert row["score_parts"] == {} and row["improvements"] == [] and row["key_facts"] == []

        # The insert that failed in Docker now works.
        cur.execute(
            """
            INSERT INTO client_documents
                (client_id, organization_id, requirement, filename, key_facts,
                 score, score_parts, improvements)
            VALUES (%s, %s, 'Fire policy schedule', 'new.pdf', '[]', 80, '{}', '[]')
            RETURNING score
            """,
            (client_id, org_id),
        )
        assert cur.fetchone()["score"] == 80

        # Running the migrations again changes nothing (idempotent).
        cur.execute(MIGRATIONS)
        assert find_schema_drift(tx)["missing_columns"] == []


def test_drift_check_is_read_only(tx):
    find_schema_drift(tx)
    with tx.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'aurevia_schema_expected'")
        assert cur.fetchone() is None
        cur.execute("SHOW search_path")
        assert "aurevia_schema_expected" not in cur.fetchone()["search_path"]
