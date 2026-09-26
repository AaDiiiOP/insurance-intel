"""
Knowledge Core entities — thin, organization-scoped wrappers around
repositories/knowledge.py's entity functions.

Nothing here talks SQL directly; that lives in repositories/knowledge.py,
same layering as doc_intelligence.py -> repositories/extraction.py.
"""
import db

ENTITY_TYPES = {
    "business", "carrier", "policy", "document", "location", "asset",
    "coverage", "claim", "risk", "person",
}


def create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                   attributes: dict | None = None) -> dict:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"Unknown entity_type '{entity_type}'. Must be one of: {sorted(ENTITY_TYPES)}")
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Entity name cannot be empty.")
    return db.create_entity(
        conn, organization_id=organization_id, entity_type=entity_type,
        name=name, attributes=attributes,
    )


def get_or_create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                          attributes: dict | None = None) -> dict:
    """
    Find an existing entity of this type/name in the organization, or create
    one. Used by the Property Policy Intelligence slice so re-running
    extraction on the same submission does not create duplicate Business /
    Asset / Policy entities every time.
    """
    existing = db.find_entity_by_name(conn, organization_id, entity_type, name)
    if existing:
        return existing
    return create_entity(
        conn, organization_id=organization_id, entity_type=entity_type,
        name=name, attributes=attributes,
    )


def get_entity(conn, entity_id: str, organization_id: str) -> dict | None:
    return db.get_entity(conn, entity_id, organization_id)


def list_entities(conn, organization_id: str, entity_type: str | None = None) -> list[dict]:
    return db.list_entities(conn, organization_id, entity_type)


def update_entity(conn, entity_id: str, organization_id: str, *,
                   name: str | None = None, attributes: dict | None = None) -> dict | None:
    return db.update_entity(conn, entity_id, organization_id, name=name, attributes=attributes)
