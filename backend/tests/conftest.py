"""
Shared pytest fixtures for the Knowledge Core regression suite.

Every test in this package runs against a REAL local Postgres database (the
same one main.py connects to — DATABASE_URL, defaulting to the project's
standard local dev connection string) and calls REAL application code
(knowledge/service.py, repositories/knowledge.py, property_policy_
intelligence.py) — never a mock of the code under test. Isolation between
test runs comes from creating one or more dedicated `organizations` rows per
test (via `test_org`/`two_test_orgs` below) rather than a separate schema:
every kc_* table (and every other tenant-scoped table in this project) is
already organization_id-scoped by design, so a fresh throwaway organization
gives each test its own blast radius without needing a second database or
schema. Rows created under a test organization are left in place after the
run (deleting the organization row would cascade-delete them, but nothing
here does that automatically) — safe because they are inert, harmless rows
scoped to an organization id nothing else references, exactly like any other
manually-created test organization already sitting in this project's local
dev database from earlier work.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://insurance_user:insurance_pass@localhost:5432/insurance_db",
)

import db  # noqa: E402
import knowledge.service as kc  # noqa: E402
from repositories.connection import init_schema  # noqa: E402

from tests.stub_ollama import StubOllamaServer  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """
    Runs init_schema() once per test session — the same migration path
    `main.py` runs on startup — so the suite can also be run against a
    database that has never seen the app boot at all, and so it always
    exercises the CURRENT schema (including this pass's `superseded` status
    and its CHECK-constraint migration) rather than assuming a stale one.
    Safe to run against a database that already has real Phase 0/Phase 1
    data in it: every statement is idempotent (CREATE TABLE IF NOT EXISTS /
    ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS ... ADD CONSTRAINT),
    the same guarantee the project's hard constraint requires of it.
    """
    init_schema()


@pytest.fixture
def conn():
    """One real Postgres connection per test, committing on success (same
    context manager main.py's endpoints use)."""
    with db.get_connection() as connection:
        yield connection


@pytest.fixture
def test_org(conn):
    """One throwaway organization for a test that only needs a single
    tenant. Named uniquely per test run so parallel/repeated runs never
    collide."""
    name = f"pytest-org-{uuid.uuid4().hex[:12]}"
    org = db.create_organization(conn, name)
    return str(org["id"])


@pytest.fixture
def two_test_orgs(conn):
    """Two throwaway organizations, for every cross-tenant isolation test —
    org A's data must never be reachable/writable using org B's id."""
    org_a = db.create_organization(conn, f"pytest-org-a-{uuid.uuid4().hex[:12]}")
    org_b = db.create_organization(conn, f"pytest-org-b-{uuid.uuid4().hex[:12]}")
    return str(org_a["id"]), str(org_b["id"])


@pytest.fixture
def make_document(conn):
    """Factory fixture: create a real `documents` row scoped to a given
    organization, with real extracted_text, returning its id. Used wherever
    a test needs kc_evidence to point at a genuine document row rather than
    a bare UUID that happens to satisfy a foreign key."""

    def _make(organization_id: str, *, filename: str = "test.pdf",
               extracted_text: str = "") -> str:
        row = db.create_document(
            conn,
            organization_id=organization_id,
            carrier_id=None,
            filename=filename,
            file_type="pdf",
            size_bytes=len(extracted_text.encode("utf-8")) or 1,
            pages=1,
            chars=len(extracted_text),
            status="parsed",
            error_message=None,
            extracted_text=extracted_text,
        )
        return str(row["id"])

    return _make


@pytest.fixture
def make_entity(conn):
    """Factory fixture: create a kc_entities row via the real service layer."""

    def _make(organization_id: str, *, entity_type: str = "asset",
              name: str | None = None) -> str:
        name = name or f"pytest entity {uuid.uuid4().hex[:8]}"
        row = kc.create_entity(
            conn, organization_id=organization_id, entity_type=entity_type, name=name,
        )
        return str(row["id"])

    return _make


@pytest.fixture(scope="module")
def stub_ollama():
    """
    A real Ollama-compatible HTTP server (see tests/stub_ollama.py), started
    in a background thread for the duration of the tests that need it.

    Used ONLY because a real Ollama instance is not reachable in this
    sandbox — llm.py's OLLAMA_HOST is pointed at 127.0.0.1:11434 (the stub's
    address) via monkeypatch inside the tests that use this fixture, and the
    real extraction code path (property_policy_intelligence.extract_
    property_policy_facts -> llm.generate_json -> real HTTP POST) runs
    completely unmocked against it — this is a stand-in for the MODEL, not a
    stand-in for the code being tested.
    """
    server = StubOllamaServer(port=11434)
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _generous_ip_login_limit(monkeypatch):
    """Phase 2A: every TestClient signs in from the same address, so the
    per-IP failed-login limit is raised for the suite; the rate-limit test
    lowers it again explicitly."""
    import auth
    monkeypatch.setattr(auth, "MAX_FAILS_PER_IP", 100_000)
