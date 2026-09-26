"""
Knowledge Core versioning — the read side of valid_from/valid_to on
kc_facts. Writing a version is knowledge/facts.py's supersede_fact(); this
module only reads.
"""
import db


def get_current_value(conn, entity_id: str, organization_id: str, fact_type: str) -> dict | None:
    """The fact with valid_to IS NULL for this entity+fact_type — the
    current value, per the versioning convention."""
    return db.get_current_fact(conn, entity_id, organization_id, fact_type)


def get_historical_values(conn, entity_id: str, organization_id: str, fact_type: str) -> list[dict]:
    """Every version of this entity+fact_type, oldest first, including the
    current one — so a caller can render a full timeline in one call."""
    return db.get_historical_facts(conn, entity_id, organization_id, fact_type)
