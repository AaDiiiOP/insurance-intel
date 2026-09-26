"""
Knowledge Core facts — creation, lookup, and the versioning primitive
(supersede_fact). Thin, organization-scoped wrappers around
repositories/knowledge.py.

Versioning note: valid_from/valid_to on kc_facts IS the version mechanism —
there is deliberately no separate "fact_versions" table. The current value
for an entity+fact_type is the row with valid_to IS NULL; supersede_fact()
closes that row and opens a new one, so the full history is just every row
for that entity+fact_type ordered by valid_from. See knowledge/versions.py
for the read side of this.
"""
from datetime import datetime, timezone

import db

VALID_STATUSES = {"needs_review", "verified", "corrected", "contradiction_flagged"}
VALID_CONFIDENCE_TYPES = {"deterministic", "ocr_engine", "model_self_reported"}

# Which fact types describe exactly ONE true current value for an
# entity+fact_type at a time ("singleton") versus which describe a SET of
# independent, simultaneously-true instances ("multi-valued").
#
# reconcile_fact()'s contradiction-detection/tolerance-comparison logic only
# makes sense for singleton fact types: a property genuinely has one sum
# insured, one occupancy, one construction type at a time, so two documents
# naming different figures for the same entity really is a disagreement
# worth flagging. A multi-valued fact type like policy.exclusion is the
# opposite — "Flood excluded" and "War excluded" are two DIFFERENT true
# facts about the same policy, not two competing answers to the same
# question, so running the same disagreement check against them was a bug:
# every additional distinct exclusion/endorsement/risk-factor a document
# named looked like it was contradicting the ones already on record.
#
# Registry kept here (not entities.py or property_policy_intelligence.py)
# because "does this fact type have one current value or many" is a
# property of the fact type itself, independent of which vertical slice or
# entity type happens to use it.
SINGLETON_FACT_TYPES = {
    "property.sum_insured", "property.occupancy", "property.construction",
    "property.location", "policy.coverage_limit", "policy.deductible",
}

MULTI_VALUED_FACT_TYPES = {
    "policy.exclusion", "policy.endorsement", "property.risk_factors",
    "policy.coverage",
}


def is_singleton_fact_type(fact_type: str) -> bool:
    """
    Whether fact_type has one true current value at a time.

    Defaults to True (singleton) for an unregistered fact type — the safer
    default is to still run contradiction detection on something new rather
    than silently skip it, matching the existing "never silently agree
    without checking" rule this module already follows elsewhere.
    """
    if fact_type in MULTI_VALUED_FACT_TYPES:
        return False
    return True


def _now():
    return datetime.now(timezone.utc)


def create_fact(conn, *, organization_id: str, entity_id: str, fact_type: str,
                 value: str, confidence: dict, produced_by: str,
                 normalized_value: float | None = None, unit: str | None = None,
                 status: str = "needs_review", make_current: bool = True) -> dict:
    """
    Insert a new fact row.

    `confidence` is {value, type, method, note} — same shape and the same
    three confidence_type values as extraction_facts (never a calibrated
    probability, see doc_intelligence.py's module docstring for what each
    type actually claims).

    `make_current=True` (the default) sets valid_from=now(), valid_to=NULL —
    this fact becomes the current value for its entity+fact_type. Pass False
    only when inserting a fact that is deliberately not current (e.g. a
    superseded historical row being backfilled) — service.py's normal write
    path always leaves this True and closes any prior current fact itself.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown status '{status}'.")
    if confidence.get("type") not in VALID_CONFIDENCE_TYPES:
        raise ValueError(f"Unknown confidence type '{confidence.get('type')}'.")

    valid_from = _now() if make_current else None
    return db.create_fact(
        conn, organization_id=organization_id, entity_id=entity_id,
        fact_type=fact_type, value=value, normalized_value=normalized_value,
        unit=unit, confidence=confidence, status=status, produced_by=produced_by,
        valid_from=valid_from, valid_to=None,
    )


def get_fact(conn, fact_id: str, organization_id: str) -> dict | None:
    return db.get_fact(conn, fact_id, organization_id)


def list_facts_for_entity(conn, entity_id: str, organization_id: str,
                           fact_type: str | None = None,
                           current_only: bool = False) -> list[dict]:
    return db.list_facts_for_entity(conn, entity_id, organization_id, fact_type, current_only)


def supersede_fact(conn, fact_id: str, organization_id: str, *, new_value: str,
                    confidence: dict, produced_by: str,
                    normalized_value: float | None = None, unit: str | None = None,
                    status: str = "corrected") -> dict:
    """
    Version a fact: close the old row's valid_to and insert a new current
    row for the same entity+fact_type. This IS the versioning mechanism —
    the old value is never overwritten or deleted, so get_historical_values
    can always show what the fact used to say and when it changed.

    Raises ValueError if the fact does not exist (or belongs to a different
    organization) — the same 404-worthy condition every org-scoped lookup in
    this codebase treats identically to "does not exist".
    """
    old = db.get_fact(conn, fact_id, organization_id)
    if not old:
        raise ValueError("Fact not found.")

    now = _now()
    db.close_fact_validity(conn, fact_id, organization_id, now)

    return db.create_fact(
        conn,
        organization_id=organization_id,
        entity_id=old["entity_id"],
        fact_type=old["fact_type"],
        value=new_value,
        normalized_value=normalized_value,
        unit=unit if unit is not None else old.get("unit"),
        confidence=confidence,
        status=status,
        produced_by=produced_by,
        valid_from=now,
        valid_to=None,
    )


def set_fact_status(conn, fact_id: str, organization_id: str, status: str,
                     *, reviewed_by: str | None = None) -> dict | None:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown status '{status}'.")
    return db.set_fact_status(conn, fact_id, organization_id, status, reviewed_by=reviewed_by)


# NOTE: there used to be a correct_fact() here that called
# db.update_fact_value() to overwrite the CURRENT row's value in place. That
# destroyed history — the original value was gone the moment a correction
# was applied, with only status/reviewed_by hinting anything had changed.
# It has been removed: knowledge/service.py's correct_fact() is now the only
# correction path, and it goes through supersede_fact() above, which closes
# the old row's valid_to and inserts a new one — the original value always
# remains readable via get_historical_values()/GET /knowledge/facts/{id}/history.
# db.update_fact_value() itself is left in repositories/knowledge.py (unused
# by this package now) rather than deleted, since it is generic SQL, not the
# bug — the bug was calling it as if it were versioning.
