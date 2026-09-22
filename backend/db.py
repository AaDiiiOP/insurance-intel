"""
Database access for Phase 1.

Raw SQL via psycopg rather than an ORM — same call made in the reference
RFP-Intelligence project this is adapted from. The schema is small and the
queries are simple; an ORM adds a layer to debug alongside everything else.

Phase 1 has two tables:
  carriers    — one per insurer/broker-partner document set the team uploads
  documents   — the individual files (policy wordings, brochures, rate
                cards) belonging to one carrier

This project's documents are ALWAYS the insurer/broker's own paperwork —
policy wordings, eligibility rules, premium tables — never a real customer's
personal data. Customer intake is a later phase (Phase 2+) and, per the
project's data-handling plan, will live in its own table with a separate
tokenisation layer before anything customer-specific touches this database.
Keeping that boundary in mind now is why `carriers`/`documents` below has no
customer-shaped columns at all — adding them later should mean new tables,
not fields bolted onto these.
"""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://insurance_user:insurance_pass@postgres:5432/insurance_db"
)


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
    carrier_id      UUID NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
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


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_carrier(conn, name: str) -> str:
    """
    Insert a carrier and return its generated id.

    Defaults to the pilot-stage default organization when no caller passes
    one — every existing call site (the carrier-briefing flow) predates the
    organization_id column and is untouched here, so it keeps working
    exactly as before rather than being required to change alongside a
    schema addition it has no reason to know about.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO carriers (name, organization_id) VALUES (%s, %s) RETURNING id",
            (name, DEFAULT_ORGANIZATION_ID),
        )
        return str(cur.fetchone()["id"])


def create_document(
    conn,
    *,
    carrier_id: str,
    filename: str,
    file_type: str,
    size_bytes: int,
    pages: int,
    chars: int,
    status: str,
    error_message: str | None,
    extracted_text: str | None,
) -> dict:
    """
    Insert one document row and return the fields the API responds with.

    Keyword-only (the bare * above) because this has several parameters of
    similar type — positional calls would be easy to get subtly wrong.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (
                carrier_id, filename, file_type, size_bytes,
                pages, chars, status, error_message, extracted_text
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, filename, status, pages, chars, error_message
            """,
            (
                carrier_id,
                filename,
                file_type,
                size_bytes,
                pages,
                chars,
                status,
                error_message,
                extracted_text,
            ),
        )
        return dict(cur.fetchone())


def list_carriers(conn) -> list[dict]:
    """
    Every carrier, newest first, with a document count and whether it has
    a summary yet — enough for a dashboard list without a second query per
    row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.id, c.name, c.created_at,
                COUNT(d.id) AS document_count,
                COUNT(d.id) FILTER (WHERE d.status = 'failed') AS failed_count,
                (s.id IS NOT NULL) AS has_summary
            FROM carriers c
            LEFT JOIN documents d ON d.carrier_id = c.id
            LEFT JOIN summaries s ON s.carrier_id = c.id
            GROUP BY c.id, c.name, c.created_at, s.id
            ORDER BY c.created_at DESC
            """
        )
        return cur.fetchall()


def get_carrier(conn, carrier_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, created_at FROM carriers WHERE id = %s", (carrier_id,))
        return cur.fetchone()


def list_documents_for_carrier(conn, carrier_id: str) -> list[dict]:
    """
    Lighter query for a list view: excludes extracted_text, which can be
    large and isn't needed until something specifically reads a document's
    full text.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, carrier_id, filename, file_type, size_bytes, pages,
                   chars, status, error_message, uploaded_at
            FROM documents
            WHERE carrier_id = %s
            ORDER BY uploaded_at ASC
            """,
            (carrier_id,),
        )
        return cur.fetchall()


def get_document_with_text(conn, document_id: str) -> dict | None:
    """Full row, including extracted_text — for the one place that needs it."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, carrier_id, filename, file_type, size_bytes, pages,
                   chars, status, error_message, extracted_text, uploaded_at
            FROM documents
            WHERE id = %s
            """,
            (document_id,),
        )
        return cur.fetchone()


def delete_carrier(conn, carrier_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM carriers WHERE id = %s", (carrier_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------


def save_summary(
    conn,
    *,
    carrier_id: str,
    summary: str,
    company_overview: dict,
    requirements: list[dict],
    plans: list[dict],
    exclusions: list[dict],
    key_points: list[dict],
    stats: dict,
    model: str | None,
) -> dict:
    """
    Upsert the one summary row for a carrier.

    A carrier can be re-summarised (new documents added, or a re-run after
    a prompt change) — this replaces the previous summary rather than
    versioning it, same "current state, not history" choice the reference
    project makes for its own summaries table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summaries (
                carrier_id, summary, summary_original, company_overview,
                requirements, plans, exclusions, key_points, stats, model
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (carrier_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                summary_original = EXCLUDED.summary_original,
                company_overview = EXCLUDED.company_overview,
                requirements = EXCLUDED.requirements,
                plans = EXCLUDED.plans,
                exclusions = EXCLUDED.exclusions,
                key_points = EXCLUDED.key_points,
                stats = EXCLUDED.stats,
                model = EXCLUDED.model,
                generated_at = now(),
                edited_at = NULL
            RETURNING id, carrier_id, summary, company_overview, requirements,
                      plans, exclusions, key_points, stats, model, generated_at,
                      edited_at
            """,
            (
                carrier_id,
                summary,
                summary,
                Jsonb(company_overview),
                Jsonb(requirements),
                Jsonb(plans),
                Jsonb(exclusions),
                Jsonb(key_points),
                Jsonb(stats),
                model,
            ),
        )
        return dict(cur.fetchone())


def get_summary(conn, carrier_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, carrier_id, summary, company_overview, requirements,
                   plans, exclusions, key_points, stats, model, generated_at,
                   edited_at
            FROM summaries
            WHERE carrier_id = %s
            """,
            (carrier_id,),
        )
        return cur.fetchone()


def update_summary_text(conn, carrier_id: str, summary: str) -> dict | None:
    """User-edited summary text. Leaves the structured fields untouched."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE summaries
            SET summary = %s, edited_at = now()
            WHERE carrier_id = %s
            RETURNING id, carrier_id, summary, company_overview, requirements,
                      plans, exclusions, key_points, stats, model, generated_at,
                      edited_at
            """,
            (summary, carrier_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------


def save_chunks(conn, document_id: str, chunks: list[dict]) -> None:
    """
    Replace all stored chunks for a document.

    Delete-then-insert rather than upsert: a re-summarise can produce a
    different chunk count than before (different boundaries after an edit
    to the parser or a document re-upload), and stale extra rows from a
    previous run would otherwise survive alongside the new ones.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        for chunk in chunks:
            cur.execute(
                """
                INSERT INTO chunks (document_id, chunk_index, section_title,
                                     page_start, text, summary)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    chunk["index"],
                    chunk.get("section_title"),
                    chunk.get("page_start"),
                    chunk["text"],
                    chunk.get("summary"),
                ),
            )


def get_chunks_for_document(conn, document_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, document_id, chunk_index, section_title, page_start,
                   text, summary, created_at
            FROM chunks
            WHERE document_id = %s
            ORDER BY chunk_index ASC
            """,
            (document_id,),
        )
        return cur.fetchall()


def get_chunks_for_carrier(conn, carrier_id: str) -> list[dict]:
    """Every chunk across every document belonging to one carrier."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ch.id, ch.document_id, ch.chunk_index, ch.section_title,
                   ch.page_start, ch.text, ch.summary, d.filename
            FROM chunks ch
            JOIN documents d ON d.id = ch.document_id
            WHERE d.carrier_id = %s
            ORDER BY d.uploaded_at ASC, ch.chunk_index ASC
            """,
            (carrier_id,),
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# Phase 2 — clients
# --------------------------------------------------------------------------

# Columns a client write accepts. Listed once so the insert, the update and
# the API layer cannot drift apart.
CLIENT_FIELDS = (
    "business_name", "entity_type", "industry", "city", "state",
    "turnover", "years_trading", "employee_count",
    "pan", "gstin", "contact_name", "contact_phone", "contact_email",
    "sum_insured", "risk_notes",
)


def create_client(conn, *, fields: dict, categories: list[str],
                  redacted_profile: str) -> dict:
    """
    Insert a client and return the full row.

    Same pilot-stage default-organization fallback as create_carrier: the
    existing client-intake flow predates organization_id and is untouched
    here, kept working by defaulting rather than requiring every call site
    to be updated for a column it has no reason to know about.
    """
    columns = list(CLIENT_FIELDS)
    values = [fields.get(c) for c in columns]

    placeholders = ", ".join(["%s"] * (len(columns) + 3))
    column_sql = ", ".join(columns + ["categories", "redacted_profile", "organization_id"])

    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO clients ({column_sql}) VALUES ({placeholders}) "
            f"RETURNING *",
            (*values, Jsonb(categories), redacted_profile, DEFAULT_ORGANIZATION_ID),
        )
        return dict(cur.fetchone())


def update_client(conn, client_id: str, *, fields: dict, categories: list[str],
                  redacted_profile: str) -> dict | None:
    """Replace a client's details. Returns None if the client is gone."""
    columns = list(CLIENT_FIELDS)
    assignments = ", ".join(f"{c} = %s" for c in columns)
    values = [fields.get(c) for c in columns]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE clients
            SET {assignments},
                categories = %s,
                redacted_profile = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (*values, Jsonb(categories), redacted_profile, client_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_clients(conn) -> list[dict]:
    """
    Every client, newest first, with whether a match has been run.

    Excludes risk_notes and redacted_profile — a list view does not need
    them, and they are the longest columns on the table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.business_name, c.industry, c.city, c.state,
                   c.categories, c.turnover, c.created_at,
                   (SELECT max(created_at) FROM matches m WHERE m.client_id = c.id)
                       AS last_matched_at
            FROM clients c
            ORDER BY c.created_at DESC
            """
        )
        return cur.fetchall()


def get_client(conn, client_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def delete_client(conn, client_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM clients WHERE id = %s", (client_id,))
        return cur.rowcount > 0


def save_client_tokens(conn, client_id: str, mapping: dict) -> None:
    """Upsert the placeholder map for one client."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO client_tokens (client_id, mapping)
            VALUES (%s, %s)
            ON CONFLICT (client_id) DO UPDATE
                SET mapping = EXCLUDED.mapping, updated_at = now()
            """,
            (client_id, Jsonb(mapping)),
        )


def get_client_tokens(conn, client_id: str) -> dict:
    """The placeholder map, or an empty dict if none stored yet."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mapping FROM client_tokens WHERE client_id = %s", (client_id,)
        )
        row = cur.fetchone()
        return row["mapping"] if row else {}


# --------------------------------------------------------------------------
# Phase 2 — matches
# --------------------------------------------------------------------------


def save_match(conn, *, client_id: str, results: list[dict],
               sources: list[dict], model: str | None, stats: dict) -> dict:
    """Append a match run. History, not current state — see the schema note."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO matches (client_id, results, sources, model, stats)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, client_id, results, sources, model, stats, created_at
            """,
            (client_id, Jsonb(results), Jsonb(sources), model, Jsonb(stats)),
        )
        return dict(cur.fetchone())


def get_latest_match(conn, client_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, client_id, results, sources, model, stats, created_at
            FROM matches
            WHERE client_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (client_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Reindex support
# --------------------------------------------------------------------------


def all_indexable_documents(conn) -> list[dict]:
    """
    Every parsed document that has stored chunks, with its carrier.

    Used to rebuild the vector index from Postgres. This is what makes the
    Chroma volume disposable: nothing here needs a model call to regenerate,
    only re-embedding.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT d.id AS document_id, d.filename,
                   c.id AS carrier_id, c.name AS carrier_name
            FROM documents d
            JOIN carriers c ON c.id = d.carrier_id
            JOIN chunks ch ON ch.document_id = d.id
            WHERE d.status = 'parsed'
            ORDER BY c.name
            """
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# Phase 3 — documents held, and gap reports
# --------------------------------------------------------------------------


def set_client_documents(conn, client_id: str, *, held: list[str],
                         missing: list[str]) -> None:
    """Replace both document lists for a client."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE clients
            SET documents_held = %s, documents_missing = %s, updated_at = now()
            WHERE id = %s
            """,
            (Jsonb(held), Jsonb(missing), client_id),
        )


def save_gap_report(conn, *, client_id: str, carrier_id: str, items: list[dict],
                    summary: str, stats: dict, model: str | None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO gap_reports (client_id, carrier_id, items, summary, stats, model)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, client_id, carrier_id, items, summary, stats, model, created_at
            """,
            (client_id, carrier_id, Jsonb(items), summary, Jsonb(stats), model),
        )
        return dict(cur.fetchone())


def get_latest_gap_reports(conn, client_id: str) -> list[dict]:
    """
    The most recent report per carrier for one client.

    DISTINCT ON keeps one row per carrier — the newest — so re-running for
    one carrier does not hide the others.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (g.carrier_id)
                   g.id, g.client_id, g.carrier_id, c.name AS carrier_name,
                   g.items, g.summary, g.stats, g.model, g.created_at
            FROM gap_reports g
            JOIN carriers c ON c.id = g.carrier_id
            WHERE g.client_id = %s
            ORDER BY g.carrier_id, g.created_at DESC
            """,
            (client_id,),
        )
        return cur.fetchall()


# --------------------------------------------------------------------------
# Phase 3.5 — documents the client actually sent
# --------------------------------------------------------------------------


def save_client_document(conn, *, client_id: str, requirement: str, filename: str,
                         file_type: str, size_bytes: int, chars: int,
                         extracted_text: str | None, verification: dict) -> dict:
    """Store one uploaded document and its verification result."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO client_documents (
                client_id, requirement, filename, file_type, size_bytes, chars,
                extracted_text, verdict, doc_type, reason, identifiers, notes,
                key_facts, score, score_parts, improvements
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, client_id, requirement, filename, file_type, size_bytes,
                      chars, verdict, doc_type, reason, identifiers, notes,
                      key_facts, score, score_parts, improvements, uploaded_at
            """,
            (
                client_id, requirement, filename, file_type, size_bytes, chars,
                extracted_text,
                verification.get("verdict", "unchecked"),
                verification.get("doc_type"),
                verification.get("reason"),
                Jsonb(verification.get("identifiers") or {}),
                Jsonb(verification.get("notes") or []),
                Jsonb(verification.get("key_facts") or []),
                verification.get("score"),
                Jsonb(verification.get("score_parts") or {}),
                Jsonb(verification.get("improvements") or []),
            ),
        )
        return dict(cur.fetchone())


