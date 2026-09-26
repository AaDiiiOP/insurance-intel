"""
Knowledge Core relationships — directed edges between two entities in the
same organization (Business -[owns]-> Asset, Policy -[provides]-> Coverage,
...). Thin, organization-scoped wrappers around repositories/knowledge.py.
"""
import db


def create_relationship(conn, *, organization_id: str, from_entity_id: str,
                         to_entity_id: str, relationship_type: str,
                         attributes: dict | None = None) -> dict:
    return db.create_relationship(
        conn, organization_id=organization_id, from_entity_id=from_entity_id,
        to_entity_id=to_entity_id, relationship_type=relationship_type,
        attributes=attributes,
    )


def get_or_create_relationship(conn, *, organization_id: str, from_entity_id: str,
                                to_entity_id: str, relationship_type: str,
                                attributes: dict | None = None) -> dict:
    """Avoids creating a duplicate `owns`/`has_policy`/etc. edge every time
    extraction re-runs on the same submission."""
    existing = db.find_relationship(conn, organization_id, from_entity_id, to_entity_id, relationship_type)
    if existing:
        return existing
    return create_relationship(
        conn, organization_id=organization_id, from_entity_id=from_entity_id,
        to_entity_id=to_entity_id, relationship_type=relationship_type,
        attributes=attributes,
    )


def find_relationships(conn, organization_id: str, entity_id: str,
                        relationship_type: str | None = None,
                        direction: str = "from") -> list[dict]:
    return db.find_relationships(conn, organization_id, entity_id, relationship_type, direction)
