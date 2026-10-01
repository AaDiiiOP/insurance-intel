"""
Knowledge Core — SQL access for kc_entities / kc_facts / kc_evidence /
kc_relationships.

Same convention as every other repositories/*.py module: every function that
touches one of these tables takes organization_id and filters on it INSIDE
the query itself, never fetch-then-check in Python. A row belonging to a
different organization is indistinguishable from one that does not exist.

This module is the only place that writes raw SQL against the Knowledge Core
tables; backend/knowledge/*.py calls into it rather than opening its own
queries, the same layering doc_intelligence.py (business logic) already has
over repositories/extraction.py (SQL) via db.py's shim.
"""
from psycopg.types.json import Jsonb

VALID_ENTITY_TYPES = {
    "business", "carrier", "policy", "document", "location", "asset",
    "coverage", "claim", "risk", "person",
}

VALID_CONFIDENCE_TYPES = {"deterministic", "ocr_engine", "model_self_reported"}

VALID_FACT_STATUSES = {"needs_review", "verified", "corrected", "contradiction_flagged", "superseded"}


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                   attributes: dict | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kc_entities (organization_id, entity_type, name, attributes)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (organization_id, entity_type, name, Jsonb(attributes or {})),
        )
        return dict(cur.fetchone())


def get_entity(conn, entity_id: str, organization_id: str) -> dict | None:
    """Scoped to the caller's organization — filtered in the query itself,
    same pattern as get_extraction_fact / get_submission."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM kc_entities WHERE id = %s AND organization_id = %s",
            (entity_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def find_entity_by_name(conn, organization_id: str, entity_type: str, name: str) -> dict | None:
    """
    Get-or-create support: find an existing entity of this type and name
    within the organization, case-insensitively, so re-running extraction on
    the same submission does not create a duplicate Business/Asset/Policy
    entity every time.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM kc_entities
            WHERE organization_id = %s AND entity_type = %s AND lower(name) = lower(%s)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (organization_id, entity_type, name),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_entities(conn, organization_id: str, entity_type: str | None = None) -> list[dict]:
    with conn.cursor() as cur:
        if entity_type:
            cur.execute(
                "SELECT * FROM kc_entities WHERE organization_id = %s AND entity_type = %s "
                "ORDER BY created_at DESC",
                (organization_id, entity_type),
            )
        else:
            cur.execute(
                "SELECT * FROM kc_entities WHERE organization_id = %s ORDER BY created_at DESC",
                (organization_id,),
            )
        return cur.fetchall()


