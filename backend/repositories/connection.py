"""
Database connection, schema and shared constants for Aurevia BrokerOS.

This module holds what used to live at the top of the monolithic db.py:
the connection helper, the full SCHEMA/MIGRATIONS SQL, init_schema(), and
DEFAULT_ORGANIZATION_ID. Every repositories/*.py module imports from here
rather than opening its own connection or redefining these constants.

Organization scoping: every table above that can hold more than one
organization's data carries an `organization_id` column, and every read/write
function below that touches such a table takes an `organization_id` argument
and includes it in the SQL WHERE clause itself (not "fetch, then check in
Python"), so a request scoped to org B genuinely cannot see or modify org
A's rows — the query returns nothing rather than something to filter out.
This is still pilot-stage, application-level tenant separation: the caller
supplying the organization id is trusted, there is no Postgres row-level
security and no real user authentication behind the header yet. See
Results.md for how this boundary has been tested.
"""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://insurance_user:insurance_pass@postgres:5432/insurance_db"
)

# The pilot-stage default organization id. Defined here, near the top, so
# it is available as a default argument value on functions defined much
# earlier in this file (create_carrier, create_client) than the
# Document Intelligence section below where this constant historically
# lived — a default argument is evaluated at function-definition time, so
# the constant has to exist before that point, not merely before the
# function is first called.
DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"


@contextmanager
def get_connection():
    """
    Yield a connection with dict rows, committing on success.

    dict_row means queries return {"id": ..., "name": ...} instead of tuples,
    so callers index by column name and aren't broken by column reordering.
    """
    with psycopg.connect(DATABASE_URL, connect_timeout=5, row_factory=dict_row) as conn:
        yield conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS carriers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Nullable: a Document Intelligence upload is not filed against any
    -- carrier-briefing carrier at all (see the organization_id migration
    -- below for the historical note on why this predates that change).
    carrier_id      UUID REFERENCES carriers(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    file_type       TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    pages           INTEGER NOT NULL DEFAULT 0,
    chars           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('parsed', 'failed')),
    error_message   TEXT,
    extracted_text  TEXT,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_carrier_id_idx ON documents(carrier_id);

-- Phase 1, summarisation ------------------------------------------------
-- One summary per carrier: the AI's read of everything uploaded for that
-- carrier — company overview, requirements, plans on offer, everything the
-- brief asked for ("everything means everything"). Structured (not just
-- prose) so the UI can render distinct sections rather than one wall of
-- text, and so later phases (matching, gap analysis) can read specific
-- fields — e.g. `plans` — without re-parsing prose.

CREATE TABLE IF NOT EXISTS summaries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    carrier_id        UUID NOT NULL UNIQUE REFERENCES carriers(id) ON DELETE CASCADE,
    -- summary is what the user sees and may edit; summary_original is what
    -- the model produced, kept so an edit can be told apart from a rerun.
    summary           TEXT NOT NULL,
    summary_original  TEXT NOT NULL,
    company_overview  JSONB NOT NULL DEFAULT '{}'::jsonb,
    requirements      JSONB NOT NULL DEFAULT '[]'::jsonb,
    plans             JSONB NOT NULL DEFAULT '[]'::jsonb,
    exclusions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_points        JSONB NOT NULL DEFAULT '[]'::jsonb,
    stats             JSONB NOT NULL DEFAULT '{}'::jsonb,
    model             TEXT,
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at         TIMESTAMPTZ
);

-- Chunks are stored rather than recomputed: a later phase (RAG-based plan
-- matching) embeds these exact pieces into a vector store and cites them,
-- so the text and its provenance have to survive past the summarise call
-- that created them. Same reasoning as the reference project this is
-- adapted from.
CREATE TABLE IF NOT EXISTS chunks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    section_title TEXT,
    page_start    INTEGER,
    text          TEXT NOT NULL,
    summary       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id);

-- Phase 2, clients --------------------------------------------------------
-- The first point where a real business's details enter the system. Kept in
-- their own tables, deliberately not bolted onto `carriers`: the two have
-- different sensitivity and different lifecycles, and mixing them would make
-- "delete everything about this client" a query nobody could write with
-- confidence.
--
-- Exact values live HERE and only here. Everything downstream — the
-- embedding call, every prompt — sees the redacted copy instead. See
-- tokenise.py for where that boundary is drawn.