def list_client_documents(conn, client_id: str) -> list[dict]:
    """
    Every document attached for a client, newest first per requirement.

    Excludes extracted_text — a list view never needs it and it is the
    largest column on the table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, client_id, requirement, filename, file_type, size_bytes,
                   chars, verdict, doc_type, reason, identifiers, notes,
                   key_facts, score, score_parts, improvements, uploaded_at
            FROM client_documents
            WHERE client_id = %s
            ORDER BY uploaded_at DESC
            """,
            (client_id,),
        )
        return cur.fetchall()


def delete_client_document(conn, document_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM client_documents WHERE id = %s", (document_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Phase 4 — drafted paperwork
# --------------------------------------------------------------------------


def save_draft(conn, *, client_id: str, carrier_id: str | None, kind: str,
               title: str, body: str, body_redacted: str, warnings: list[dict],
               stats: dict, model: str | None) -> dict:
    """Insert a new draft. Always starts at status 'draft'."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO drafts (client_id, carrier_id, kind, title, body,
                                body_redacted, warnings, stats, model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, client_id, carrier_id, kind, title, body, status,
                      warnings, stats, model, created_at, updated_at
            """,
            (client_id, carrier_id, kind, title, body, body_redacted,
             Jsonb(warnings), Jsonb(stats), model),
        )
        return dict(cur.fetchone())


