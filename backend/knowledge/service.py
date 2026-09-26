"""
Knowledge Core — the single public interface.

Everything outside the `knowledge` package should call functions in this
module, not entities.py/facts.py/evidence.py/relationships.py/versions.py
directly — this mirrors how the rest of the codebase calls doc_intelligence.py
rather than reaching into repositories/extraction.py itself.

The one piece of real logic here (beyond composing the submodules) is
reconcile_fact(): when a newly-extracted fact disagrees with the current fact
already on record for the same entity+fact_type, BOTH are flagged
contradiction_flagged rather than one silently overwriting the other — the
exact same rule doc_intelligence.find_contradiction() enforces for
extraction_facts, reused here rather than reimplemented, because the
question ("do two supposedly-equal figures actually agree?") does not change
just because the fact now lives on an entity instead of a bare document.
"""
from doc_intelligence import CONTRADICTION_TOLERANCE, _numeric_value

import db

from . import entities as _entities
from . import evidence as _evidence
from . import facts as _facts
from . import relationships as _relationships
from . import versions as _versions

# Re-exported so callers of knowledge.service don't need to know the entity
# CHECK list lives in knowledge/entities.py.
ENTITY_TYPES = _entities.ENTITY_TYPES


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                   attributes: dict | None = None) -> dict:
    return _entities.create_entity(
        conn, organization_id=organization_id, entity_type=entity_type,
        name=name, attributes=attributes,
    )


def get_or_create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                          attributes: dict | None = None) -> dict:
    return _entities.get_or_create_entity(
        conn, organization_id=organization_id, entity_type=entity_type,
        name=name, attributes=attributes,
    )


def get_entity(conn, entity_id: str, organization_id: str) -> dict | None:
    return _entities.get_entity(conn, entity_id, organization_id)


def list_entities(conn, organization_id: str, entity_type: str | None = None) -> list[dict]:
    return _entities.list_entities(conn, organization_id, entity_type)


def update_entity(conn, entity_id: str, organization_id: str, **kwargs) -> dict | None:
    return _entities.update_entity(conn, entity_id, organization_id, **kwargs)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def get_fact(conn, fact_id: str, organization_id: str) -> dict | None:
    return _facts.get_fact(conn, fact_id, organization_id)


def find_facts(conn, entity_id: str, organization_id: str, *,
                fact_type: str | None = None, current_only: bool = False) -> list[dict]:
    return _facts.list_facts_for_entity(conn, entity_id, organization_id, fact_type, current_only)


def update_fact(conn, fact_id: str, organization_id: str, *, new_value: str,
                 normalized_value: float | None = None, reviewed_by: str) -> dict | None:
    """
    Human review correction — VERSIONS the fact via correct_fact() below
    (facts.supersede_fact under the hood) rather than overwriting the
    existing row's value in place. The old row's original value survives
    with valid_to now set; a new row carries the correction. If this
    correction resolves an open contradiction, the matching kc_conflicts
    row is marked resolved automatically. See knowledge/service.correct_fact
    for the full contract.
    """
    try:
        return correct_fact(
            conn, fact_id, organization_id, new_value=new_value,
            normalized_value=normalized_value, reviewed_by=reviewed_by,
        )
    except ValueError:
        return None


def get_current_value(conn, entity_id: str, organization_id: str, fact_type: str) -> dict | None:
    return _versions.get_current_value(conn, entity_id, organization_id, fact_type)


def get_historical_values(conn, entity_id: str, organization_id: str, fact_type: str) -> list[dict]:
    return _versions.get_historical_values(conn, entity_id, organization_id, fact_type)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def link_evidence(conn, *, organization_id: str, fact_id: str, document_id: str,
                   page: int | None = None, section: str | None = None,
                   snippet: str | None = None, source: str | None = None) -> dict:
    return _evidence.create_evidence(
        conn, organization_id=organization_id, fact_id=fact_id, document_id=document_id,
        page=page, section=section, snippet=snippet, source=source,
    )


def get_evidence_for_fact(conn, fact_id: str, organization_id: str) -> list[dict]:
    return _evidence.get_evidence_for_fact(conn, fact_id, organization_id)


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def create_relationship(conn, *, organization_id: str, from_entity_id: str,
                         to_entity_id: str, relationship_type: str,
                         attributes: dict | None = None) -> dict:
    return _relationships.get_or_create_relationship(
        conn, organization_id=organization_id, from_entity_id=from_entity_id,
        to_entity_id=to_entity_id, relationship_type=relationship_type,
        attributes=attributes,
    )


def find_relationships(conn, organization_id: str, entity_id: str,
                        relationship_type: str | None = None,
                        direction: str = "from") -> list[dict]:
    return _relationships.find_relationships(conn, organization_id, entity_id, relationship_type, direction)


# ---------------------------------------------------------------------------
# The write path: create_fact() + reconcile_fact()
# ---------------------------------------------------------------------------

