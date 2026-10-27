"""
v0.10.2 Phase 1B tenant-isolation audit — every Knowledge Core / query /
comparison / document endpoint, called by organization B with organization
A's ids. Nothing of A's may come back, and no write on A's data may succeed.
Isolation is application-level (organization_id in every query), not
PostgreSQL row-level security.
"""
import json

import pytest
from tests.auth_helpers import signed_in
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
import knowledge.service as kc
from api import carriers as api_carriers
from api import doc_intelligence as api_doc
from api import knowledge as api_knowledge
from api import submissions as api_submissions
from tests.test_knowledge_query import world  # noqa: F401  (fixture)

SECRET_VALUES = ("180,000,000", "150,000,000", "500,000", "Plastic moulding", "Orion")


@pytest.fixture
def client(conn):
    """Phase 2A: a factory — a client signed in to the given organization."""
    def make(org):
        return signed_in([api_knowledge.router, api_carriers.router, api_doc.router, api_submissions.router],
                         conn, org)
    return make


def _no_leak(resp):
    body = resp.text
    if resp.headers.get("content-type", "").startswith("application/json"):
        data = resp.json()
        if isinstance(data, dict):
            data.pop("question", None)          # the caller's own words echoed back
        body = json.dumps(data)
    assert not any(v in body for v in SECRET_VALUES), f"leak in {resp.request.url}: {body[:300]}"


def test_reads_from_another_tenant_return_nothing(conn, world, client):
    other = str(db.create_organization(conn, "pytest-org-isolation-b")["id"])
    sub = db.create_submission(conn, organization_id=world["org"], label="Orion submission")
    conn.commit()
    h = {"X-Organization-Id": other}
    client = client(other)
    pol, biz = str(world["policy"]["id"]), str(world["biz"]["id"])
    fact = str(world["facts"]["limit26"]["id"])
    doc = world["docs"]["d26"]

    not_found = [
        f"/knowledge/entities/{biz}",
        f"/knowledge/entities/{biz}/knowledge",
        f"/knowledge/entities/{biz}/knowledge?as_of=2026-06-01",
        f"/knowledge/entities/{biz}/relationships",
        f"/knowledge/entities/{pol}/facts",
        f"/knowledge/entities/{pol}/facts/policy.coverage_limit/current",
        f"/knowledge/facts/{fact}",
        f"/knowledge/facts/{fact}/history",
        f"/documents/{doc}/text",
        f"/doc-intelligence/submissions/{sub['id']}",
        f"/knowledge/compare-entities?left_entity_id={pol}&right_entity_id={pol}",
        f"/knowledge/compare-submissions?left_submission_id={sub['id']}&right_submission_id={sub['id']}",
    ]
    for url in not_found:
        r = client.get(url, headers=h)
        assert r.status_code in (400, 404), f"{url} -> {r.status_code}"
        _no_leak(r)

    for url in ("/knowledge/entities", "/knowledge/possible-duplicates", "/doc-intelligence/submissions",
                f"/documents/{doc}/chunks", f"/doc-intelligence/documents/{doc}/facts"):
        r = client.get(url, headers=h)
        assert r.status_code in (200, 400, 404), f"{url} -> {r.status_code}"
        _no_leak(r)

    q = client.post("/knowledge/query", headers=h,
                    json={"question": "What is the coverage limit for Orion Plastics?", "use_llm": False})
    assert q.status_code == 200 and q.json()["answer"] is None
    _no_leak(q)
    q2 = client.post("/knowledge/query", headers=h,
                     json={"question": "What is the coverage limit?", "entity_id": pol, "use_llm": False})
    assert q2.status_code == 200 and q2.json()["answer"] is None
    _no_leak(q2)


def test_writes_on_another_tenants_data_are_refused(conn, world, client):
    other = str(db.create_organization(conn, "pytest-org-isolation-w")["id"])
    sub = db.create_submission(conn, organization_id=world["org"], label="Orion submission w")
    conn.commit()
    h = {"X-Organization-Id": other}
    client = client(other)
    fact = str(world["facts"]["ded26"]["id"])

    r = client.put(f"/knowledge/facts/{fact}", headers=h,
                   json={"new_value": "1", "reviewed_by": "intruder", "reason": "synthetic cross-tenant probe"})
    assert r.status_code == 404           # v0.16.0: reason given, so only the tenant check can refuse it
    r = client.post(f"/knowledge/facts/{fact}/review", headers=h,
                    json={"action": "verify", "reviewed_by": "intruder"})
    assert r.status_code in (400, 404)
    r = client.post(f"/knowledge/submissions/{sub['id']}/analyze-property-policy", headers=h)
    assert r.status_code in (400, 404)

    # A's fact is untouched
    row = db.get_fact(conn, fact, world["org"])
    assert row["value"] == "500,000" and row["status"] == "needs_review" and row["valid_to"] is None
    with pytest.raises(Exception):
        kc.create_relationship(conn, organization_id=other, from_entity_id=str(world["biz"]["id"]),
                               to_entity_id=str(world["policy"]["id"]), relationship_type="has_policy")