def list_drafts(conn, client_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.client_id, d.carrier_id, c.name AS carrier_name,
                   d.kind, d.title, d.body, d.status, d.warnings, d.stats,
                   d.model, d.created_at, d.updated_at
            FROM drafts d
            LEFT JOIN carriers c ON c.id = d.carrier_id
            WHERE d.client_id = %s
            ORDER BY d.created_at DESC
            """,
            (client_id,),
        )
        return cur.fetchall()


def get_draft(conn, draft_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.id, d.client_id, d.carrier_id, c.name AS carrier_name,
                   d.kind, d.title, d.body, d.status, d.warnings, d.stats,
                   d.model, d.created_at, d.updated_at
            FROM drafts d
            LEFT JOIN carriers c ON c.id = d.carrier_id
            WHERE d.id = %s
            """,
            (draft_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_draft_body(conn, draft_id: str, *, title: str, body: str,
                      warnings: list[dict]) -> dict | None:
    """
    Save a human edit.

    Always moves status to 'edited', including away from 'approved'. That is
    deliberate: a document that changes after sign-off has to be signed off
    again, or the approval refers to text nobody approved.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE drafts
            SET title = %s, body = %s, warnings = %s,
                status = 'edited', updated_at = now()
            WHERE id = %s
            RETURNING id, client_id, carrier_id, kind, title, body, status,
                      warnings, stats, model, created_at, updated_at
            """,
            (title, body, Jsonb(warnings), draft_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_draft(conn, draft_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM drafts WHERE id = %s", (draft_id,))
        return cur.rowcount > 0


def log_draft_event(conn, *, draft_id: str, event: str, actor: str | None = None,
                    detail: str | None = None) -> None:
    """Append one entry to a draft's audit trail. Never updates an existing row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO draft_events (draft_id, event, actor, detail)
            VALUES (%s, %s, %s, %s)
            """,
            (draft_id, event, actor, detail),
        )


def get_draft_events(conn, draft_id: str) -> list[dict]:
    """A draft's history, oldest first — the order it actually happened in."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, draft_id, event, actor, detail, created_at
            FROM draft_events
            WHERE draft_id = %s
            ORDER BY created_at ASC
            """,
            (draft_id,),
        )
        return cur.fetchall()


def set_draft_status(conn, draft_id: str, status: str) -> dict | None:
    """Move a draft between draft / edited / approved."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE drafts SET status = %s, updated_at = now()
            WHERE id = %s
            RETURNING id
            """,
            (status, draft_id),
        )
        return cur.fetchone()


# --------------------------------------------------------------------------
# Phase 5 — corrections and learned guidance
# --------------------------------------------------------------------------


def save_correction(conn, *, kind: str, client_id: str | None,
                    carrier_id: str | None, subject: str | None,
                    before_text: str | None, after_text: str | None,
                    meta: dict) -> None:
    """Record one human correction. Append-only, like the audit trail."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corrections (kind, client_id, carrier_id, subject,
                                     before_text, after_text, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (kind, client_id, carrier_id, subject, before_text, after_text,
             Jsonb(meta)),
        )


def recent_corrections(conn, kind: str, limit: int = 20) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, kind, client_id, carrier_id, subject, before_text,
                   after_text, meta, created_at
            FROM corrections
            WHERE kind = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (kind, limit),
        )
        return cur.fetchall()


def get_house_style(conn, kind: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM house_style WHERE kind = %s", (kind,))
        row = cur.fetchone()
        return dict(row) if row else None


def save_house_style(conn, *, kind: str, guidance: str, derived_from: int,
                     edited_by_hand: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO house_style (kind, guidance, derived_from, edited_by_hand)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (kind) DO UPDATE SET
                guidance = EXCLUDED.guidance,
                derived_from = EXCLUDED.derived_from,
                edited_by_hand = EXCLUDED.edited_by_hand,
                updated_at = now()
            RETURNING *
            """,
            (kind, guidance, derived_from, edited_by_hand),
        )
        return dict(cur.fetchone())