def create_fact(conn, *, organization_id: str, entity_id: str, fact_type: str,
                 value: str, confidence: dict, produced_by: str,
                 normalized_value: float | None = None, unit: str | None = None,
                 status: str = "needs_review") -> dict:
    """
    Create a fact directly, with no reconciliation against any existing
    current fact for the same entity+fact_type. Most callers extracting a
    fact for the first time should go through reconcile_fact() instead,
    which calls this internally after checking for a contradiction — this is
    exposed separately for a caller that already knows there is nothing to
    reconcile against (e.g. a fact type that has no possible duplicate, or a
    caller restoring a known-good historical value).
    """
    return _facts.create_fact(
        conn, organization_id=organization_id, entity_id=entity_id,
        fact_type=fact_type, value=value, confidence=confidence,
        produced_by=produced_by, normalized_value=normalized_value, unit=unit,
        status=status,
    )


def reconcile_fact(conn, *, organization_id: str, entity_id: str, fact_type: str,
                    value: str, confidence: dict, produced_by: str,
                    normalized_value: float | None = None, unit: str | None = None,
                    document_id: str | None = None, evidence_page: int | None = None,
                    evidence_section: str | None = None, evidence_snippet: str | None = None,
                    evidence_source: str | None = None) -> dict:
    """
    Write a newly-extracted fact for entity_id+fact_type.

    First checks whether fact_type is singleton or multi-valued
    (knowledge/facts.py's SINGLETON_FACT_TYPES/MULTI_VALUED_FACT_TYPES):

    - Multi-valued (e.g. policy.exclusion, policy.endorsement,
      property.risk_factors, policy.coverage): a document can state several
      DIFFERENT true instances of this fact type at once ("Flood excluded"
      and "War excluded" are both true about the same policy, not competing
      answers). No reconciliation/contradiction check runs at all — the new
      fact is simply inserted as its own current-eligible row, status
      'needs_review', with its own evidence. Nothing here ever treats a new
      distinct value for a multi-valued fact type as disagreeing with an
      existing one.

    - Singleton (e.g. property.sum_insured, property.occupancy,
      policy.coverage_limit, policy.deductible): checked against whatever is
      already the CURRENT fact for that same entity+fact_type, mirroring
      doc_intelligence.find_contradiction()'s rule exactly, just at the
      entity level instead of the submission level:

        - No current fact yet: the new fact becomes current
          (valid_from=now(), valid_to=NULL), status stays whatever was
          passed in (normally 'needs_review' — nothing here is ever
          auto-verified, same "no silent guessing" rule as Document
          Intelligence).
        - A current fact exists and the two values agree (within
          CONTRADICTION_TOLERANCE, comparing normalized_value when both
          facts have one, or the free-text value otherwise): the new
          evidence is attached to the EXISTING current fact instead of
          creating a duplicate row.
        - A current fact exists and the two values DISAGREE beyond
          tolerance: BOTH are kept and BOTH are flagged
          'contradiction_flagged', exactly like extraction_facts — AND a
          kc_conflicts row is opened (or, if one is already open for this
          entity+fact_type, the new fact is linked onto it) so a reader
          asking "what is the current value" is told about the unresolved
          disagreement instead of get_current_fact()'s
          "ORDER BY created_at DESC LIMIT 1" silently picking the newer
          side as if it had won. A reviewer resolves this by correcting one
          via update_fact() -> facts.supersede_fact(), which also resolves
          the conflict row (see resolve_conflict_with_correction below).

    Returns the fact row that resulted for the NEW value (either the
    newly-created fact, or the existing current fact if evidence was merged
    into it instead) — callers that need both sides of a contradiction can
    read `contradicts` off the returned dict's `_contradiction` key.

    Evidence is linked automatically whenever `document_id` is given —
    every value this creates is traceable Fact -> Evidence -> Document,
    same evidence rigor as Phase 0, never optional.
    """
    contradiction_info = None

    if not _facts.is_singleton_fact_type(fact_type):
        # Multi-valued: every extraction is its own new, independently-true
        # fact. Never compared against, or flagged as contradicting, any
        # other fact of the same type for this entity.
        fact = _facts.create_fact(
            conn, organization_id=organization_id, entity_id=entity_id,
            fact_type=fact_type, value=value, confidence=confidence,
            produced_by=produced_by, normalized_value=normalized_value, unit=unit,
            status="needs_review",
        )
    else:
        current = _versions.get_current_value(conn, entity_id, organization_id, fact_type)

        if current is None:
            fact = _facts.create_fact(
                conn, organization_id=organization_id, entity_id=entity_id,
                fact_type=fact_type, value=value, confidence=confidence,
                produced_by=produced_by, normalized_value=normalized_value, unit=unit,
                status="needs_review",
            )
        else:
            agree = _values_agree(current, value, normalized_value)
            if agree:
                fact = current
            else:
                # Disagreement: keep BOTH, flag BOTH. Neither is silently
                # overwritten or picked as "the" answer.
                fact = _facts.create_fact(
                    conn, organization_id=organization_id, entity_id=entity_id,
                    fact_type=fact_type, value=value, confidence=confidence,
                    produced_by=produced_by, normalized_value=normalized_value, unit=unit,
                    status="contradiction_flagged",
                )
                db.set_fact_status(conn, str(current["id"]), organization_id, "contradiction_flagged")
                current = db.get_fact(conn, str(current["id"]), organization_id)
                contradiction_info = {
                    "other_fact_id": str(current["id"]),
                    "other_value": current["value"],
                }

                # Open (or extend) the kc_conflicts row for this
                # entity+fact_type — this is what makes the contradiction
                # visible to a reader asking "what's the current value"
                # instead of only living in each fact's own status column.
                conflict = db.get_unresolved_conflict(conn, entity_id, organization_id, fact_type)
                if conflict is None:
                    db.create_conflict(
                        conn, organization_id=organization_id, entity_id=entity_id,
                        fact_type=fact_type,
                        fact_ids=[str(current["id"]), str(fact["id"])],
                    )
                else:
                    db.add_fact_to_conflict(conn, str(conflict["id"]), str(fact["id"]))

    if document_id:
        _evidence.create_evidence(
            conn, organization_id=organization_id, fact_id=str(fact["id"]),
            document_id=document_id, page=evidence_page, section=evidence_section,
            snippet=evidence_snippet, source=evidence_source,
        )

    fact = dict(fact)
    fact["_contradiction"] = contradiction_info
    return fact


