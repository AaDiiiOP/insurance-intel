"""v0.8.2 deterministic entity normalization / alias handling (real Postgres)."""
import db
import knowledge.service as kc
from knowledge import entities as ent


def test_normalize_name():
    n = ent.normalize_name
    assert n("ABC Pvt. Ltd.") == n("ABC Private Limited") == "abc"
    assert n("Tata & Sons Ltd") == n("Tata and Sons Limited")
    assert n("Ltd") == "ltd"  # never normalizes to empty


def test_variant_spelling_reuses_entity_and_records_alias(conn, test_org):
    a = ent.get_or_create_entity(conn, organization_id=test_org, entity_type="business",
                                  name="Sunrise Textiles Pvt Ltd")
    b = ent.get_or_create_entity(conn, organization_id=test_org, entity_type="business",
                                  name="Sunrise Textiles Private Limited")
    assert str(a["id"]) == str(b["id"])
    assert "Sunrise Textiles Private Limited" in (b["attributes"] or {}).get("aliases", [])
    # the alias now matches directly too
    c = ent.get_or_create_entity(conn, organization_id=test_org, entity_type="business",
                                  name="sunrise textiles ltd")
    assert str(c["id"]) == str(a["id"])


def test_different_types_and_orgs_never_merge(conn, test_org):
    other = str(db.create_organization(conn, "pytest-org-other-norm")["id"])
    a = ent.get_or_create_entity(conn, organization_id=test_org, entity_type="business", name="Acme Ltd")
    b = ent.get_or_create_entity(conn, organization_id=test_org, entity_type="carrier", name="Acme Limited")
    c = ent.get_or_create_entity(conn, organization_id=other, entity_type="business", name="Acme Limited")
    assert len({str(a["id"]), str(b["id"]), str(c["id"])}) == 3


def test_ambiguous_match_creates_new_instead_of_guessing(conn, test_org):
    ent.create_entity(conn, organization_id=test_org, entity_type="business", name="Zeta Co")
    ent.create_entity(conn, organization_id=test_org, entity_type="business", name="Zeta Ltd")
    c = ent.get_or_create_entity(conn, organization_id=test_org, entity_type="business", name="Zeta Pvt Ltd")
    assert c["name"] == "Zeta Pvt Ltd"
    assert len(ent.list_entities(conn, test_org, "business")) == 3


def test_similar_but_different_names_are_flagged_not_merged(conn, test_org):
    ent.create_entity(conn, organization_id=test_org, entity_type="business", name="Sunrise Textile Mills")
    ent.create_entity(conn, organization_id=test_org, entity_type="business", name="Sunrise Textile Mills Unit 2")
    pairs = ent.find_possible_duplicates(conn, organization_id=test_org, entity_type="business")
    assert len(pairs) == 1 and pairs[0]["similarity"] >= 0.4
    assert len(ent.list_entities(conn, test_org, "business")) == 2


def test_list_and_duplicate_endpoints(conn, test_org):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import knowledge as api_knowledge

    ent.create_entity(conn, organization_id=test_org, entity_type="business", name="Orion Foods Mills")
    ent.create_entity(conn, organization_id=test_org, entity_type="business", name="Orion Foods Mills Unit 2")
    conn.commit()
    app = FastAPI(); app.include_router(api_knowledge.router)
    c = TestClient(app); h = {"X-Organization-Id": test_org}
    r = c.get("/knowledge/entities?entity_type=business", headers=h)
    assert r.status_code == 200 and len(r.json()["entities"]) == 2
    d = c.get("/knowledge/possible-duplicates", headers=h)
    assert d.status_code == 200 and len(d.json()["pairs"]) == 1


def test_baseline_and_renewal_are_not_flagged(conn, test_org):
    ent.create_entity(conn, organization_id=test_org, entity_type="policy", name="increased_sum_insured baseline 90e2a712 — policy")
    ent.create_entity(conn, organization_id=test_org, entity_type="policy", name="increased_sum_insured renewal 90e2a712 — policy")
    assert ent.find_possible_duplicates(conn, organization_id=test_org) == []