def insight_counts(conn) -> dict:
    """
    The numbers behind the Review page, computed from real rows.

    Every figure here is a plain count over what actually happened — nothing
    is estimated or modelled, so a number that looks wrong can be traced to
    the rows that produced it.
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM carriers")
        out["carriers"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM summaries")
        out["carriers_briefed"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM clients")
        out["clients"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM matches")
        out["matches_run"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM gap_reports")
        out["gap_reports"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM drafts")
        out["drafts"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM drafts WHERE status = 'approved'")
        out["drafts_approved"] = cur.fetchone()["n"]

        # How often an attached document turned out to be wrong. The point of
        # verification, stated as a number.
        cur.execute(
            """
            SELECT verdict, count(*) AS n
            FROM client_documents GROUP BY verdict ORDER BY n DESC
            """
        )
        out["verdicts"] = {r["verdict"]: r["n"] for r in cur.fetchall()}

        # Carriers whose briefing found few or no requirements — the most
        # actionable signal in here, because gap analysis is worthless
        # against a carrier with an empty checklist.
        cur.execute(
            """
            SELECT c.name, jsonb_array_length(s.requirements) AS requirements
            FROM summaries s JOIN carriers c ON c.id = s.carrier_id
            ORDER BY requirements ASC
            LIMIT 10
            """
        )
        out["thin_briefings"] = [
            {"carrier": r["name"], "requirements": r["requirements"]}
            for r in cur.fetchall()
        ]

        # Requirements a person kept overriding: the tool judged them wrong
        # often enough to be worth looking at.
        cur.execute(
            """
            SELECT subject, count(*) AS n
            FROM corrections
            WHERE kind = 'gap_override' AND subject IS NOT NULL
            GROUP BY subject ORDER BY n DESC LIMIT 8
            """
        )
        out["overridden_requirements"] = [
            {"requirement": r["subject"], "times": r["n"]} for r in cur.fetchall()
        ]

        # How heavily drafts get rewritten before sign-off.
        cur.execute(
            """
            SELECT
                count(*) AS edits,
                avg((meta->>'changed_pct')::float) AS avg_changed
            FROM corrections WHERE kind = 'draft_edit'
            """
        )
        row = cur.fetchone()
        out["draft_edits"] = row["n"] if "n" in row else row["edits"]
        out["avg_draft_change_pct"] = (
            round(row["avg_changed"], 1) if row["avg_changed"] is not None else None
        )
    return out


# --------------------------------------------------------------------------
# Document Intelligence vertical slice — organizations, facts, events
# --------------------------------------------------------------------------

DEFAULT_ORGANIZATION_ID = "00000000-0000-0000-0000-000000000001"


def get_or_create_default_organization(conn) -> str:
    """
    The pilot-stage default org.

    A real onboarding flow would let a broker create their own organization;
    this vertical slice does not build that yet — it only needs to prove the
    column and the scoping work, which a single default organization already
    does. Genuinely creating a second organization (and confirming a second
    broker's data stays apart from the first's) is the actual test of this
    boundary, not a UI to manage many of them.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (DEFAULT_ORGANIZATION_ID, "Default organization"),
        )
    return DEFAULT_ORGANIZATION_ID


