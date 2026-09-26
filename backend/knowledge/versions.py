"""
Knowledge Core versioning — the read side of valid_from/valid_to on
kc_facts. Writing a version is knowledge/facts.py's supersede_fact(); this
module only reads.
"""
import db


def get_current_value(conn, entity_id: str, organization_id: str, fact_type: str) -> dict | None:
    """
    The fact with valid_to IS NULL for this entity+fact_type — the current
    value, per the versioning convention.

    Deliberately does NOT check for an unresolved conflict — this is the
    plain "give me the row valid_to IS NULL" read, used internally by
    reconcile_fact() to decide whether a NEW fact should become current,
    agree with the current one, or open/extend a conflict. A caller asking
    "what should I show the user as THE current value" wants
    get_current_value_or_conflict() instead, which is conflict-aware.
    """
    return db.get_current_fact(conn, entity_id, organization_id, fact_type)


def get_current_value_or_conflict(conn, entity_id: str, organization_id: str,
                                   fact_type: str) -> dict:
    """
    What a reader should actually be told is "the current value" for this
    entity+fact_type — conflict-aware, unlike get_current_value() above.

    If there is an unresolved kc_conflicts row for this entity+fact_type,
    NO fact is picked as authoritative — the whole point of item 1's fix is
    that get_current_fact()'s "ORDER BY created_at DESC LIMIT 1" must not be
    read as an answer once two facts have been flagged as disagreeing.
    Instead this returns {"current_value": None, "unresolved_conflict":
    True, "conflicting_facts": [...]}, naming every fact still part of the
    open disagreement.

    Otherwise, returns {"current_value": <fact dict or None>,
    "unresolved_conflict": False, "conflicting_facts": []} — the ordinary
    valid_to IS NULL row, if one exists.
    """
    conflict = db.get_unresolved_conflict(conn, entity_id, organization_id, fact_type)
    if conflict is not None:
        fact_ids = db.get_conflict_fact_ids(conn, str(conflict["id"]))
        conflicting_facts = [
            f for f in (db.get_fact(conn, fid, organization_id) for fid in fact_ids)
            if f is not None
        ]
        return {
            "current_value": None,
            "unresolved_conflict": True,
            "conflict_id": str(conflict["id"]),
            "conflicting_facts": conflicting_facts,
        }

    current = db.get_current_fact(conn, entity_id, organization_id, fact_type)
    return {
        "current_value": current,
        "unresolved_conflict": False,
        "conflict_id": None,
        "conflicting_facts": [],
    }


def get_historical_values(conn, entity_id: str, organization_id: str, fact_type: str) -> list[dict]:
    """Every version of this entity+fact_type, oldest first, including the
    current one — so a caller can render a full timeline in one call."""
    return db.get_historical_facts(conn, entity_id, organization_id, fact_type)
