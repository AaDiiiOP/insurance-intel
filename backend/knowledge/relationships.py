"""
Knowledge Core relationships — directed edges between two entities in the
same organization (Business -[owns]-> Asset, Policy -[provides]-> Coverage,
...). Thin, organization-scoped wrappers around repositories/knowledge.py.
"""
import db

# Allowed relationship kinds and the entity types each may join
# (from-types, to-types). Enforced here in code rather than by a database
# CHECK so adding a kind later needs no migration. "provides" is the older
# name for policy->coverage and stays valid.
ALLOWED_RELATIONSHIPS: dict[str, tuple[frozenset, frozenset]] = {
    "owns":        (frozenset({"business"}), frozenset({"asset"})),
    "located_at":  (frozenset({"asset"}), frozenset({"location"})),
    "operates_at": (frozenset({"business"}), frozenset({"location"})),
    "has_policy":  (frozenset({"business"}), frozenset({"policy"})),
    "contains":    (frozenset({"policy"}), frozenset({"coverage"})),
    "provides":    (frozenset({"policy"}), frozenset({"coverage"})),
    "applies_to":  (frozenset({"policy"}), frozenset({"business"})),
    "protects":    (frozenset({"coverage"}), frozenset({"asset", "risk"})),
    "exposed_to":  (frozenset({"business"}), frozenset({"risk"})),
    "has_claim":   (frozenset({"business"}), frozenset({"claim"})),
    "supports":    (frozenset({"document"}), frozenset({
        "business", "asset", "location", "policy", "coverage", "risk", "claim"})),
}


def validate_relationship(conn, organization_id: str, from_entity_id: str,
                           to_entity_id: str, relationship_type: str) -> None:
    """Raises ValueError for an unknown kind, an entity that does not exist in
    THIS organization (a cross-tenant id looks identical to a missing one), or
    a kind that does not join those two entity types."""
    rule = ALLOWED_RELATIONSHIPS.get(relationship_type)
    if rule is None:
        raise ValueError(f"Unknown relationship type '{relationship_type}'. "
                         f"Allowed: {sorted(ALLOWED_RELATIONSHIPS)}")
    src = db.get_entity(conn, from_entity_id, organization_id)
    dst = db.get_entity(conn, to_entity_id, organization_id)
    if not src or not dst:
        raise ValueError("Both entities must exist in this organization.")
    if src["entity_type"] not in rule[0] or dst["entity_type"] not in rule[1]:
        raise ValueError(
            f"'{relationship_type}' cannot join {src['entity_type']} -> {dst['entity_type']}.")


def create_relationship(conn, *, organization_id: str, from_entity_id: str,
                         to_entity_id: str, relationship_type: str,
                         attributes: dict | None = None) -> dict:
    validate_relationship(conn, organization_id, from_entity_id, to_entity_id,
                          relationship_type)
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