def create_organization(conn, name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (name) VALUES (%s) RETURNING id, name, created_at",
            (name,),
        )
        return dict(cur.fetchone())


def list_organizations(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, created_at FROM organizations ORDER BY created_at")
        return cur.fetchall()


def get_organization(conn, organization_id: str) -> dict | None:
    """Used to confirm an X-Organization-Id header actually names a real
    organization, before trusting it to scope every other query."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, created_at FROM organizations WHERE id = %s",
            (organization_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def save_extraction_fact(conn, *, organization_id: str, document_id: str,
                         fact_type: str, label: str, value: str,
                         evidence: dict, confidence: dict,
                         status: str, produced_by: str) -> dict:
    """
    Insert one extracted fact with its evidence and typed confidence.

    `evidence` is {page, section, snippet, source}. `confidence` is
    {value, type, method, note} — see the schema comment on
    extraction_facts for what each confidence_type actually claims.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_facts (
                organization_id, document_id, fact_type, label, value,
                evidence_page, evidence_section, evidence_snippet, evidence_source,
                confidence_value, confidence_type, confidence_method, confidence_note,
                status, original_value, produced_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                organization_id, document_id, fact_type, label, value,
                evidence.get("page"), evidence.get("section"),
                evidence.get("snippet"), evidence.get("source", "text_layer"),
                confidence["value"], confidence["type"], confidence["method"],
                confidence.get("note"),
                status, value, produced_by,
            ),
        )
        return dict(cur.fetchone())