def get_current_value_or_conflict(conn, entity_id: str, organization_id: str,
                                   fact_type: str) -> dict:
    """
    The conflict-aware read of "what is the current value" for an
    entity+fact_type — see knowledge/versions.py's function of the same
    name for the full contract. Callers that want to SHOW a user a current
    value (as opposed to reconcile_fact()'s internal write-path check)
    should call this, not get_current_value().
    """
    return _versions.get_current_value_or_conflict(conn, entity_id, organization_id, fact_type)


def correct_fact(conn, fact_id: str, organization_id: str, *, new_value: str,
                  normalized_value: float | None = None, unit: str | None = None,
                  confidence: dict | None = None, reviewed_by: str) -> dict:
    """
    A human review correction — VERSIONS the fact rather than overwriting it
    in place: closes the old row's valid_to and inserts a new current row
    carrying the corrected value, status='corrected' (facts.supersede_fact).

    If the fact being corrected was part of an unresolved kc_conflicts row
    (i.e. it or its current counterpart was 'contradiction_flagged'), that
    conflict is marked resolved with resolution_fact_id pointing at the
    NEW, corrected fact — a human just settled which value is right, so the
    "no single current value" state this conflict represented no longer
    holds.

    Raises ValueError if the fact does not exist for this organization
    (facts.supersede_fact's existing behaviour, unchanged).
    """
    old = db.get_fact(conn, fact_id, organization_id)
    if not old:
        raise ValueError("Fact not found.")

    correction_confidence = confidence or {
        "value": 1.0, "type": "model_self_reported",
        "method": "human_review_correction", "note": None,
    }

    new_fact = _facts.supersede_fact(
        conn, fact_id, organization_id, new_value=new_value,
        confidence=correction_confidence, produced_by=f"human:{reviewed_by}",
        normalized_value=normalized_value, unit=unit, status="corrected",
    )
    db.set_fact_status(
        conn, str(new_fact["id"]), organization_id, "corrected", reviewed_by=reviewed_by,
    )
    new_fact = db.get_fact(conn, str(new_fact["id"]), organization_id)

    conflict = db.get_unresolved_conflict(
        conn, str(old["entity_id"]), organization_id, old["fact_type"]
    )
    if conflict is not None:
        db.resolve_conflict(
            conn, str(conflict["id"]), organization_id,
            resolution_fact_id=str(new_fact["id"]),
        )

    return new_fact


def _values_agree(current_fact: dict, new_value: str, new_normalized: float | None) -> bool:
    """
    Whether a newly-extracted value agrees with the current fact, within
    CONTRADICTION_TOLERANCE — reusing doc_intelligence's numeric-comparison
    logic exactly (strip currency/formatting, compare relative difference),
    rather than re-deriving the same rule a second time.

    A fact with no recoverable number on either side is treated as
    "cannot compare" -> agree (nothing to contradict), same principle as
    doc_intelligence.find_contradiction() skipping facts it cannot parse a
    number from, applied here as "don't flag what can't be compared" rather
    than "assume the worst".
    """
    current_number = (
        float(current_fact["normalized_value"])
        if current_fact.get("normalized_value") is not None
        else _numeric_value(current_fact["value"])
    )
    new_number = new_normalized if new_normalized is not None else _numeric_value(new_value)

    if current_number is None or new_number is None:
        # Not a numeric fact type (e.g. a free-text occupancy description or
        # exclusion clause) — compared as text instead. Case/whitespace
        # differences are not a contradiction; anything else is.
        return " ".join(str(current_fact["value"]).split()).lower() == " ".join(str(new_value).split()).lower()

    if current_number == 0:
        return True  # avoid a division by zero on a degenerate value

    relative_difference = abs(new_number - current_number) / abs(current_number)
    return relative_difference <= CONTRADICTION_TOLERANCE