def update_entity(conn, entity_id: str, organization_id: str, *,
                   name: str | None = None, attributes: dict | None = None) -> dict | None:
    """
    Partial update, scoped to the caller's organization. `attributes`, when
    given, REPLACES the stored jsonb wholesale — callers that only want to
    add a field should read-merge-write, same as every other jsonb column in
    this codebase (e.g. house_style.guidance edits).
    """
    fields, values = [], []
    if name is not None:
        fields.append("name = %s")
        values.append(name)
    if attributes is not None:
        fields.append("attributes = %s")
        values.append(Jsonb(attributes))
    if not fields:
        return get_entity(conn, entity_id, organization_id)

    fields.append("updated_at = now()")
    values.extend([entity_id, organization_id])
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE kc_entities SET {', '.join(fields)} "
            f"WHERE id = %s AND organization_id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def create_fact(conn, *, organization_id: str, entity_id: str, fact_type: str,
                 value: str, normalized_value: float | None, unit: str | None,
                 confidence: dict, status: str, produced_by: str,
                 valid_from, valid_to=None, effective_from=None,
                 effective_to=None) -> dict:
    """
    Insert one fact row. `confidence` is {value, type, method, note}, same
    shape as extraction.save_extraction_fact's `confidence` argument.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kc_facts (
                organization_id, entity_id, fact_type, value, normalized_value, unit,
                confidence_value, confidence_type, confidence_method, confidence_note,
                status, produced_by, valid_from, valid_to,
                effective_from, effective_to
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                organization_id, entity_id, fact_type, value, normalized_value, unit,
                confidence["value"], confidence["type"], confidence.get("method"),
                confidence.get("note"),
                status, produced_by, valid_from, valid_to,
                effective_from, effective_to,
            ),
        )
        return dict(cur.fetchone())


def get_fact(conn, fact_id: str, organization_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM kc_facts WHERE id = %s AND organization_id = %s",
            (fact_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_facts_for_entity(conn, entity_id: str, organization_id: str,
                           fact_type: str | None = None,
                           current_only: bool = False) -> list[dict]:
    clauses = ["entity_id = %s", "organization_id = %s"]
    params = [entity_id, organization_id]
    if fact_type:
        clauses.append("fact_type = %s")
        params.append(fact_type)
    if current_only:
        clauses.append("valid_to IS NULL")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM kc_facts WHERE {' AND '.join(clauses)} "
            f"ORDER BY fact_type, created_at DESC",
            params,
        )
        return cur.fetchall()


def get_current_fact(conn, entity_id: str, organization_id: str, fact_type: str) -> dict | None:
    """The current value for an entity+fact_type — the row with valid_to IS
    NULL, per the versioning convention. At most one such row should exist
    at a time (service.py's write path enforces this by always closing the
    old row before opening a new one)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM kc_facts WHERE entity_id = %s AND organization_id = %s "
            "AND fact_type = %s AND valid_to IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (entity_id, organization_id, fact_type),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_historical_facts(conn, entity_id: str, organization_id: str, fact_type: str) -> list[dict]:
    """Every version of an entity+fact_type, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM kc_facts WHERE entity_id = %s AND organization_id = %s "
            "AND fact_type = %s ORDER BY COALESCE(valid_from, created_at) ASC",
            (entity_id, organization_id, fact_type),
        )
        return cur.fetchall()


def get_facts_effective_on(conn, entity_id: str, organization_id: str, as_of,
                            fact_type: str | None = None) -> list[dict]:
    """Facts whose stated EFFECTIVE period contains `as_of` (a date) —
    excludes 'superseded' rows. Facts with no stated period are NOT returned
    here (their period is unknown); see knowledge/versions.py."""
    clauses = ["entity_id = %s", "organization_id = %s", "status <> 'superseded'",
               "effective_from IS NOT NULL", "effective_from <= %s",
               "(effective_to IS NULL OR effective_to >= %s)"]
    params = [entity_id, organization_id, as_of, as_of]
    if fact_type:
        clauses.append("fact_type = %s")
        params.append(fact_type)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM kc_facts WHERE {' AND '.join(clauses)} "
            f"ORDER BY fact_type, created_at DESC", params)
        return cur.fetchall()


def close_fact_validity(conn, fact_id: str, organization_id: str, valid_to) -> dict | None:
    """Marks a fact's valid_to — the versioning half of supersede_fact()."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE kc_facts SET valid_to = %s WHERE id = %s AND organization_id = %s RETURNING *",
            (valid_to, fact_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def set_fact_status(conn, fact_id: str, organization_id: str, status: str,
                     *, reviewed_by: str | None = None) -> dict | None:
    """
    reviewed_by is cast explicitly (%s::text) rather than left for Postgres
    to infer: a bare NULL parameter compared with IS NOT NULL/COALESCE gives
    psycopg's server-side prepare nothing to infer a type from
    ("could not determine data type of parameter") when every other use of
    that same parameter is also NULL, which is exactly the automatic
    contradiction-flagging call this function serves (no reviewer — it is
    the system flagging the OTHER side of a disagreement, not a human
    review action).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE kc_facts
            SET status = %s,
                reviewed_by = COALESCE(%s::text, reviewed_by),
                reviewed_at = CASE WHEN %s::text IS NOT NULL THEN now() ELSE reviewed_at END
            WHERE id = %s AND organization_id = %s
            RETURNING *
            """,
            (status, reviewed_by, reviewed_by, fact_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_fact_value(conn, fact_id: str, organization_id: str, *, value: str,
                       normalized_value: float | None, status: str,
                       reviewed_by: str | None = None) -> dict | None:
    """A human correction — same idea as extraction.review_extraction_fact,
    generalised to the Knowledge Core's fact shape."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE kc_facts
            SET value = %s, normalized_value = %s, status = %s,
                reviewed_by = COALESCE(%s, reviewed_by), reviewed_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING *
            """,
            (value, normalized_value, status, reviewed_by, fact_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class NotYoursError(ValueError):
    """
    Raised when a caller-supplied id (a fact, document, or entity) exists
    but does not belong to the organization_id the caller is acting as.

    A ValueError subclass so existing call sites that already catch
    ValueError around a repositories/knowledge.py call (see
    api/knowledge.py's analyze_property_policy) keep working without
    change; callers that want to tell "not yours" apart from "genuinely
    missing" for a different HTTP status can catch this subclass
    specifically.
    """


def create_evidence(conn, *, organization_id: str, fact_id: str, document_id: str,
                     page: int | None, section: str | None, snippet: str | None,
                     source: str | None) -> dict:
    """
    Insert one evidence row, but only after confirming BOTH the fact and the
    document it cites actually belong to organization_id — checked inside
    this function, not just trusted from the API layer above it, so a bug or
    a future caller anywhere else in the codebase cannot create evidence
    that quietly links a fact in one organization to a document in another.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM kc_facts WHERE id = %s AND organization_id = %s",
            (fact_id, organization_id),
        )
        if cur.fetchone() is None:
            raise NotYoursError(
                f"Fact {fact_id} does not belong to organization {organization_id}."
            )

        cur.execute(
            "SELECT 1 FROM documents WHERE id = %s AND organization_id = %s",
            (document_id, organization_id),
        )
        if cur.fetchone() is None:
            raise NotYoursError(
                f"Document {document_id} does not belong to organization {organization_id}."
            )

        cur.execute(
            """
            INSERT INTO kc_evidence (organization_id, fact_id, document_id, page, section, snippet, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (organization_id, fact_id, document_id, page, section, snippet, source),
        )
        return dict(cur.fetchone())


def get_evidence_for_fact(conn, fact_id: str, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.*, d.filename AS document_filename
            FROM kc_evidence e
            JOIN documents d ON d.id = e.document_id
            WHERE e.fact_id = %s AND e.organization_id = %s
            ORDER BY e.created_at ASC
            """,
            (fact_id, organization_id),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def create_relationship(conn, *, organization_id: str, from_entity_id: str,
                         to_entity_id: str, relationship_type: str,
                         attributes: dict | None = None) -> dict:
    """
    Insert one relationship row, but only after confirming BOTH endpoint
    entities actually belong to organization_id — checked inside this
    function for the same reason create_evidence checks its fact/document:
    trusting the API layer alone means the very next caller added anywhere
    else in the codebase could skip the check and wire together entities
    from two different organizations into one edge.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM kc_entities WHERE id = %s AND organization_id = %s",
            (from_entity_id, organization_id),
        )
        if cur.fetchone() is None:
            raise NotYoursError(
                f"Entity {from_entity_id} does not belong to organization {organization_id}."
            )

        cur.execute(
            "SELECT 1 FROM kc_entities WHERE id = %s AND organization_id = %s",
            (to_entity_id, organization_id),
        )
        if cur.fetchone() is None:
            raise NotYoursError(
                f"Entity {to_entity_id} does not belong to organization {organization_id}."
            )

        cur.execute(
            """
            INSERT INTO kc_relationships (organization_id, from_entity_id, to_entity_id,
                                           relationship_type, attributes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (organization_id, from_entity_id, to_entity_id, relationship_type,
             Jsonb(attributes) if attributes is not None else None),
        )
        return dict(cur.fetchone())


def find_relationship(conn, organization_id: str, from_entity_id: str,
                       to_entity_id: str, relationship_type: str) -> dict | None:
    """Get-or-create support, same idea as find_entity_by_name."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM kc_relationships
            WHERE organization_id = %s AND from_entity_id = %s AND to_entity_id = %s
            AND relationship_type = %s
            LIMIT 1
            """,
            (organization_id, from_entity_id, to_entity_id, relationship_type),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def find_relationships(conn, organization_id: str, entity_id: str,
                        relationship_type: str | None = None,
                        direction: str = "from") -> list[dict]:
    """
    Relationships touching one entity, scoped to the caller's organization.

    direction='from' finds relationships where entity_id is the source
    (Business -[owns]-> ?); direction='to' finds ones where it is the target
    (? -[owns]-> Asset); direction='both' returns either.
    """
    column = {"from": "from_entity_id", "to": "to_entity_id"}.get(direction)
    clauses = ["organization_id = %s"]
    params = [organization_id]

    if direction == "both":
        clauses.append("(from_entity_id = %s OR to_entity_id = %s)")
        params.extend([entity_id, entity_id])
    else:
        clauses.append(f"{column} = %s")
        params.append(entity_id)

    if relationship_type:
        clauses.append("relationship_type = %s")
        params.append(relationship_type)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM kc_relationships WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at ASC",
            params,
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Conflicts — see kc_conflicts' schema comment in connection.py for why this
# exists: get_current_fact() alone cannot express "there is no single
# current value because two facts disagree and nobody has resolved it yet".
# ---------------------------------------------------------------------------

def get_unresolved_conflict(conn, entity_id: str, organization_id: str,
                             fact_type: str) -> dict | None:
    """The open (status='unresolved') conflict for this entity+fact_type, if
    any — at most one can exist at a time (kc_conflicts_one_unresolved_idx)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM kc_conflicts
            WHERE entity_id = %s AND organization_id = %s AND fact_type = %s
            AND status = 'unresolved'
            LIMIT 1
            """,
            (entity_id, organization_id, fact_type),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_conflict(conn, conflict_id: str, organization_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM kc_conflicts WHERE id = %s AND organization_id = %s",
            (conflict_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def create_conflict(conn, *, organization_id: str, entity_id: str,
                     fact_type: str, fact_ids: list[str]) -> dict:
    """Open a new unresolved conflict linking every fact_id given (normally
    both sides of the disagreement) via kc_conflict_facts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kc_conflicts (organization_id, entity_id, fact_type, status)
            VALUES (%s, %s, %s, 'unresolved')
            RETURNING *
            """,
            (organization_id, entity_id, fact_type),
        )
        conflict = dict(cur.fetchone())
        for fact_id in fact_ids:
            cur.execute(
                "INSERT INTO kc_conflict_facts (conflict_id, fact_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (str(conflict["id"]), fact_id),
            )
        return conflict


def add_fact_to_conflict(conn, conflict_id: str, fact_id: str) -> None:
    """Link one more disagreeing fact onto an already-open conflict, rather
    than opening a second conflict row for the same entity+fact_type."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kc_conflict_facts (conflict_id, fact_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (conflict_id, fact_id),
        )


def get_conflict_fact_ids(conn, conflict_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fact_id FROM kc_conflict_facts WHERE conflict_id = %s",
            (conflict_id,),
        )
        return [str(row["fact_id"]) for row in cur.fetchall()]


def resolve_conflict(conn, conflict_id: str, organization_id: str, *,
                      resolution_fact_id: str) -> dict | None:
    """Marks a conflict resolved once a human correction (supersede_fact)
    settles which value is authoritative."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE kc_conflicts
            SET status = 'resolved', resolution_fact_id = %s, resolved_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING *
            """,
            (resolution_fact_id, conflict_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None