def list_extraction_facts(conn, document_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM extraction_facts WHERE document_id = %s ORDER BY created_at",
            (document_id,),
        )
        return cur.fetchall()


def get_extraction_fact(conn, fact_id: str, organization_id: str) -> dict | None:
    """
    Fetch one fact, scoped to the caller's organization.

    Same reasoning as get_submission: filtered in the query itself, so a
    fact belonging to a different organization is indistinguishable from
    one that does not exist, rather than fetched and rejected afterward.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM extraction_facts WHERE id = %s AND organization_id = %s",
            (fact_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def review_extraction_fact(conn, fact_id: str, *, organization_id: str, event: str,
                           reviewer: str, new_value: str | None,
                           reason: str | None) -> dict | None:
    """
    Record a human review action and update the fact's current state.

    Requires organization_id and checks it before writing anything — a
    review action changes data, so the ownership check here matters even
    more than on a read: without it, a caller who obtained or guessed
    another organization's fact_id could not just see the fact but alter
    it. The extraction_events row is the permanent record of what
    happened; the extraction_facts row is updated to reflect current
    status so the UI can show "corrected" without replaying the event log
    every time.
    """
    fact = get_extraction_fact(conn, fact_id, organization_id)
    if not fact:
        return None

    before_value = fact["value"]
    after_value = new_value if event == "corrected" else before_value
    new_status = {"verified": "verified", "corrected": "corrected", "flagged": "needs_review"}[event]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extraction_events (fact_id, event, reviewer, before_value, after_value, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (fact_id, event, reviewer, before_value, after_value, reason),
        )
        cur.execute(
            """
            UPDATE extraction_facts
            SET value = %s, status = %s, updated_at = now()
            WHERE id = %s AND organization_id = %s
            RETURNING *
            """,
            (after_value, new_status, fact_id, organization_id),
        )
        return dict(cur.fetchone())


def get_extraction_events(conn, fact_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM extraction_events WHERE fact_id = %s ORDER BY created_at ASC",
            (fact_id,),
        )
        return cur.fetchall()


def save_document_quality(conn, *, document_id: str, used_ocr: bool,
                          pages_total: int, pages_ocr_low_confidence: int,
                          avg_ocr_confidence: float | None, notes: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_quality (
                document_id, used_ocr, pages_total, pages_ocr_low_confidence,
                avg_ocr_confidence, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_id) DO UPDATE SET
                used_ocr = EXCLUDED.used_ocr,
                pages_total = EXCLUDED.pages_total,
                pages_ocr_low_confidence = EXCLUDED.pages_ocr_low_confidence,
                avg_ocr_confidence = EXCLUDED.avg_ocr_confidence,
                notes = EXCLUDED.notes
            """,
            (document_id, used_ocr, pages_total, pages_ocr_low_confidence,
             avg_ocr_confidence, Jsonb(notes)),
        )