CREATE TABLE IF NOT EXISTS clients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name   TEXT NOT NULL,
    entity_type     TEXT,
    industry        TEXT,
    city            TEXT,
    state           TEXT,
    turnover        TEXT,
    years_trading   TEXT,
    employee_count  TEXT,
    -- Identifiers. Stored exactly, redacted before any model sees them.
    pan             TEXT,
    gstin           TEXT,
    contact_name    TEXT,
    contact_phone   TEXT,
    contact_email   TEXT,
    -- What they are asking for.
    categories      JSONB NOT NULL DEFAULT '[]'::jsonb,
    sum_insured     TEXT,
    -- Free text: what the broker learned on the call.
    risk_notes      TEXT,
    -- Paperwork the client has, and what they have said they do not have.
    -- Most carrier requirements are about documents rather than facts, so
    -- gap analysis cannot work from the structured fields alone.
    documents_held    JSONB NOT NULL DEFAULT '[]'::jsonb,
    documents_missing JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The redacted copy of everything above, assembled once at write time so
    -- every downstream reader uses the same text and cannot accidentally
    -- build its own from the raw columns.
    redacted_profile TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- placeholder -> real value, per client. Never sent anywhere; applied only
-- on the way out, after a model has finished writing.
CREATE TABLE IF NOT EXISTS client_tokens (
    client_id   UUID PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
    mapping     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One match run. Kept as history rather than overwritten: a broker needs to
-- be able to say which recommendation was made when, and re-running after
-- adding a carrier should not erase what was advised last week.
CREATE TABLE IF NOT EXISTS matches (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id    UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    results      JSONB NOT NULL DEFAULT '[]'::jsonb,
    sources      JSONB NOT NULL DEFAULT '[]'::jsonb,
    model        TEXT,
    stats        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS matches_client_id_idx ON matches(client_id, created_at DESC);

-- Phase 3, gap analysis --------------------------------------------------
-- One run per client per carrier: what that carrier requires, checked
-- against what is held. History rather than current state, for the same
-- reason matches are: a broker needs to be able to say what was outstanding
-- when they advised, and re-running after chasing a document should not
-- erase the earlier picture.

CREATE TABLE IF NOT EXISTS gap_reports (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    carrier_id  UUID NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
    items       JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary     TEXT,
    stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
    model       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gap_reports_client_idx
    ON gap_reports(client_id, carrier_id, created_at DESC);

-- Phase 3.5, documents the client actually sent ---------------------------
-- Attached against the requirement they are meant to satisfy, so a checklist
-- row can move from "client says they have it" to "we have read it and it is
-- the right thing".
--
-- Keyed by requirement TEXT rather than an id: requirement lists are
-- regenerated whenever a briefing is re-run, and an id would not survive
-- that. The text is stable enough to re-match against, and a requirement
-- whose wording changes materially deserves a fresh look anyway.

CREATE TABLE IF NOT EXISTS client_documents (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    requirement      TEXT NOT NULL,
    filename         TEXT NOT NULL,
    file_type        TEXT,
    size_bytes       BIGINT NOT NULL DEFAULT 0,
    chars            INTEGER NOT NULL DEFAULT 0,
    extracted_text   TEXT,
    verdict          TEXT NOT NULL DEFAULT 'unchecked',
    doc_type         TEXT,
    reason           TEXT,
    score            INTEGER,
    score_parts      JSONB NOT NULL DEFAULT '{}'::jsonb,
    improvements     JSONB NOT NULL DEFAULT '[]'::jsonb,
    identifiers      JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes            JSONB NOT NULL DEFAULT '[]'::jsonb,
    key_facts        JSONB NOT NULL DEFAULT '[]'::jsonb,
    uploaded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS client_documents_client_idx
    ON client_documents(client_id, uploaded_at DESC);

-- Phase 4, drafted paperwork ----------------------------------------------
-- Documents the tool writes for the broker, rather than reads from someone
-- else. Every draft starts and stays a DRAFT: a licensed human is
-- accountable for what reaches an insurer, so nothing here sends anything,
-- and `status` exists so that "written" and "signed off" can never be
-- confused in the UI.
--
-- body is stored with real values restored. body_redacted keeps what the
-- model actually produced, so an edit can be told from a generation and the
-- restoration step is auditable rather than invisible.

CREATE TABLE IF NOT EXISTS drafts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id      UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    carrier_id     UUID REFERENCES carriers(id) ON DELETE SET NULL,
    kind           TEXT NOT NULL DEFAULT 'submission',
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    body_redacted  TEXT,
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'edited', 'approved')),
    warnings       JSONB NOT NULL DEFAULT '[]'::jsonb,
    stats          JSONB NOT NULL DEFAULT '{}'::jsonb,
    model          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS drafts_client_idx
    ON drafts(client_id, created_at DESC);

-- Phase 4 part 2, the audit trail -----------------------------------------
-- Append-only. Every state change on a draft is recorded here and nothing
-- ever updates or deletes a row, because the point of an audit trail is that
-- it cannot be tidied up afterwards.
--
-- `actor` is a name typed by whoever is signing off. There is no user system
-- in this tool, and inventing one would be worse than being honest that the
-- name is self-declared — it records who SAID they approved it, which is
-- still the thing a broker needs months later.

CREATE TABLE IF NOT EXISTS draft_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id    UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    event       TEXT NOT NULL
                CHECK (event IN ('created', 'edited', 'approved', 'revoked',
                                 'exported')),
    actor       TEXT,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS draft_events_draft_idx
    ON draft_events(draft_id, created_at ASC);

-- Phase 5, the feedback loop ----------------------------------------------
-- Every time a person corrects the tool, the correction is kept. Not to
-- train anything — nothing here fine-tunes a model — but because the
-- corrections ARE the signal: which requirements keep getting overridden,
-- how heavily drafts get rewritten, which carriers produce thin briefings.
--
-- A tool that is wrong in the same way every week and never notices is the
-- failure this table exists to prevent.

CREATE TABLE IF NOT EXISTS corrections (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT NOT NULL CHECK (kind IN ('draft_edit', 'gap_override')),
    client_id   UUID REFERENCES clients(id) ON DELETE CASCADE,
    carrier_id  UUID REFERENCES carriers(id) ON DELETE SET NULL,
    subject     TEXT,
    before_text TEXT,
    after_text  TEXT,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS corrections_kind_idx
    ON corrections(kind, created_at DESC);

-- Guidance distilled from those corrections, per document kind. Regenerated
-- on request rather than continuously, and editable by hand — a broker who
-- disagrees with what the tool concluded should be able to just fix it,
-- which is faster and more honest than waiting for enough data to outvote it.

-- organization_id (and the (kind, organization_id) primary key it becomes
-- part of) is added by MIGRATIONS below, not here: organizations does not
-- exist yet at this point in SCHEMA, so a direct foreign-key reference
-- here would fail on a brand-new database. Same reasoning as
-- carriers/clients, which get organization_id only via MIGRATIONS too.
CREATE TABLE IF NOT EXISTS house_style (
    kind          TEXT PRIMARY KEY,
    guidance      TEXT NOT NULL DEFAULT '',
    derived_from  INTEGER NOT NULL DEFAULT 0,
    edited_by_hand BOOLEAN NOT NULL DEFAULT false,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ==========================================================================
-- Document Intelligence vertical slice
--
-- One end-to-end path, deliberately narrow: upload -> quality check ->
-- text/OCR -> one extracted fact -> evidence + typed confidence -> human
-- review/correction -> append-only audit event. Nothing below is meant to
-- generalise yet — the point of a vertical slice is to find out whether
-- these four shapes (organization, fact, evidence+confidence, event) are
-- right BEFORE building the other extraction paths on top of them.
-- ==========================================================================

-- Pilot-stage tenant boundary: application-level filtering on
-- organization_id, not database-enforced row-level security. Every relevant
-- table gets the column below; every query in this vertical slice is scoped
-- by it. This is NOT production-grade tenant isolation — a bug in
-- application code could still cross the boundary, which real RLS would
-- prevent. Explicitly acceptable for a 2-3 broker pilot; revisit before
-- real customer data at scale, per the pilot-stage decision on file.
CREATE TABLE IF NOT EXISTS organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One structured, evidence-backed fact extracted from one document.
--
-- This is the object the whole slice exists to validate: whether "a fact"
-- can be represented once, uniformly, whether it came from a deterministic
-- check, an OCR read, or a model's own judgement. If this shape is wrong,
-- it is wrong here in one place — not repeated wrong across ten extraction
-- paths built on a bad assumption.
--
-- Confidence is deliberately NOT a single number. 92% from a checksum and
-- 92% from a model's self-report are different claims, and collapsing them
-- into one field would let a UI display them as though they meant the same
-- thing. confidence_type says which kind of claim value actually is:
--   'deterministic'      — a rule either passed or it did not (value is 1.0 or 0.0)
--   'ocr_engine'          — the OCR engine's own per-token/region confidence
--   'model_self_reported' — an LLM's stated plausibility, never a calibrated probability
CREATE TABLE IF NOT EXISTS extraction_facts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    -- What was extracted, kept generic on purpose: this slice extracts one
    -- fact type (coverage/sum insured limit) to prove the shape works, not
    -- to build a taxonomy. A fixed enum here would be exactly the kind of
    -- premature structure the pilot-gate decision says to avoid.
    fact_type         TEXT NOT NULL,
    label             TEXT NOT NULL,
    value             TEXT NOT NULL,

    -- Evidence: where this value came from, so a reviewer can check the
    -- source page rather than take the extraction on faith.
    evidence_page      INTEGER,
    evidence_section    TEXT,
    evidence_snippet    TEXT,
    evidence_source     TEXT NOT NULL DEFAULT 'text_layer'
                         CHECK (evidence_source IN ('text_layer', 'ocr')),

    -- Typed confidence — see the note above. method names the specific
    -- engine/rule/model version, so accuracy can be tracked per-method over
    -- time once there is enough data to compare.
    confidence_value    REAL NOT NULL,
    confidence_type     TEXT NOT NULL
                         CHECK (confidence_type IN
                                ('deterministic', 'ocr_engine', 'model_self_reported')),
    confidence_method   TEXT NOT NULL,
    confidence_note     TEXT,

    -- The crucial rule from the pilot-stage plan: if the system doesn't
    -- know, it says so, rather than guessing. needs_review is set
    -- automatically when confidence falls below a threshold (see
    -- doc_intelligence.py) or extraction could not settle on a value at all.
    status              TEXT NOT NULL DEFAULT 'needs_review'
                         CHECK (status IN ('needs_review', 'verified', 'corrected')),

    -- The value as first extracted, kept even after a correction — a
    -- reviewer's correction should be comparable against what the model
    -- actually said, not overwrite the evidence that it said it.
    original_value      TEXT,

    -- Which model/rule produced this, and its version, so accuracy is
    -- trackable per version once an evaluation set exists (Phase 3).
    produced_by         TEXT NOT NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS extraction_facts_document_idx
    ON extraction_facts(document_id);
CREATE INDEX IF NOT EXISTS extraction_facts_org_idx
    ON extraction_facts(organization_id, status);

-- Append-only. Every verify/correct action on a fact is recorded here and
-- nothing is ever updated or deleted — matches the same append-only
-- principle as draft_events, for the same reason: an audit trail that can
-- be edited is not an audit trail.
CREATE TABLE IF NOT EXISTS extraction_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id     UUID NOT NULL REFERENCES extraction_facts(id) ON DELETE CASCADE,
    event       TEXT NOT NULL CHECK (event IN ('verified', 'corrected', 'flagged')),
    -- Self-declared, same honesty as draft_events.actor: there is no auth
    -- system yet, so this records who SAID they reviewed it.
    reviewer    TEXT NOT NULL,
    before_value TEXT,
    after_value  TEXT,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS extraction_events_fact_idx
    ON extraction_events(fact_id, created_at ASC);

-- Document-level OCR/quality outcome. One row per document, showing whether
-- OCR ran at all and what the page-level picture looked like — separate
-- from extraction_facts because "was this document readable" is a property
-- of the document, not of any one fact pulled from it.
CREATE TABLE IF NOT EXISTS document_quality (
    document_id     UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    used_ocr        BOOLEAN NOT NULL DEFAULT false,
    pages_total     INTEGER NOT NULL DEFAULT 0,
    pages_ocr_low_confidence INTEGER NOT NULL DEFAULT 0,
    avg_ocr_confidence REAL,
    -- Per-page OCR confidence, 1-indexed as a JSON object {"1": 92.3, "2":
    -- 41.0, ...} (page N -> that page's own OCR confidence). NULL when OCR
    -- never ran on this document. Added so a fact's evidence page can be
    -- scored by its OWN page's confidence rather than the document-wide
    -- average — see ocr.quality_check_and_ocr / property_policy_intelligence.
    page_confidences JSONB,
    notes           JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A submission: the grouping the vertical slice was missing before
-- contradiction detection could exist at all. Every document uploaded for
-- doc-intelligence belongs to exactly one submission; comparing facts
-- ACROSS documents (a policy vs. its own coverage schedule) requires
-- knowing they belong together in the first place, which nothing before
-- this table could express — each upload was previously its own unrelated
-- "carrier" with no link to any other document.
CREATE TABLE IF NOT EXISTS submissions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- Free text the uploader supplies (a company name, a reference) — not a
    -- foreign key to `clients`, since a doc-intelligence submission is not
    -- necessarily tied to a client record in the main app yet. Deliberately
    -- loose for the same reason the rest of this vertical slice is loose:
    -- this proves the shape works before it is wired into the main flow.
    label             TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS submissions_org_idx ON submissions(organization_id);

-- ==========================================================================
-- Knowledge Core (Phase 1)
--
-- Per two outside audits' shared recommendation, the shape below:
--
--   Document -> Evidence -> Insurance Facts -> Entities -> Relationships
--     -> Policy/Coverage Knowledge -> Reasoning -> Broker Workflow
--
-- This does NOT replace extraction_facts/extraction_events above — those
-- stay exactly as they are, still serving the Document Intelligence
-- vertical slice unchanged. The Knowledge Core is a separate, additive set
-- of tables that generalises the same evidence+confidence+review shape
-- extraction_facts proved out, so it can hold more than one fact type,
-- attach facts to a real entity (a business, a policy, an asset) rather
-- than just a document, and express relationships between entities. The
-- "migration" from one to the other is conceptual, not a data migration in
-- this pass: nothing in extraction_facts is read, copied, or deleted here.
--
-- Organization scoping follows the same non-negotiable rule as every table
-- above: organization_id on every table, filtered INSIDE the SQL query on
-- every read and write, from the first line of DDL — not added after the
-- fact the way the pre-refactor carrier/client/match/gap/draft code
-- originally was (see the "Foundation refactor" section of Results.md for
-- exactly what that cost to fix later).
-- ==========================================================================

-- A domain entity within an organization: a business, a carrier, a policy,
-- a document, a location, an asset, a coverage, a claim, a risk, or a
-- person. Deliberately NOT the same concept as `organizations` above —
-- organizations is the tenant boundary itself; kc_entities holds the things
-- an organization's brokers reason about (their clients' businesses,
-- properties, policies), scoped to that organization, not tenants of it.
-- Kept as a small, fixed CHECK list on purpose, per the explicit instruction
-- not to expand it casually — a genuinely new kind of thing to reason about
-- is a deliberate schema change, not a value nobody agreed to add.
CREATE TABLE IF NOT EXISTS kc_entities (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_type       TEXT NOT NULL CHECK (entity_type IN (
                          'business', 'carrier', 'policy', 'document',
                          'location', 'asset', 'coverage', 'claim', 'risk',
                          'person', 'business_unit', 'operation', 'exposure',
                          'risk_factor', 'control'
                      )),
    name              TEXT NOT NULL,
    -- Type-specific fields (a business's industry, an asset's address) live
    -- here rather than as columns — the entity list is fixed, but what each
    -- type needs to record is not, and a jsonb bag avoids a schema change
    -- every time a new attribute turns out to matter for one entity type.
    attributes        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kc_entities_org_type_idx
    ON kc_entities(organization_id, entity_type);

-- One structured, versioned, evidence-backed fact about an entity.
--
-- fact_type is a namespaced free-text string ("property.sum_insured",
-- "policy.coverage_limit"), not a fixed enum like entity_type above —
-- new fact types are expected to be added often as the Knowledge Core
-- grows, and a CHECK constraint here would need a migration for every one.
--
-- Versioning is valid_from/valid_to, not a separate version table: the
-- "current" value for an entity+fact_type is the row with valid_to IS
-- NULL; superseding a fact closes the old row's valid_to and opens a new
-- row, so the full history is just every row for that entity+fact_type
-- ordered by valid_from. See knowledge/versions.py.
--
-- confidence_* mirrors extraction_facts exactly on purpose (same
-- confidence_type values, same meaning) — this is the same typed-confidence
-- decision, generalised to more fact types, not a new one.
CREATE TABLE IF NOT EXISTS kc_facts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_id           UUID NOT NULL REFERENCES kc_entities(id) ON DELETE CASCADE,

    fact_type           TEXT NOT NULL,
    value                TEXT NOT NULL,
    -- Populated only when the fact is a comparable numeric quantity (a sum
    -- insured, a deductible) — lets reconciliation compare numbers without
    -- re-parsing `value` every time. NULL for a fact that is inherently
    -- text (an occupancy description, an exclusion clause).
    normalized_value     NUMERIC,
    unit                 TEXT,

    confidence_value     NUMERIC NOT NULL,
    confidence_type      TEXT NOT NULL
                          CHECK (confidence_type IN
                                 ('deterministic', 'ocr_engine', 'model_self_reported')),
    confidence_method    TEXT,
    confidence_note      TEXT,

    -- Same lifecycle as extraction_facts.status, plus the same
    -- 'contradiction_flagged' state for the same reason: a disagreement
    -- between two documents about the same entity+fact_type is a more
    -- specific and more urgent reason for review than the generic
    -- 'needs_review' state. 'superseded' is distinct from 'corrected':
    -- 'corrected' is the NEW row a human correction produced (the winning
    -- side); 'superseded' is what a LOSING side of a resolved conflict
    -- becomes once correct_fact() closes it out (see kc_conflicts below) —
    -- it is no longer 'contradiction_flagged' because the conflict is
    -- resolved, but it was never itself corrected either.
    status               TEXT NOT NULL DEFAULT 'needs_review'
                          CHECK (status IN
                                 ('needs_review', 'verified', 'corrected',
                                  'contradiction_flagged', 'superseded')),

    -- Versioning: this IS the current value for entity_id+fact_type when
    -- valid_to IS NULL. supersede_fact() closes valid_to on the old row and
    -- inserts a new one with valid_from = now().
    valid_from           TIMESTAMPTZ,
    valid_to             TIMESTAMPTZ,

    -- EFFECTIVE period: when the fact is true in the insured's world (the
    -- policy period a limit applies to), as stated by the source. Distinct
    -- from valid_from/valid_to above, which record when AURE learned/replaced
    -- a value. NULL = the source stated no period (UNKNOWN, never guessed).
    effective_from       DATE,
    effective_to         DATE,

    produced_by          TEXT NOT NULL,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at          TIMESTAMPTZ,
    reviewed_by          TEXT
);

CREATE INDEX IF NOT EXISTS kc_facts_org_entity_idx
    ON kc_facts(organization_id, entity_id, fact_type);
-- Finds "the current fact" (valid_to IS NULL) for an entity+fact_type
-- without a full table scan — the lookup versions.get_current_value() and
-- service.reconcile_fact() both run on every single fact write.
CREATE INDEX IF NOT EXISTS kc_facts_current_idx
    ON kc_facts(entity_id, fact_type) WHERE valid_to IS NULL;

-- Evidence: where one fact's value came from — same reasoning as
-- extraction_facts.evidence_* columns, pulled into its own table here
-- because a fact can (once contradictions are resolved, or once a fact is
-- corroborated by more than one document) end up with more than one piece
-- of evidence, where extraction_facts only ever needed exactly one.
CREATE TABLE IF NOT EXISTS kc_evidence (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    fact_id           UUID NOT NULL REFERENCES kc_facts(id) ON DELETE CASCADE,
    document_id       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page              INTEGER,
    section           TEXT,
    snippet           TEXT,
    source            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kc_evidence_fact_idx ON kc_evidence(fact_id);
CREATE INDEX IF NOT EXISTS kc_evidence_org_idx ON kc_evidence(organization_id);

-- A directed relationship between two entities in the same organization
-- (Business -[owns]-> Asset, Business -[has_policy]-> Policy, Policy
-- -[provides]-> Coverage, ...). relationship_type is free text for the same
-- reason kc_facts.fact_type is: new relationship kinds are expected as the
-- Knowledge Core grows.
CREATE TABLE IF NOT EXISTS kc_relationships (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    from_entity_id       UUID NOT NULL REFERENCES kc_entities(id) ON DELETE CASCADE,
    to_entity_id         UUID NOT NULL REFERENCES kc_entities(id) ON DELETE CASCADE,
    relationship_type    TEXT NOT NULL,
    attributes           JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kc_relationships_from_idx
    ON kc_relationships(organization_id, from_entity_id, relationship_type);
CREATE INDEX IF NOT EXISTS kc_relationships_to_idx
    ON kc_relationships(organization_id, to_entity_id, relationship_type);

-- An unresolved disagreement between two (or more) kc_facts rows for the
-- SAME entity+fact_type. Without this, get_current_fact()'s
-- "ORDER BY created_at DESC LIMIT 1" silently treated the newer of two
-- contradicting facts as authoritative even though BOTH were flagged
-- 'contradiction_flagged' — a conflict row is what lets a reader tell
-- "no current value because nobody has reconciled this yet" apart from
-- "one clear current value", instead of the read path guessing.
--
-- One row per entity+fact_type conflict; kc_conflict_facts is the join
-- table linking every fact_id that is part of that disagreement (there can
-- be more than two if a third document adds a third disagreeing value
-- before anyone reviews the first two). resolution_fact_id is filled in
-- when a human correction (facts.supersede_fact) settles which value is
-- right — see knowledge/service.py's resolve path.
CREATE TABLE IF NOT EXISTS kc_conflicts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    entity_id           UUID NOT NULL REFERENCES kc_entities(id) ON DELETE CASCADE,
    fact_type           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'unresolved'
                         CHECK (status IN ('unresolved', 'resolved')),
    resolution_fact_id  UUID REFERENCES kc_facts(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS kc_conflicts_org_entity_idx
    ON kc_conflicts(organization_id, entity_id, fact_type);
-- One unresolved conflict per entity+fact_type at a time — reconcile_fact()
-- adds new facts to the existing unresolved conflict row rather than
-- opening a second one for the same disagreement.
CREATE UNIQUE INDEX IF NOT EXISTS kc_conflicts_one_unresolved_idx
    ON kc_conflicts(entity_id, fact_type) WHERE status = 'unresolved';

CREATE TABLE IF NOT EXISTS kc_conflict_facts (
    conflict_id         UUID NOT NULL REFERENCES kc_conflicts(id) ON DELETE CASCADE,
    fact_id             UUID NOT NULL REFERENCES kc_facts(id) ON DELETE CASCADE,
    PRIMARY KEY (conflict_id, fact_id)
);

CREATE INDEX IF NOT EXISTS kc_conflict_facts_fact_idx ON kc_conflict_facts(fact_id);
"""


# Columns added to tables that already existed in an earlier version.
#
# CREATE TABLE IF NOT EXISTS does nothing at all when the table is already
# there, so a column added to SCHEMA later never reaches a database created
# before it — a new table appears, a new column silently does not. These run
# on every startup and are no-ops once applied.
#
# Anything added to an existing table from now on needs a line here as well
# as in SCHEMA above.
MIGRATIONS = """
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS documents_held JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS documents_missing JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS exclusions JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 'exported' was added to the allowed events after draft_events shipped, so
-- the original CHECK has to be replaced rather than extended in place.
-- Dropped by name first, which makes this safe to run on every startup.
ALTER TABLE draft_events DROP CONSTRAINT IF EXISTS draft_events_event_check;
ALTER TABLE draft_events ADD CONSTRAINT draft_events_event_check
    CHECK (event IN ('created', 'edited', 'approved', 'revoked', 'exported'));

-- Pilot-stage tenant boundary, added to tables that predate it. A single
-- default organization is created and backfilled onto every existing row,
-- so this migration never breaks data from before the boundary existed.
INSERT INTO organizations (id, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default organization')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE carriers
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE carriers SET organization_id = '00000000-0000-0000-0000-000000000001'
    WHERE organization_id IS NULL;
ALTER TABLE carriers ALTER COLUMN organization_id SET NOT NULL;

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE clients SET organization_id = '00000000-0000-0000-0000-000000000001'
    WHERE organization_id IS NULL;
ALTER TABLE clients ALTER COLUMN organization_id SET NOT NULL;

-- Contradiction detection needs two things existing rows predate:
-- (1) a link from a document to the submission it belongs to, so documents
--     can be grouped by company/case at all;
-- (2) fields on a fact recording that it was found to disagree with another
--     fact of the same type in the same submission, and what that other
--     fact said.
--
-- Nullable throughout: existing documents/facts from before this migration
-- have no submission and are simply never compared — they are not backfilled
-- into a submission, because there is no reliable way to know which
-- historical uploads belonged together after the fact. New uploads through
-- the updated endpoint always get a submission_id.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS submission_id UUID
    REFERENCES submissions(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS documents_submission_idx ON documents(submission_id);

ALTER TABLE extraction_facts
    ADD COLUMN IF NOT EXISTS contradicts_fact_id UUID
    REFERENCES extraction_facts(id) ON DELETE SET NULL;
ALTER TABLE extraction_facts
    ADD COLUMN IF NOT EXISTS contradiction_note TEXT;

-- 'contradiction_flagged' distinguishes "nobody has reviewed this yet" from
-- "two documents disagree and a human must decide which is right" — the
-- original CHECK predates this status and has to be replaced rather than
-- extended in place, same pattern as draft_events_event_check above.
ALTER TABLE extraction_facts DROP CONSTRAINT IF EXISTS extraction_facts_status_check;
ALTER TABLE extraction_facts ADD CONSTRAINT extraction_facts_status_check
    CHECK (status IN ('needs_review', 'verified', 'corrected', 'contradiction_flagged'));

-- Bringing the carrier-briefing / client-intake / gap-analysis / drafting
-- tables up to the same organization_id standard doc-intelligence already
-- has. Same backfill pattern as carriers/clients above: every existing row
-- is assigned to the default organization so nothing pre-existing loses
-- access to its own data.
--
-- documents gets organization_id directly, rather than only inheriting it
-- via carrier_id, because carrier_id is being made nullable below (a
-- doc-intelligence upload has no carrier) — a document with no carrier
-- still needs to be scopable to an organization on its own.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE documents d SET organization_id = COALESCE(
    (SELECT c.organization_id FROM carriers c WHERE c.id = d.carrier_id),
    '00000000-0000-0000-0000-000000000001'
) WHERE d.organization_id IS NULL;
ALTER TABLE documents ALTER COLUMN organization_id SET NOT NULL;

-- carrier_id becomes optional: a doc-intelligence upload is not filed
-- against any carrier-briefing carrier at all, and previously had to
-- manufacture a fake one just to satisfy this NOT NULL constraint.
ALTER TABLE documents ALTER COLUMN carrier_id DROP NOT NULL;

ALTER TABLE gap_reports
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE gap_reports g SET organization_id = COALESCE(
    (SELECT c.organization_id FROM clients c WHERE c.id = g.client_id),
    '00000000-0000-0000-0000-000000000001'
) WHERE g.organization_id IS NULL;
ALTER TABLE gap_reports ALTER COLUMN organization_id SET NOT NULL;

ALTER TABLE drafts
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE drafts d SET organization_id = COALESCE(
    (SELECT c.organization_id FROM clients c WHERE c.id = d.client_id),
    '00000000-0000-0000-0000-000000000001'
) WHERE d.organization_id IS NULL;
ALTER TABLE drafts ALTER COLUMN organization_id SET NOT NULL;

ALTER TABLE client_documents
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE client_documents cd SET organization_id = COALESCE(
    (SELECT c.organization_id FROM clients c WHERE c.id = cd.client_id),
    '00000000-0000-0000-0000-000000000001'
) WHERE cd.organization_id IS NULL;
ALTER TABLE client_documents ALTER COLUMN organization_id SET NOT NULL;

-- corrections: client_id/carrier_id are nullable on the base table, so
-- organization_id cannot always be backfilled from either — fall back to
-- the default organization for a correction that (unusually) names
-- neither, same principle as every other backfill above.
ALTER TABLE corrections
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE corrections co SET organization_id = COALESCE(
    (SELECT cl.organization_id FROM clients cl WHERE cl.id = co.client_id),
    (SELECT ca.organization_id FROM carriers ca WHERE ca.id = co.carrier_id),
    '00000000-0000-0000-0000-000000000001'
) WHERE co.organization_id IS NULL;
ALTER TABLE corrections ALTER COLUMN organization_id SET NOT NULL;

-- house_style: was one global row per document kind, shared across every
-- organization — which is exactly the leak this migration closes: two
-- organizations' draft edits fed a single shared "learned" guidance. The
-- primary key widens from (kind) to (kind, organization_id) so each
-- organization gets its own guidance row per kind, seeded from whatever
-- existed before (attributed to the default organization, since a
-- pre-existing row cannot be retroactively split between organizations
-- that did not exist as separate tenants when it was written).
ALTER TABLE house_style
    ADD COLUMN IF NOT EXISTS organization_id UUID
    REFERENCES organizations(id) ON DELETE CASCADE;
UPDATE house_style SET organization_id = '00000000-0000-0000-0000-000000000001'
    WHERE organization_id IS NULL;
ALTER TABLE house_style ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE house_style DROP CONSTRAINT IF EXISTS house_style_pkey;
ALTER TABLE house_style ADD PRIMARY KEY (kind, organization_id);

-- Per-page OCR confidence, added to an existing document_quality table so a
-- fact's evidence page can be scored by its own page rather than the
-- document-wide average (see the column's comment in SCHEMA above).
-- Nullable and never backfilled: a document quality-checked before this
-- migration has no way to recover which page had which confidence after
-- the fact, so it simply has no per-page data — avg_ocr_confidence (already
-- stored) remains the only figure available for it.
ALTER TABLE document_quality
    ADD COLUMN IF NOT EXISTS page_confidences JSONB;

-- 'superseded' added to kc_facts.status: when correct_fact() resolves a
-- kc_conflicts row, every OTHER fact that was part of that conflict (the
-- losing side(s), not the one just corrected) must be closed out too —
-- valid_to set and status moved off 'contradiction_flagged' — so exactly
-- one fact row is left with valid_to IS NULL per entity+fact_type once a
-- conflict is resolved, rather than that holding only "by coincidence" of
-- which row is newest. Same replace-the-CHECK-by-name pattern as
-- extraction_facts_status_check/draft_events_event_check above, since the
-- original CHECK predates this status.
-- v0.8.0: effective period of a fact (see kc_facts above). Nullable, so every
-- existing row stays valid and keeps its meaning (period unknown).
ALTER TABLE kc_facts ADD COLUMN IF NOT EXISTS effective_from DATE;
ALTER TABLE kc_facts ADD COLUMN IF NOT EXISTS effective_to DATE;
ALTER TABLE kc_facts DROP CONSTRAINT IF EXISTS kc_facts_status_check;
ALTER TABLE kc_facts ADD CONSTRAINT kc_facts_status_check
    CHECK (status IN ('needs_review', 'verified', 'corrected',
                       'contradiction_flagged', 'superseded'));
-- v0.12.0 (Phase 1D): business-risk entity types. Same replace-the-CHECK-by-
-- name pattern; every existing row stays valid.
ALTER TABLE kc_entities DROP CONSTRAINT IF EXISTS kc_entities_entity_type_check;
ALTER TABLE kc_entities ADD CONSTRAINT kc_entities_entity_type_check
    CHECK (entity_type IN ('business', 'carrier', 'policy', 'document', 'location', 'asset',
                           'coverage', 'claim', 'risk', 'person',
                           'business_unit', 'operation', 'exposure', 'risk_factor', 'control'));
-- v0.13.0 (Phase 1E): broker review decisions on coverage mappings and
-- coverage findings. Mappings and findings themselves are computed on read
-- from the Knowledge Core (never stored, so they can never drift from the
-- facts); only the human decisions are stored, append-only. subject_id is
-- the deterministic id of the mapping/finding; fingerprint is a hash of the
-- values the reviewer saw, so a review never silently carries over to a
-- finding whose evidence has since changed.
CREATE TABLE IF NOT EXISTS kc_coverage_reviews (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    business_id      UUID NOT NULL REFERENCES kc_entities(id) ON DELETE CASCADE,
    subject_type     TEXT NOT NULL CHECK (subject_type IN ('mapping', 'finding')),
    subject_id       UUID NOT NULL,
    fingerprint      TEXT NOT NULL,
    decision         TEXT NOT NULL CHECK (decision IN ('OPEN', 'REVIEWED', 'ACCEPTED', 'REJECTED',
                                                       'NEEDS_MORE_EVIDENCE')),
    reviewer         TEXT NOT NULL,
    comment          TEXT,
    evidence         JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kc_coverage_reviews_org_business_idx
    ON kc_coverage_reviews (organization_id, business_id, created_at);
"""


def init_schema() -> None:
    """
    Create tables and apply column migrations. Safe on every startup.

    Migrations run after SCHEMA so that a fresh database gets its tables
    first and the ALTERs are simply no-ops against them.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute(MIGRATIONS)
