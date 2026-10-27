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


def find_possible_duplicates(conn, *, organization_id: str, entity_type: str | None = None) -> list[dict]:
    """Look-alike entity pairs for human review (never merged automatically)."""
    return _entities.find_possible_duplicates(conn, organization_id=organization_id,
                                              entity_type=entity_type)


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
                 status: str = "needs_review",
                 effective_from=None, effective_to=None) -> dict:
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
        status=status, effective_from=effective_from, effective_to=effective_to,
    )


def reconcile_fact(conn, *, organization_id: str, entity_id: str, fact_type: str,
                    value: str, confidence: dict, produced_by: str,
                    normalized_value: float | None = None, unit: str | None = None,
                    document_id: str | None = None, evidence_page: int | None = None,
                    evidence_section: str | None = None, evidence_snippet: str | None = None,
                    evidence_source: str | None = None,
                    effective_from=None, effective_to=None) -> dict:
    """
    Write a newly-extracted fact for entity_id+fact_type.

    effective_from / effective_to (optional, date or ISO string) are the
    period the SOURCE says the value applies to (e.g. a 2025 policy period).
    When the new fact AND the current fact both state a period and the two
    periods do not overlap, they are different truths about different times,
    not a contradiction: both are kept, the later period stays current and
    the earlier one is closed as historical. Without stated periods on both
    sides the behaviour below is unchanged.

    First checks whether fact_type is singleton or multi-valued
    (knowledge/fact_types.py's FACT_DEFINITIONS registry):

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
    effective_from = _facts.to_date(effective_from)
    effective_to = _facts.to_date(effective_to)
    period = {"effective_from": effective_from, "effective_to": effective_to}

    if not _facts.is_singleton_fact_type(fact_type):
        # Multi-valued: every extraction is its own new, independently-true
        # fact. Never compared against, or flagged as contradicting, any
        # other fact of the same type for this entity.
        fact = _facts.create_fact(
            conn, organization_id=organization_id, entity_id=entity_id,
            fact_type=fact_type, value=value, confidence=confidence,
            produced_by=produced_by, normalized_value=normalized_value, unit=unit,
            status="needs_review", **period,
        )
    else:
        current = _versions.get_current_value(conn, entity_id, organization_id, fact_type)

        if current is None:
            fact = _facts.create_fact(
                conn, organization_id=organization_id, entity_id=entity_id,
                fact_type=fact_type, value=value, confidence=confidence,
                produced_by=produced_by, normalized_value=normalized_value, unit=unit,
                status="needs_review", **period,
            )
        elif _different_periods(current, effective_from, effective_to) and \
                (same := _same_period_fact(conn, organization_id, entity_id, fact_type,
                                           effective_from, effective_to)) is not None:
            # v0.13.0 fix (found by Phase 1E temporal tests): the new value is for
            # an EARLIER period that already has a value (e.g. a 2026 document
            # read after the 2027 renewal). Compare it with THAT period's value,
            # not with the current (later) period — otherwise a disagreement
            # inside the earlier period was silently stored as history and the
            # later-recorded value answered for that period.
            if _values_agree(same, value, normalized_value):
                fact = same
            else:
                fact = _facts.create_fact(
                    conn, organization_id=organization_id, entity_id=entity_id,
                    fact_type=fact_type, value=value, confidence=confidence,
                    produced_by=produced_by, normalized_value=normalized_value, unit=unit,
                    status="contradiction_flagged", **period,
                )
                if same.get("valid_to") is not None:      # keep both visible for that period
                    fact = db.close_fact_validity(conn, str(fact["id"]), organization_id, same["valid_to"])
                db.set_fact_status(conn, str(same["id"]), organization_id, "contradiction_flagged")
                contradiction_info = {"other_fact_id": str(same["id"]), "other_value": same["value"]}
                conflict = db.get_unresolved_conflict(conn, entity_id, organization_id, fact_type)
                if conflict is None:
                    db.create_conflict(conn, organization_id=organization_id, entity_id=entity_id,
                                       fact_type=fact_type, fact_ids=[str(same["id"]), str(fact["id"])])
                else:
                    db.add_fact_to_conflict(conn, str(conflict["id"]), str(fact["id"]))
        elif _different_periods(current, effective_from, effective_to):
            # Two values for two non-overlapping stated periods (2025 limit vs
            # 2026 limit): history, not disagreement. Keep both; the later
            # period is current, the earlier is closed as historical. Neither
            # is overwritten, so both stay queryable (get_facts_in_force).
            fact = _facts.create_fact(
                conn, organization_id=organization_id, entity_id=entity_id,
                fact_type=fact_type, value=value, confidence=confidence,
                produced_by=produced_by, normalized_value=normalized_value, unit=unit,
                status="needs_review", **period,
            )
            now = _facts._now()
            if effective_from >= current["effective_from"]:
                # Close the WHOLE earlier period, not just one row of it: if that
                # period holds conflicting values (two documents disagree), closing
                # only `current` left the other one open, so the earlier period
                # then showed a single value and the conflict lost a side.
                for f in db.list_facts_for_entity(conn, entity_id, organization_id, fact_type, False):
                    if (f.get("valid_to") is None and f["status"] != "superseded"
                            and f.get("effective_from") == current.get("effective_from")
                            and f.get("effective_to") == current.get("effective_to")):
                        db.close_fact_validity(conn, str(f["id"]), organization_id, now)
            else:
                fact = db.close_fact_validity(conn, str(fact["id"]), organization_id, now)
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
                    status="contradiction_flagged", **period,
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
    holds. Resolving the conflict also CLOSES OUT every other fact that was
    part of it (every fact_id linked via kc_conflict_facts, other than the
    one just corrected and the new row superseding it): each loses its
    'contradiction_flagged' status in favour of 'superseded', and gets the
    same valid_to timestamp as the correction. Without this, the losing
    side of the conflict would be left with valid_to IS NULL and status
    'contradiction_flagged' forever — a second row satisfying "current"
    for the same entity+fact_type even though the conflict is resolved,
    which only "happens to" read correctly because get_current_value_or_
    conflict() picks the newest row; the invariant (at most one row with
    valid_to IS NULL per entity+fact_type once resolved) was never actually
    enforced at the data level. This closes it at the data level instead.

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

        # Close out every OTHER fact that was part of this conflict — the
        # losing side(s). `fact_id` (the one just corrected) is now itself
        # closed by supersede_fact() above; `new_fact["id"]` is the new
        # current row and must stay open. Everything else linked to this
        # conflict was flagged 'contradiction_flagged' and is still sitting
        # with valid_to IS NULL — that ends here.
        resolved_at = new_fact["valid_from"]
        for other_fact_id in db.get_conflict_fact_ids(conn, str(conflict["id"])):
            if other_fact_id in (str(fact_id), str(new_fact["id"])):
                continue
            db.close_fact_validity(conn, other_fact_id, organization_id, resolved_at)
            db.set_fact_status(conn, other_fact_id, organization_id, "superseded")

    return new_fact


def _same_period_fact(conn, organization_id, entity_id, fact_type, eff_from, eff_to):
    """The latest non-rejected fact stating exactly this period, if any."""
    if eff_from is None:
        return None
    rows = [f for f in db.list_facts_for_entity(conn, entity_id, organization_id, fact_type, False)
            if f["status"] != "superseded" and f.get("effective_from") == eff_from
            and f.get("effective_to") == eff_to]
    return max(rows, key=lambda f: f["valid_from"]) if rows else None


def _different_periods(current: dict, new_from, new_to) -> bool:
    """True only when BOTH facts state a period and the periods do not
    overlap. A missing period on either side means the period is unknown, so
    the facts are compared by value exactly as before."""
    cur_from, cur_to = current.get("effective_from"), current.get("effective_to")
    if new_from is None or cur_from is None:
        return False
    far = _facts.date.max
    return not (new_from <= (cur_to or far) and cur_from <= (new_to or far))


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


# ---------------------------------------------------------------------------
# Reading everything known about one entity, and human review
# ---------------------------------------------------------------------------

def get_facts_in_force(conn, entity_id: str, organization_id: str, as_of,
                        fact_type: str | None = None) -> dict:
    """What was true on a date (see versions.get_facts_in_force)."""
    return _versions.get_facts_in_force(conn, entity_id, organization_id, as_of, fact_type)


def get_entity_knowledge(conn, entity_id: str, organization_id: str, as_of=None) -> dict | None:
    """
    Everything the Knowledge Core holds about one entity, in one organization-
    scoped read: current facts (each with its evidence), historical facts,
    unresolved conflicts, relationships in both directions (with the other
    entity's type and name), and — when `as_of` is given — what was in force
    on that date, separate from "current". None if the entity does not exist
    in this organization (a cross-tenant id looks identical to a missing one).
    """
    entity = db.get_entity(conn, entity_id, organization_id)
    if not entity:
        return None

    all_facts = db.list_facts_for_entity(conn, entity_id, organization_id)
    current, historical = [], []
    for f in all_facts:
        f = dict(f)
        if f.get("valid_to") is None and f.get("status") != "superseded":
            f["evidence"] = db.get_evidence_for_fact(conn, str(f["id"]), organization_id)
            current.append(f)
        else:
            f["evidence"] = db.get_evidence_for_fact(conn, str(f["id"]), organization_id)
            historical.append(f)

    conflicts = []
    for fact_type in sorted({f["fact_type"] for f in current}):
        c = db.get_unresolved_conflict(conn, entity_id, organization_id, fact_type)
        if c is not None:
            conflicts.append({"conflict_id": str(c["id"]), "fact_type": fact_type,
                              "fact_ids": db.get_conflict_fact_ids(conn, str(c["id"]))})

    def _edges(direction: str) -> list[dict]:
        out = []
        for r in _relationships.find_relationships(conn, organization_id, entity_id, None, direction):
            other_id = r["to_entity_id"] if direction == "from" else r["from_entity_id"]
            other = db.get_entity(conn, str(other_id), organization_id)
            if other is None:      # never reveal an entity outside this organization
                continue
            out.append({"relationship_id": str(r["id"]), "type": r["relationship_type"],
                        "entity_id": str(other["id"]), "entity_type": other["entity_type"],
                        "name": other["name"]})
        return out

    result = {
        "entity": entity,
        "current_facts": current,
        "historical_facts": historical,
        "unresolved_conflicts": conflicts,
        "relationships": {"outgoing": _edges("from"), "incoming": _edges("to")},
    }
    if as_of is not None:
        result["in_force"] = _versions.get_facts_in_force(conn, entity_id, organization_id, as_of)
    return result


def review_fact(conn, fact_id: str, organization_id: str, *, action: str,
                 reviewed_by: str) -> dict:
    """
    Human review of one fact.

      verify  needs_review -> verified. Refused unless the fact has at least
              one evidence row (nothing is verified without evidence) and is
              not part of a contradiction (a conflict is settled by
              correct_fact(), which picks the right value).
      reject  needs_review -> superseded, closed as no longer current. The row
              is kept for audit; it is never deleted.

    Raises ValueError for a missing fact (or one in another organization), an
    unknown action, a missing reviewer name, or a fact in a state that action
    does not apply to.
    """
    reviewed_by = " ".join((reviewed_by or "").split())
    if not reviewed_by:
        raise ValueError("A reviewer name is required.")
    if action not in ("verify", "reject"):
        raise ValueError("action must be 'verify' or 'reject'.")
    fact = db.get_fact(conn, fact_id, organization_id)
    if not fact:
        raise ValueError("Fact not found.")
    if fact["status"] != "needs_review":
        raise ValueError(f"Only a needs_review fact can be reviewed (this one is {fact['status']}).")

    if action == "verify":
        if not db.get_evidence_for_fact(conn, fact_id, organization_id):
            raise ValueError("A fact with no evidence cannot be verified.")
        return db.set_fact_status(conn, fact_id, organization_id, "verified", reviewed_by=reviewed_by)

    db.close_fact_validity(conn, fact_id, organization_id, _facts._now())
    return db.set_fact_status(conn, fact_id, organization_id, "superseded", reviewed_by=reviewed_by)