def get_document_quality(conn, document_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM document_quality WHERE document_id = %s", (document_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# --------------------------------------------------------------------------
# Submissions and cross-document contradiction detection
# --------------------------------------------------------------------------


def create_submission(conn, *, organization_id: str, label: str) -> dict:
    """A new group of documents believed to belong to the same case/company."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO submissions (organization_id, label) VALUES (%s, %s) "
            "RETURNING id, organization_id, label, created_at",
            (organization_id, label),
        )
        return dict(cur.fetchone())


def get_submission(conn, submission_id: str, organization_id: str) -> dict | None:
    """
    Fetch a submission, scoped to the caller's organization.

    Deliberately requires organization_id and filters on it, rather than
    fetching by id alone and letting the caller check afterward: a
    submission that belongs to a different organization must not exist as
    far as this query is concerned, not exist-but-get-rejected one layer up
    where a future call site could forget the check. Returns None for a
    submission that exists but belongs to someone else, same as one that
    does not exist at all — a 404 either way, so a caller ownership probe
    cannot even distinguish "not found" from "not yours".
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM submissions WHERE id = %s AND organization_id = %s",
            (submission_id, organization_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_submissions(conn, organization_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM submissions WHERE organization_id = %s ORDER BY created_at DESC",
            (organization_id,),
        )
        return cur.fetchall()


def set_document_submission(conn, document_id: str, submission_id: str) -> None:
    """
    Attach an already-created document to a submission.

    Kept separate from create_document rather than added as a new parameter
    to it: create_document is also called by the carrier-briefing flow,
    which has no concept of a submission at all, and every call site would
    otherwise need updating for a feature it doesn't use.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET submission_id = %s WHERE id = %s",
            (submission_id, document_id),
        )


def list_submission_documents(conn, submission_id: str, organization_id: str) -> list[dict]:
    """
    Documents in a submission, scoped to the caller's organization via a
    join back to submissions — belt-and-braces alongside the caller
    checking get_submission() first, so this function is safe to call even
    if a future call site forgets that check.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.* FROM documents d
            JOIN submissions s ON s.id = d.submission_id
            WHERE d.submission_id = %s AND s.organization_id = %s
            ORDER BY d.uploaded_at ASC
            """,
            (submission_id, organization_id),
        )
        return cur.fetchall()


def list_submission_facts(conn, submission_id: str, organization_id: str) -> list[dict]:
    """
    Every extraction_facts row for every document in one submission, scoped
    to the caller's organization.

    This is what a contradiction check compares — every fact currently on
    record for the submission, joined back to which document it came from
    so the comparison can name both sources when it flags a mismatch.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.*, d.filename AS document_filename
            FROM extraction_facts f
            JOIN documents d ON d.id = f.document_id
            JOIN submissions s ON s.id = d.submission_id
            WHERE d.submission_id = %s AND s.organization_id = %s
            ORDER BY f.created_at ASC
            """,
            (submission_id, organization_id),
        )
        return cur.fetchall()


def flag_contradiction(conn, *, fact_id: str, contradicts_fact_id: str,
                       note: str) -> dict | None:
    """
    Mark a fact as disagreeing with another fact in the same submission.

    Both facts are flagged, symmetrically — a reviewer opening either one
    should see that it is in dispute, not just the one that happened to be
    extracted second. Status moves to 'contradiction_flagged' regardless of
    whatever it was before, since a disagreement between two documents is a
    more specific and more urgent reason for review than the generic
    'needs_review' state it may have already been in.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extraction_facts
            SET status = 'contradiction_flagged',
                contradicts_fact_id = %s,
                contradiction_note = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (contradicts_fact_id, note, fact_id),
        )
        return dict(cur.fetchone()) if cur.rowcount else None
