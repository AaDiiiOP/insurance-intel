# Aurevia BrokerOS

**Version: 0.16.0** (see `frontend/package.json` — the one version number
for the whole project; bump it there on every release)

**Status (stated precisely, 2026-10-02):**

| Part | Status |
|---|---|
| Phase 0 — Document Intelligence | Frozen. 100% accuracy, 0 silent guesses. |
| Phase 1A — property/policy extraction | Safety hardening done; extraction quality still in a development loop. Full 20-company run of v0.7.0: 100% recall, 19/20 companies clean, 2 silent wrong (AUR-SYN-014 endorsements). Fixed in v0.7.1 and re-checked on that company only (0 silent wrong). **No full 20-company run exists on the fixed code; it is due at the end of the Knowledge Core phase.** |
| Phase 1B — Knowledge Core | Implemented and hardening: entities, facts, evidence, relationships, versioning, temporal facts, conflicts, review, tenant scoping, 13/13 deterministic comparison cases. |
| v0.8.1 — policy-period extraction | Real-Qwen `--limit 3` (2026-10-02): recall 100%, evidence 100%, period read on 3/3 policies, but **hard gate FAILED: 2 silent wrong** — `policy.coverage` 'Plant and machinery' read from the endorsement's valuation note (AUR-SYN-002, -003). Not caused by the period code. |
| v0.8.3 — coverage validity rule | Fix for the above. Checked deterministically on all 20 companies: 40/40 ground-truth coverages kept, the valuation note rejected in 20/20 endorsements. 98/98 tests. Real-Qwen re-check on AUR-SYN-002/003: silent wrong 0 (reported by developer). Full 20-company run due at phase end. |

| v0.9.0 — Knowledge explorer UI | Entity → relationships → facts (Current / Historical / Needs review / Verified / Contradiction / Superseded) → evidence → source page, "in force on" date, Verify/Reject, duplicate-name review. Backend: two read endpoints; extraction and scoring untouched. 100/100 tests; screens checked in a browser. |

| v0.10.0 — Knowledge query layer (Phase 1B.5) | `POST /knowledge/query`: deterministic answers from facts + evidence + periods + relationships; previous/change/as-of questions; abstains on contradiction, ambiguity, missing period or no evidence; keyword retrieval + quote-verified local-model answer only when no fact type applies. Policy comparison now carries evidence, % change and an assessment. 125/125 tests; 13/13 comparison; ground-truth simulation 40/40. **Real-Qwen 4-company `--query-check` run pending.** |

| v0.10.0 real-Qwen 4-company `--query-check` (2026-10-06) | Recall 98.1%, evidence 100% verified / 100% page accuracy; **silent wrong 1** (AUR-SYN-002 endorsement written as "Endorsement SYN-END-0002-01: …" — a string not in the document); query check 40 questions: 33 correct, 6 abstained (coverage-limit contradictions in 4/4 companies from the machinery valuation note; 1 occupancy; 1 sum insured), **1 wrong** (same endorsement). Gate FAILED. |
| v0.10.1 — fixes for that run | Endorsement header labels stripped from values; a limit / sum insured must be stated on a limit / sum-insured line. Deterministic check on all 20 companies: 60/60 ground-truth limits & sums kept, machinery decoys rejected 40/40, endorsement cleanup 40/40. 128/128 tests, 13/13 comparison. Real-Qwen re-run of the same 4 companies (2026-10-06): 0 silent wrong, 0 wrong answers (reported by the developer; result file in `backend/evaluations/phase1a/`). Full 20-company acceptance still pending. |

| v0.10.2 — Phase 1B audit & hardening | Full audit against the code (see `PHASE_1B_STATUS.md`). Fixed: corrections of last year's value came back as current; human corrections were ignored by the query layer (silent-wrong path); an already-replaced value could be re-corrected; misleading questions ("time limit", "insurer address", "when built") got stored facts; peril-specific questions got the policy-wide value. Tenant-isolation audit over every endpoint (reads and writes): no leaks. 145/145 tests, 13/13 comparison, simulation 40/40 + 28/28 probes. **Real-Qwen 4-company `--query-check` (now with 7 safety probes per company) pending.** |

| v0.10.2 real-Qwen 4-company `--query-check` (2026-10-07) | Silent wrong **0**, wrong answers **0**, evidence 100% / page 100%, recall 96.2% (one Ollama HTTP 500 on AUR-SYN-001; clean re-run of 001: recall 100%, 7/7 probes). 1 of 28 safety probes failed: an undated limit was given for a date before the policy began. |
| v0.10.3 — fix | Undated period-bound values are never given outside the known policy periods; reproduced and tested (146/146). `recheck_query.py` re-grades stored facts without the model. Recheck on Run 1's stored facts (2026-10-07): 40 questions, 34 correct, 6 abstained, **0 wrong**, **28/28 probes**. **Phase 1B: READY FOR ACCEPTANCE on the 4-company development benchmark; 20-company acceptance deferred.** |

| v0.11.0 — Phase 1C Policy Intelligence | Policy number, insurer, conditions, definitions, sub-limits, per-coverage limits, endorsement ids/dates and document roles read deterministically from labelled lines; policy view / history / conflicts / typed comparison with both-side evidence (`/policies/*`); new policy questions; Policy panel in the explorer. 166/166 tests, 13/13 comparison, structure reader 20/20 companies, Real Qwen AUR-SYN-001–004 (2026-10-08): silent wrong 0, recall 100%, query 0 wrong + 28/28 probes, **policy check 28/28 correct + 24/24 probes**. **Phase 1C: READY FOR ACCEPTANCE (4-company development benchmark; 20-company deferred).** See `PHASE_1C_STATUS.md`. |
| v0.12.0 — Phase 1D Business Risk Intelligence | Business risk graph in the existing Knowledge Core (no graph database): locations, operations, assets, controls, losses, risk factors, risks, exposures, workforce, surveyor rating — read deterministically from labelled lines, each with document + page, dated by the document's own date. Time-aware profile (`?as_of=`), change intelligence (added / changed / unchanged / conflicting; never "removed" from absence), business conflicts, graph view (`/businesses/*`), business questions, Business panel in the explorer. 177/177 tests; dry run (model off) AUR-SYN-001–004: Phase 1B 28/28 probes, Phase 1C 28/28 + 24/24, **Phase 1D 53/53 + 24/24**. Real Qwen AUR-SYN-001–004 (2026-10-08): silent wrong 0, query 0 wrong + 28/28 probes, policy 28/28 + 24/24, **business check 53/53 correct + 24/24 probes**. **Phase 1D: ACCEPTANCE PASSED, FROZEN (4-company validation, 2026-10-09: audit 198/198, evidence 100%, page 100%; 20-company deferred).** See `PHASE_1D_STATUS.md`. |
| v0.13.0 — Phase 1E Risk → Coverage Intelligence | Documented exposures/risks mapped to the coverages of the policy in force on a date (mapping type + status), exposure vs limit (WITHIN / EXCEEDS / unknown / needs review — never "underinsured"), exclusion / condition / sublimit / deductible reviews, coverage conflicts, coverage changes over time and policy-change → exposure impact, broker review of findings, coverage graph, Coverage audit panel. Advice questions ("adequately insured?") are declined. Fixed a Knowledge Core silent-wrong case (earlier-period value read after a renewal). 199/199 tests; dry run AUR-SYN-001–004: coverage check 36/36 + 51/51 probes, earlier gates unchanged. Real Qwen AUR-SYN-001–004 (2026-10-10): silent wrong 0, all gates 0 wrong, **coverage check 33 correct + 3 correct abstentions, 51/51 probes, evidence/page 100%**. **Phase 1E: ACCEPTED (4-company), FROZEN — 2026-10-10; 20-company not claimed.** See `PHASE_1E_STATUS.md`. |
| v0.14.0 — Phase 2A Broker Workspace | Sign-in (argon2id, HttpOnly session cookie, CSRF token, rate limiting, expiry, revocation), roles OWNER/ADMIN/BROKER/REVIEWER with server-side permissions on every route, organization taken only from the signed-in membership (header can no longer choose it), invitations and admin-issued reset codes (no e-mail), client search/filters/pagination/archive/assignment, client ↔ business link, document library with stored originals and authorized download, append-only audit history, Team & security page, small-screen layout. 222/222 tests; intelligence gates unchanged. **Not production-ready; no PostgreSQL RLS.** See `PHASE_2A_STATUS.md`. |
| v0.14.1 — Phase 2A Docker fix | Fixes paperwork upload failing on databases created by older versions (`column "score" of relation "client_documents" does not exist`, found in the developer's Docker check): startup migrations now add every later `client_documents` column; nothing is dropped. New read-only `backend/check_schema.py` compares the database with what the code expects; `backend/phase2a_live_check.py` (41 HTTP checks) added to the repo. Test flake fixed (duplicate-upload test built the .docx twice). 225/225 tests. Developer's Docker (2026-10-26): schema PASS, live check 41/41, pytest 225/225, frontend OK. **Phase 2A: FROZEN — DEVELOPMENT GATE PASSED (2026-10-26); not production-ready, no RLS.** See `PHASE_2A_STATUS.md`. |
| v0.15.0 — Phase 2B Client Intelligence Workspace | One "Client intelligence" section on the client page: Overview (needs-attention list, business, policies, coverage findings, documents), Business & risks, Policies, Coverage findings, Conflicts & changes, Documents; one as-of date; every value labelled verified / read by rule / not yet verified / conflicting / not found, with evidence to document and page. New read-only `GET /clients/{id}/intelligence` built only from the existing Phase 1C–1E engines (per-section status; one engine failing never hides the others). Knowledge Core fix: a later period no longer drops one side of an earlier conflict. 244/244 tests; model-off gates unchanged; Phase 1D audit 198/198; live check 43/43 (sandbox). Developer's Docker (2026-10-27): schema PASS, 244/244, live check 43/43, browser OK. **Phase 2B: COMPLETE — development gate passed; not production-ready, no RLS.** See `PHASE_2B_STATUS.md`. |
| v0.16.0 — Phase 2C Human Review & Audit | "Review queue": conflicts, unverified facts, facts without evidence, coverage findings and failed documents, each linked to client, evidence and page; lifecycle open → assigned → in review → needs info / resolved / dismissed → reopened, enforced on the server; decisions only through the existing Knowledge Core (verify / versioned correction / reject) and Phase 1E coverage review; reasons required for corrections, rejections and dismissals; automatic reopen when evidence changes or a verified fact becomes conflicting; append-only review history; audit events for every review action (also on the existing fact and coverage endpoints). 265/265 tests; model-off gates unchanged; live check 44/44 (sandbox). Developer's Docker (2026-10-27): schema PASS, 265/265, live check 44/44, browser OK. **Phase 2C: COMPLETE — development gate passed; not production-ready, no RLS.** See `PHASE_2C_STATUS.md`. |

Tenant isolation is application-level (not PostgreSQL RLS); revisit before production.
See `Results.md` for run-by-run detail.

*(Previously "Insurance Intelligence" — renamed, same codebase, same
principles: everything below still applies.)*

An internal tool for an insurance broking team, built around one idea: an AI
that reads and writes insurance paperwork, running entirely on the broker's
own machine, with a human reviewing everything before it reaches an insurer.

It takes a broking team from "here are the carrier's policy wordings" through
matching a client to the right cover, checking their paperwork is real and
complete, and drafting the documents that go out — end to end, five phases,
described below.

---

## What it actually does

**1. Read an insurer's paperwork, once.** Upload a carrier's policy wordings,
rate cards, brochures — whatever they send. The tool reads them and produces
a structured briefing: what they sell, what a buyer has to provide, what's
excluded, the key figures. Every fact is traceable back to the section and
page it came from.

**2. Match a client to the right carrier.** Fill in (or auto-extract from
their own submission) a client's situation, and the tool searches every
briefed carrier at once, ranks what fits, explains why, and flags anything
that would affect the price — a basement that floods, a welding bay, a past
incident. Also answers free-form questions across the whole library ("which
of our carriers covers X").

**3. Check what paperwork is missing.** For any matched carrier, the tool
diffs the client's file against that carrier's actual requirements and
produces a checklist: have it, missing, or needs confirming, with the exact
request to send the client for anything outstanding.

**3.5. Verify the documents themselves.** Upload the real files against each
checklist item. Every document is checksummed where that's possible (GSTIN,
Aadhaar), cross-checked against the client's record (catching a document
that belongs to a *different* company), and scored out of 100 on
completeness, currency, and legibility — with a note on what would raise the
score. A document is scored on substance, never format: an accountant's
Tally export is treated the same as a formal letterhead PDF.

**4. Draft the paperwork.** Four kinds of document, all written from the
client's actual file, never invented facts:
- a submission letter to the underwriter,
- a structured risk summary to go with a proposal,
- a chase email to the client for outstanding items,
- or — given the carrier's own blank proposal form — the form itself,
  filled in, with unanswerable fields left visibly blank rather than guessed.

Every draft goes through review and sign-off with an append-only audit trail,
then exports as a real `.docx`. Nothing is ever sent automatically — there is
no send path in the code.

**5. Learn how this team writes.** The tool watches how brokers edit its own
drafts and, on request, distills that into a few lines of guidance applied to
future ones. Nothing is trained — it's plain text you can read, edit, or
delete — and a rule that would make a document less complete or less candid
is refused, in code, even if it's typed in by hand.

**Document Intelligence (early vertical slice).** A separate, deliberately
narrow path under its own "Doc Intelligence" screen: upload any policy
document — including a scanned one — and the tool extracts its coverage
limit / sum insured as one structured, evidence-backed fact rather than
free text. If the document has no usable text layer, OCR (Tesseract) runs
automatically. Every fact carries **typed** confidence — a value from a
deterministic rule reads differently from an OCR engine's own score, which
reads differently again from a model's self-reported plausibility, and the
UI labels which kind it's showing rather than presenting all three as
equivalent percentages. A human then verifies, corrects, or flags the fact,
and every action is kept in an append-only trail.

This is intentionally the only fact type extracted this way today. It
exists to prove the shape (evidence + typed confidence + review + audit)
works end to end before the same pattern is extended to more fact types or
document kinds — see "Document Intelligence" below for the reasoning.

---

## Why it's built this way

**Nothing leaves the machine unless you choose otherwise.** The model is a
local Ollama instance (`qwen2.5:7b` for reasoning, `nomic-embed-text` for
search) — no document or client detail is sent to an external API.

**Client identifiers are redacted before the model ever sees them — not
after.** Business names, PAN, GSTIN, phone numbers, emails are replaced with
placeholders (`{{BUSINESS_1}}`, `{{PAN_1}}`) the moment a client record is
created. The model reasons and drafts entirely in terms of placeholders; real
values are substituted back into the output only at the very end, by code the
model has no access to.

**A human signs off on everything that could reach an insurer.** Drafts are
never auto-sent. Approval requires a named person and is blocked outright if
the draft still contains an unfilled placeholder. The audit trail is
append-only — an edit after approval visibly revokes it rather than silently
keeping the old sign-off.

**Facts are separated from judgement everywhere it matters.** Whether a PAN
is on file is a database lookup, not something asked of the model — and if
the model disagrees with the record, the record wins. A checksum is computed,
not estimated. The model is only ever asked to do the part that genuinely
needs reading and judgement: whether a document is the right one, whether a
risk fits a policy, how to phrase a letter.

---

## Running it

### One-time setup

```bash
# Install Ollama: https://ollama.com
ollama pull qwen2.5:7b
ollama pull nomic-embed-text

# Confirm it's up:
curl http://localhost:11434/api/tags
```

### Every time

```bash
docker-compose up --build
```

Open **http://localhost:5183**.

| Service  | URL                   |
|----------|-----------------------|
| Frontend | http://localhost:5183 |
| Backend  | http://localhost:8100 (`/docs` for the API browser) |
| Postgres | localhost:5442 (`insurance_user` / `insurance_pass`) |
| ChromaDB | http://localhost:8101 |

The sidebar shows a status light; **Settings** probes every dependency and
says exactly which one is down and why, rather than a generic failure. First
briefing/match/draft on a fresh model can take a couple of minutes — there's
no cap on how long a request is allowed to run.

Ollama runs on the *host*, not in a container — it needs direct GPU/Metal
access a container wouldn't get. `docker-compose.yml` maps
`host.docker.internal` so the containers can still reach it.

---

## Running it on a server (AWS EC2 or similar)

Use `docker-compose.prod.yml`, not the dev one. It differs in ways that
matter on a public box: Ollama runs as a container rather than on the host,
the frontend is a built static bundle served by nginx instead of Vite's dev
server, Postgres and Ollama are not reachable from outside the machine, and
the file refuses to start without a real password and public URL rather than
falling back to local-dev defaults.

```bash
export PUBLIC_URL=http://<your-ec2-public-ip-or-dns>
export POSTGRES_PASSWORD=<something real>

docker compose -f docker-compose.prod.yml up -d --build

# Pull the models into the ollama container. Several GB, one time — they
# persist on a volume across restarts.
docker compose -f docker-compose.prod.yml exec ollama ollama pull qwen2.5:7b
docker compose -f docker-compose.prod.yml exec ollama ollama pull nomic-embed-text
```

The app is then on port 80, the API on 8100. Override with `FRONTEND_PORT`
and `BACKEND_PORT` if something else on the instance already uses them.

**Three things that are genuinely environment-specific, not config:**

1. **Instance sizing.** `qwen2.5:7b` needs real memory and compute. A small
   instance will either fail to load it or run unusably slowly. CPU-only,
   budget an `m5.xlarge` or larger; a `g4dn.xlarge` GPU instance is far
   faster (uncomment the `deploy:` block in the prod compose and install the
   NVIDIA container toolkit first). This is a cost decision no config file
   can avoid.
2. **Security groups.** Open inbound `80` (the app) and `8100` (the API) —
   to your own IP where possible, not `0.0.0.0/0`. Nothing else needs to be
   open: Postgres, ChromaDB and Ollama have no host ports in the prod
   compose, so they are reachable only from inside the instance. An exposed
   `11434` would be an open, unauthenticated model endpoint.
3. **`PUBLIC_URL` is baked into the frontend at build time.** It is compiled
   into the JavaScript a browser downloads, so changing it — a new IP after
   a stop/start, or moving to a domain — needs
   `docker compose -f docker-compose.prod.yml up -d --build`, not just a
   restart.

**If the page loads but everything fails with "Failed to fetch"** while
`/health` works: that is CORS. `PUBLIC_URL` must match the address the
browser actually loads the app from, exactly — protocol, host and port. The
prod compose passes it through to `CORS_ORIGINS` for you; the variable
accepts a comma-separated list if you need to allow both an IP and a domain
during a DNS cutover.

---

## Document Intelligence

A separate vertical slice, reachable from its own "Doc Intelligence" screen,
kept apart from the rest of the app on purpose.

**What it does.** Upload one policy document. If it has a usable text layer,
that's read directly; if it doesn't — a scanned copy, a photographed
schedule — Tesseract OCR runs automatically, page by page, and reports its
own confidence for each page. Either way, the tool then extracts exactly one
fact: the coverage limit / sum insured. That fact is stored with:

- **Evidence** — the page, section, and an exact quoted snippet it came from,
  so a reviewer can check the source rather than take the number on faith.
- **Typed confidence** — not a bare percentage. Three distinct kinds, each
  meaning something different, and the UI names which one it's showing:
  - `deterministic` — a rule (a labelled currency figure matching a known
    pattern) either matched or it didn't. Value is 1.0 or the fact isn't
    created at all.
  - `ocr_engine` — Tesseract's own per-word recognition confidence,
    averaged for the page the value came from.
  - `model_self_reported` — the local LLM's own stated plausibility when
    neither of the above applies. Never a calibrated probability, and
    never confused with the other two in the schema or the UI.
- **Status** — every fact starts `needs_review`. Nothing is ever
  auto-verified; a human always makes the final call.

A reviewer can **✓ Verify**, **✎ Correct** (with a reason), or **⚠ flag for
review**. Every action appends a row to an audit trail that is never edited
or deleted — the same principle as the draft-approval trail elsewhere in
this tool.

**Cross-document contradiction detection.** A `submission` groups documents
believed to belong to the same case (`POST /doc-intelligence/submissions`),
and every document uploaded against one (`POST /doc-intelligence/upload`
with a `submission_id`) is checked against every other fact already on
record for that submission. If a policy states one sum insured and its own
coverage schedule states a different one, both facts move to a
`contradiction_flagged` status — neither is silently kept over the other —
each carrying a note naming the other document and the percentage
difference. Two values are treated as agreeing within 1% (rounding,
formatting differences), so genuine disagreement is what actually gets
flagged. Verified against real cases — see `Results.md`.

**Now in the UI too.** The "Doc Intelligence" screen has a submission
picker — create a submission, upload several documents into it, and each
one's extracted fact appears as its own card. A contradiction is visible
immediately: both cards get a red left border and a "Contradicts another
document" pill, with the note naming the other document and the size of the
disagreement. Verified with the real AUR-SYN-004 case end to end (screenshot
in the development history for this feature). Uploading with no submission
selected still scores a single document on its own, exactly as before.

**Why only one fact type.** This exists to answer one question before
anything is built on top of it: do the evidence/confidence/review/audit
shapes actually hold up against a real document? Extracting broadly before
that question is answered would mean discovering a wrong schema after
building ten extraction paths on it, instead of after building one. Once
this shape is validated against real use, the same pattern extends to more
fact types — this is deliberately not that yet.

**Pilot-stage tenant boundary — now actually enforced, not just present as a
column.** An `X-Organization-Id` header controls which organization a
request acts as (defaulting to a single default organization when omitted,
so the existing UI and every earlier example above keep working
unmodified). Every read and write checks it — fetching another
organization's submission or fact returns the same 404 as one that does not
exist, and attempting to review or correct another organization's fact is
rejected the same way. Verified with real HTTP calls between two created
organizations, including confirming a write is actually blocked, not just a
read — see `Results.md`, Test 1. Still **application-level filtering, not
database-enforced isolation** (no Postgres row-level security) — a caller
that sends an id it should not have is still trusted by this layer. That
remains the deliberate pilot-stage boundary; what changed is that it is now
enforced everywhere it applies, rather than not existing.

**Now visible and usable in the UI, not just provable via curl.** The Doc
Intelligence screen has an "Acting as" organization switcher — create a
second organization and switch to it, and the submission list visibly
empties, because the backend genuinely treats the other organization's data
as though it does not exist. Every API call `api.js` makes carries whichever
organization is currently selected automatically; nothing elsewhere in the
app needed to change. Verified with a real browser test: created "Broker A,"
made a submission under it, created "Broker B," and confirmed Broker A's
submission disappeared from the list the moment the switch happened —
screenshots of each step exist in the development history for this feature.
**Nothing new was added to run this.** OCR uses Tesseract (a system package,
installed in the backend's own Docker image) and Poppler for PDF rendering —
both fully local and offline. Extraction uses the same local Ollama model as
every other feature in this tool. No new database, no cloud OCR, no paid API.

### Evaluation

`backend/eval_harness.py` runs the real pipeline against a labelled test set
and reports actual accuracy — not estimated. See **`Results.md`** in the
project root for the full run (20 synthetic companies, 100 documents, 6
scanned/OCR variants, 4 deliberate cross-document contradictions): 100%
extraction accuracy with a real `qwen2.5:7b`, all 4 contradiction cases now
correctly flagged, and one open finding worth reading before treating either
number as the whole picture — the model's self-reported confidence barely
varies across genuinely different documents.

Re-run it against any labelled document set with the same folder shape:

```bash
cd backend
python3 eval_harness.py /path/to/dataset
```

### Knowledge Core regression tests

`backend/tests/` is a permanent, re-runnable pytest suite covering the
Knowledge Core (entities/facts/evidence/relationships/conflicts/versioning):
singleton contradiction detection, multi-valued fact types never
conflicting, conflict resolution closing out the losing fact, version
history after a correction, evidence snippet anchoring (including a snippet
that spans a chunk boundary), cross-tenant evidence/relationship rejection,
page-specific OCR confidence, and chunked extraction past the old
12,000-character truncation point. It runs against a real local Postgres
database (throwaway `organizations` rows, not a mocked connection) and real
application code; where a real model call would otherwise be needed, it
runs against the same kind of Ollama-compatible stub server used earlier in
this project (`backend/tests/stub_ollama.py`) rather than mocking the code
under test.

```bash
cd backend
python3 -m pytest tests/
```

(`python3 -m pytest`, not a bare `pytest` — this project's dependencies,
including `pytest` itself per `requirements.txt`, are installed against the
same Python `python3`/`uvicorn` resolve to; a separately-installed `pytest`
executable on `PATH` can point at a different Python with none of this
project's packages installed.)

**Hard OCR stress test — closed, against the real model.** This dataset's
"scanned" cases are clean computer-rendered text turned into an image, which
only proves OCR *triggers* correctly, not that it's accurate under real
conditions. `backend/build_hard_ocr_set.py` builds 11 genuinely degraded
variants from one real source page (rotation, blur, low contrast, low
resolution, shadow, sensor noise, perspective skew, a combined
multi-distortion "phone photo," each with random JPEG recompression), and
`backend/run_hard_ocr_test.py` runs all 11 through the real pipeline and
records which model actually ran (`model_used`, `model_was_reachable`) so a
results file can't be mistaken for a stand-in run. Run against the real
local `qwen2.5:7b` (confirmed reachable): **6 correct, 5 correctly
abstained, 0 silent wrong answers, 0 parse failures.** The one case that
had been an open question through earlier stand-in runs — `perspective_skew`
— now extracts correctly under the real model. Full detail in
`Results.md`, Test 3, and `backend/evaluations/phase0/hard_ocr_results.json`.

```bash
cd backend
python3 build_hard_ocr_set.py /path/to/source.pdf
python3 run_hard_ocr_test.py
```

---

## Knowledge Core / Policy Intelligence

Reachable from the "Knowledge" screen. Builds on Document Intelligence's
evidence/confidence/review shapes but extracts a full fact model — ten
property/policy fact types instead of one — into a small, versioned entity
graph (Business/Asset/Policy), rather than one bare fact per document.

**Phase 1A — Knowledge Core plumbing hardened and permanently regression-
tested; real-model extraction quality confirmed by a fresh, authoritative
run (2026-09-29) with `model_reachable: true`.**
`property_policy_intelligence.py` extracts `property.sum_insured`,
`property.location`, `property.occupancy`, `property.construction`,
`property.risk_factors`, `policy.coverage`, `policy.coverage_limit`,
`policy.deductible`, `policy.exclusion`, and `policy.endorsement` from
every document in a submission, chunked (not truncated) and
snippet-verified against the real source text. Each fact type's shape —
singleton (one true current value, contradiction-checked) or multi-valued
(independently true, never contradiction-checked) — lives in one place,
`knowledge/fact_types.py`'s `FACT_DEFINITIONS` registry, so it is never
re-derived or duplicated across the extraction, reconciliation, and
evaluation code.

Two separate things are true here, and it matters not to conflate them:

- **The Knowledge Core plumbing** (contradiction detection, versioning,
  conflict resolution, evidence anchoring) is solid — covered by the
  17-test regression suite in `backend/tests/`, which runs against a real
  Postgres database and real application code. This does not need
  re-proving; it does not depend on any one model's extraction quality.
- **Real-model extraction quality** — whether `qwen2.5:7b` actually
  extracts all ten fact types correctly from real documents — is now
  confirmed by `backend/evaluations/phase1a/latest.json`
  (`model_reachable: true`, run 2026-09-29T18:11:41Z, `qwen2.5:7b` via
  local Ollama): **zero silently-wrong facts, 67.5% fact recall, 1.7%
  false-extraction rate** across all 20 synthetic companies
  (`backend/eval_property_policy.py`). The zero-silent-wrong-facts result
  is the hard gate and it holds; 67.5% recall means roughly a third of
  ground-truth facts are not yet being extracted, which is not the same
  claim as "100% extraction accuracy" and should not be read as one — it's
  the statistical threshold to keep improving over time, not a pass/fail
  gate. This fresh run reproduces the earlier historical result
  (`backend/evaluations/historical/2026-09-28_run3_silent_wrong_0.json`)
  almost exactly, confirming it wasn't a fluke. See `Results.md`'s Phase 1A
  section for the full detail and the dataset gap that was found and fixed
  along the way (some fact types weren't actually stated in the source
  documents for the model to find — a benchmark bug, not a model bug).

```bash
cd backend
python3 eval_property_policy.py /path/to/dataset
```

Every run is kept as its own timestamped file under
`backend/evaluations/phase1a/` (never overwritten in place — see
`backend/evaluations/README.md` for the full layout and why it exists),
and `backend/evaluations/phase1a/latest.json` always reflects the most
recent run where the model was genuinely reachable. The script itself
exits non-zero, and prints a plain warning, for a run where the model was
not reachable — that run is still saved for the record, but is never
written to `latest.json` and must never be read as a Phase 1A result.
Historical pre-`evaluations/` result files (from before this layout
existed) are preserved under `backend/evaluations/historical/`.

**Phase 1A extraction-improvement loop (v0.6.0 — code changed, NOT yet
re-measured with real Qwen).** The fresh real-Qwen run showed recall is very
uneven by fact type, which the blended 67.5% hid: `policy.coverage` 0/40,
`property.risk_factors` 0/20, `policy.endorsement` 3/40, while location,
occupancy, construction, sum insured, deductible, coverage limit and
exclusions were all at or near 100%. Findings and changes:

- **Benchmark defect found and fixed.** 20 of the 40 endorsement ground-truth
  facts (the "Additional named insured…" sentence) were clipped off the page
  in every generated PDF, so no reader — human or model — could ever extract
  them. `scripts/gen_phase1_supplement.py` now wraps long lines, and an audit
  confirms every ground-truth value now appears in its source PDF (0 missing,
  down from 20). The endorsement recall ceiling was 50% until this fix.
- **Extraction prompt rewritten** (`prompts/extract_property_facts.txt`): it
  said "one entry per fact type" and later "one per occurrence"; it now
  separates single-valued from multi-valued types explicitly, and its
  coverage example no longer teaches the model to emit one combined phrase.
  Abstention rules are unchanged.
- **Recovery pass** (`property_policy_intelligence.extract_with_recovery`,
  `prompts/extract_multivalued_facts.txt`): only when the main pass returned
  nothing for a multi-valued type whose labelled section is visibly in the
  text, one focused second call asks for just those types. It costs nothing
  when the main prompt already works, and it is stricter than the main pass:
  a recovered item whose snippet does not verify against the source is
  dropped, not written.
- **Evidence is now measured from what the Knowledge Core persisted**
  (Fact → Evidence → Document), with each snippet independently re-verified
  against the stored document text. Previously the benchmark reported
  `snippet_verified` / `evidence_page` as null for every fact.
- **Result files now carry per-fact-type recall** and `extraction_diagnostics`
  (what the model returned, what was dropped and under what name, whether
  the recovery pass fired) so a low number can be explained without another
  long run. Scoring rules were not changed.

**v0.6.1 — hard-gate fix (quick check on 3/20 passed; full 20-company run pending).** The v0.6.0 quick check
(`--limit 3`, saved in `backend/evaluations/phase1a/result-20260930T155314…json`,
NOT `latest.json`) showed 100% recall on all 10 fact types for those 3
companies, but 4 silently wrong `property.risk_factors` facts, all from
`04_risk_survey.pdf`: "Moderate risk rating", "Low overall risk rating", "Low
risk rating" and "Fire protection: hydrants and portable extinguishers
reported". The prompt already said these are not risk factors and the 7B model
ignored it. v0.6.1 adds a narrow deterministic validity rule
(`is_non_hazard_risk_factor`) that rejects a risk rating or a positive
protection statement (a protection statement with negative wording such as "no
hydrants" is kept), plus an explicit prompt example. It is counted in
`extraction_diagnostics` as `dropped_non_hazard_risk_factor`. Scoring is
unchanged. Known remaining issues from that run: occupancy paraphrases from
the risk survey and one `coverage_limit` "n1,650,000,000" from an endorsement
(both flagged as contradictions, not silent), and one endorsement snippet that
did not verify (98.4% verified).

**v0.7.0 — false-extraction investigation (3-company loop; full 20 NOT rerun).**
The v0.6.1 quick check (0 silent wrong, 100% recall, 9.7% false extraction: 3
`policy.coverage_limit` + 3 `property.occupancy`) was analysed fact by fact
against the source PDFs:

- **`n1,650,000,000` (AUR-SYN-003 coverage_limit) is NOT a legitimate revised
  value.** It appears in none of that company's documents (the endorsement says
  n150,000,000 and n500,000,000). Root cause: the snippet check only proves the
  *quote* is real, not that the extracted *number* is what the quote says.
  Fix: a numeric value must be stated in its source document, or be the sum of
  2-3 stated figures (so a legitimately combined n500M + n100M = n600M limit is
  still accepted). Otherwise it is dropped (`dropped_numeric_not_in_source`).
  The other two coverage_limit "false" values of the latest run are not in the
  console output; the result file now classifies every false fact itself.
- **The occupancy "false" facts** (e.g. "precision components" from the risk
  survey) ARE in their source documents: they are shorter restatements of the
  occupancy the policy describes in full. The Knowledge Core compares singleton
  text exactly, so it flags them as a contradiction. Correct safe behaviour
  (nothing silent), but noisy; a semantic equivalence check is a separate,
  later decision and was not added.
- **"Duplicate" singleton facts (e.g. sum_insured 14 extracted) are a counting
  artefact, not duplicate rows.** `reconcile_fact` already attaches a repeated
  value to the ONE existing fact as extra evidence (covered by a new test). The
  extractor just returns that fact once per occurrence. Results now also report
  `unique_persisted_facts` next to `fact_occurrences`. The existing metrics are
  unchanged.
- **Result files now classify every false extraction** as UNSUPPORTED /
  CONTRADICTORY_FACT / LEGITIMATE_ADDITIONAL_FACT (`false_extraction_breakdown`).
  This is reporting only; what counts as false or silently wrong is unchanged.

Status: code + 53 tests done, NOT yet measured with real Qwen. Next command:
`docker-compose exec backend python3 eval_property_policy.py /app/benchmark_data --limit 3`.
The 20-company benchmark has NOT been rerun in this iteration.

**First full 20-company run (v0.7.0, 2026-10-01, real qwen2.5:7b) — HARD GATE FAILED.**
`backend/evaluations/phase1a/latest.json` is this run. Recall 100% on all 10 fact
types (every one 20/20 or 40/40), false-extraction 6.7%, evidence snippet-verified
99.8%, page accuracy 100%, **silent wrong 2** (both in AUR-SYN-014). The two
facts were policy CONDITIONS from `01_property_policy.pdf` ("Fire protection
systems must be maintained.", "Material changes in occupancy or operations must
be notified.") labelled `policy.endorsement`. All 20 policies carry that same
Conditions line and the model got it right in 19. Phase 1A is therefore NOT
accepted. Other findings of that run: 25 of 27 false extractions are
CONTRADICTORY_FACT (13 occupancy restatements from risk surveys; 8 coverage
limits that are really "Revised machinery declared value" figures from
endorsements; 4 sum-insured values from coverage schedules that genuinely differ
from the policy, e.g. 370M vs 350M — correctly flagged). The other 2 are the
silent ones above. One snippet did not verify (AUR-SYN-009 coverage).

**v0.7.1 fix (not re-measured):** a `policy.endorsement` is dropped unless its
source document mentions an endorsement, change or amendment (none of the 20
policy documents does); the prompt now says conditions are not endorsements and
that a revised single-item value is not a coverage limit. New flag
`--only AUR-SYN-014` scores named companies only (partial run, never
`latest.json`). Caveat of the classifier: LEGITIMATE_ADDITIONAL_FACT only means
the VALUE is in the source — it does not check the fact TYPE, which is why the 2
silent facts carried that label.

**AUR-SYN-014 re-check on v0.7.1 (2026-10-01, `--only AUR-SYN-014`, real qwen2.5:7b) —
PASSED.** Silent wrong 0 (was 2), recall 100% on all 10 fact types, evidence
snippet-verified / traceable / page accuracy 100%, false-extraction 5.0% (1
coverage_limit, a CONTRADICTORY_FACT). Diagnostics: 22 facts returned, 2
rating/protection risk factors dropped. Note: the new endorsement-text rule did
not fire in this run — the model labelled the conditions correctly this time —
so the rule's effect is covered by its unit tests, not by this run.

**Current Phase 1A status, stated precisely:** the full 20-company run (v0.7.0)
gave 100% recall, 19 of 20 companies clean and 2 silent wrong facts in one
company (AUR-SYN-014). That company was fixed in v0.7.1 and re-verified on its
own with 0 silent wrong. A full 20-company run on v0.7.1 has NOT been done;
`phase1a/latest.json` is still the v0.7.0 full run. 55/55 regression tests pass.

## Knowledge Core v2 (v0.8.0) — effective dates, entity read, review

Built on the existing Knowledge Core (not a rewrite). Phase 1A extraction was
NOT changed, so the extraction benchmark was not rerun; `eval_policy_comparison`
still passes 13/13 and the suite is 73/73.

- **Effective dates.** `kc_facts.effective_from / effective_to` (nullable DATE,
  migration is additive and safe on existing data) hold the period a value
  applies to, as stated by the source. They are separate from `valid_from /
  valid_to`, which record when a value was learned or replaced. NULL = period
  unknown, never guessed.
- **History is not contradiction.** If a new fact and the current fact both
  state a period and the periods do not overlap (2025 limit vs 2026 limit),
  both are kept; the later period is current, the earlier is closed as
  historical. Overlapping periods with different values are still a
  contradiction; facts with no stated period behave exactly as before.
- **As-of reads.** `get_facts_in_force(entity, as_of)` answers "what was true
  on this date" separately from "what is current"; undated current facts are
  listed apart (period unknown), never assumed to apply.
- **`get_entity_knowledge(entity, as_of=None)`.** Current facts with evidence,
  historical facts, unresolved conflicts, relationships both ways (with the
  other entity's type and name), all organization-scoped.
- **Relationship rules.** `ALLOWED_RELATIONSHIPS` (owns, located_at,
  operates_at, has_policy, contains/provides, applies_to, protects, exposed_to,
  has_claim, supports) with the entity types each may join; unknown kinds,
  wrong type pairs and cross-tenant ids are rejected. Creation was already
  idempotent (confirmed by a new test).
- **Human review.** `review_fact(verify | reject)`: verify needs at least one
  evidence row and refuses a contradicted fact (settle those with the
  correction endpoint); reject closes the fact as `superseded` and keeps the
  row for audit.
- **API.** `GET /knowledge/entities/{id}/knowledge?as_of=YYYY-MM-DD`,
  `GET /knowledge/entities/{id}/relationships`,
  `POST /knowledge/facts/{id}/review`; fact responses now include
  `effective_from / effective_to`.

**Not done yet (known limitations):** (the extractor now reads policy periods — see v0.8.1 below); there is no
frontend panel for relationships / as-of / review yet (API only); entity
resolution is still exact-name only; hybrid retrieval is untouched.

### v0.8.1 — policy period read from documents (deterministic)

`property_policy_intelligence.extract_policy_period()` reads "Period: <date> to
<date>" (also "Policy Period" / "Period of Insurance") from the document text
with plain pattern matching — **no model, so it cannot guess**. Dates must use a
month name or ISO form; numeric dd/mm/yyyy is deliberately not read (3/4/2026 is
ambiguous), and two different periods, an unreadable date or an end before the
start all give "unknown". The period found in a document is attached as
`effective_from / effective_to` to that document's period-bound facts only
(coverage, coverage limit, deductible, exclusion, endorsement, sum insured);
location, occupancy, construction and risk factors stay undated. Documents with no
stated period (schedule, endorsement, survey) stay undated, so cross-document
contradictions behave exactly as before. Checked against the real benchmark: the
period is read from 20 of 20 `01_property_policy.pdf` and from none of the other
document types. 88/88 tests. **Not yet run with real Qwen** — it only touches
dates, but run `--limit 3` once to confirm recall and silent wrong are unchanged.

Do not read any of this as an improvement yet: the last measured real-Qwen
numbers are still 0 silent wrong, 67.5% recall, 1.7% false extraction. Run
a quick check first, then the full run:

```bash
# regenerate the dataset with the wrapping fix (only needed once).
# The container only sees backend/, so copy the script there first
# (PowerShell shown; use cp on Mac/Linux). If it complains that reportlab is
# missing: docker-compose exec backend pip install reportlab
Copy-Item scripts\gen_phase1_supplement.py backend\
docker-compose exec backend python3 gen_phase1_supplement.py /app/benchmark_data
# quick check, 3 companies (does NOT overwrite phase1a/latest.json)
docker-compose exec backend python3 eval_property_policy.py /app/benchmark_data --limit 3
# full run (writes phase1a/latest.json)
docker-compose exec backend python3 eval_property_policy.py /app/benchmark_data
```

**Phase 1B — coverage comparison.** `policy_comparison.py` diffs two
submissions' CURRENT facts — typically this year's renewal against last
year's expiring policy, or a new submission against an incumbent policy —
fact type by fact type, and reports what changed:

- **Singleton types** (`policy.deductible`, `policy.coverage_limit`,
  `property.sum_insured`, …): the one current value on each side is
  compared directly. A numeric fact type reports `increased` / `decreased`
  / `unchanged` (same 1% tolerance `reconcile_fact()` already uses to call
  two figures "the same" — this module never invents a second,
  independently-tuned notion of "close enough"); a text fact type reports
  `changed` / `unchanged`. Either side missing reports `added` / `removed`.
- **Multi-valued types** (`policy.exclusion`, `policy.endorsement`,
  `policy.coverage`, `property.risk_factors`): compared as sets — which
  values are `kept`, `added`, or `removed` between the two sides, since
  there is no single "the" exclusion to diff, only which ones are present.
- A side with an **unresolved contradiction** (more than one "current" fact
  for that entity+fact_type) is reported as such rather than silently
  picking one — the same "don't guess which side of a disagreement is
  right" principle as `get_current_value_or_conflict()`.

Pure read-side: running a comparison never creates, corrects, or
supersedes a fact. Two new endpoints, both requiring each submission to
have already been run through `analyze-property-policy`:

```
GET /knowledge/compare-submissions?left_submission_id=…&right_submission_id=…
GET /knowledge/compare-entities?left_entity_id=…&right_entity_id=…      (lower-level: any two entities of the same kind)
```

`left_` is the baseline; `right_` is what changed relative to it, so
`increased` / `added` / `removed` all read the way a broker reads "what
changed this renewal." Verified end-to-end against a real local Postgres
database (not just unit-level checks) and covered by the same regression
principle as the rest of Knowledge Core — see `Results.md`.

**Phase 1B acceptance benchmark — `eval_policy_comparison.py`.** Unlike
Phase 1A's benchmark, this never calls a model: `policy_comparison.py`'s
job starts after extraction, diffing facts that are already Knowledge Core
rows. So the benchmark writes those rows directly through the real
`reconcile_fact()` write path (the same one `property_policy_
intelligence.py` itself uses) and checks that `compare_submissions()`
reports exactly the expected change for each of 13 deterministic cases:
increased/decreased sum insured, increased/decreased deductible,
unchanged (including a within-tolerance rounding case), added/removed
singleton values, added/removed/kept multi-valued values (exclusions,
endorsements), a fully-unchanged policy, a fact stated on neither side,
and cross-organization isolation (confirms one organization can never
compare or read another organization's entities). This is deterministic
and 100% reproducible — every case either passes or fails, no statistical
threshold, no model variance to account for.

```bash
cd backend
python3 eval_policy_comparison.py
```

Currently: **13/13 passing.** Results are written the same way as Phase
1A's — every run timestamped and kept under `backend/evaluations/phase1b/`,
`latest.json` reflecting the most recent run.

---



**Foundation refactor (see `Results.md` for the full writeup).** `main.py`
and `db.py` had grown into two files holding six generations of features,
and organization scoping was inconsistent — only Document Intelligence
checked it. Both are now fixed and split:

```
backend/
  main.py         FastAPI app shell only — CORS, startup schema init,
                   include_router() calls. ~100 lines.
  db.py           A thin compatibility shim (`from repositories import *`)
                   so nothing importing db.xxx() had to change.

  api/            Every endpoint's actual logic, one file per feature —
                   health, organizations, carriers, clients, matching,
                   gaps, drafting, feedback, ask, submissions,
                   doc_intelligence. Every carrier/client/match/gap/draft
                   endpoint now resolves and enforces organization_id the
                   same way doc-intelligence always did.

  repositories/   The real database functions, one file per feature, plus
                   connection.py (shared connection/schema setup). Every
                   function scopes its SQL by organization_id in the query
                   itself — a record from another organization returns
                   404, indistinguishable from one that doesn't exist.

  parsers.py      pdf / docx / xlsx / pptx -> text
  chunking.py     structure-aware splitting, page/section provenance kept
  summarise.py    map-reduce briefing generation (Phase 1)
  tokenise.py     client redaction -- the boundary the whole design leans on
  vectorstore.py  ChromaDB indexing and retrieval (Phase 2) — every chunk
                   carries organization_id, every search() enforces it as
                   a hard filter (fixed; see Results.md — this was a real
                   gap the file-split refactor above did not cover)
  matching.py     multi-query retrieval + carrier ranking (Phase 2) —
                   organization-scoped end to end
  chat.py         question answering with a similarity floor + strict
                   prompt — organization-scoped end to end
  extraction.py   reading a client's own submission into intake fields
  gaps.py         requirement checklist with a deterministic backstop (Phase 3)
  verify.py       document verification + checksums + scoring (Phase 3.5)
  drafting.py     the four document generators (Phase 4)
  export_docx.py  draft -> real .docx
  feedback.py     house-style distillation with a safety guardrail (Phase 5)
  ocr.py          quality gate + Tesseract OCR fallback (Document Intelligence)
  doc_intelligence.py  the coverage-limit extraction fact (Document Intelligence)
  llm.py          the one place that talks to Ollama
  prompts/*.txt   every prompt, kept out of the code

  knowledge/      Phase 1 — Insurance Knowledge Core. entities.py, facts.py,
                   evidence.py, relationships.py, versions.py, and
                   service.py (the single interface everything else calls:
                   create_fact, reconcile_fact, get_current_value,
                   get_historical_values, etc). Every kc_* table and every
                   function here is organization-scoped from the schema up
                   — this package didn't repeat Phase 0's mistake of adding
                   isolation after the fact. See Results.md's Phase 1
                   section for the full writeup.
  property_policy_intelligence.py  the first Knowledge Core vertical slice:
                   extracts property/policy facts from a submission's
                   documents (Property Policy + Coverage Schedule +
                   Endorsement + Risk Survey) into Business/Asset/Policy
                   entities, with every value's evidence traceable back to
                   a real document and page. Reuses Phase 0's contradiction
                   logic rather than reimplementing it. Chunks long
                   documents instead of truncating at 12,000 characters,
                   verifies evidence snippets actually appear in the source
                   text, and carries a fact's own evidence page's OCR
                   confidence rather than the document average.
  policy_comparison.py  Phase 1B — coverage comparison. Diffs two
                   submissions' CURRENT Knowledge Core facts (e.g. this
                   year's renewal against last year's expiring policy):
                   increased/decreased/added/removed, fact type by fact
                   type. Pure read-side — never writes a fact, never
                   re-extracts, safe to call repeatedly. See "Policy
                   Intelligence" below.
  eval_policy_comparison.py  Phase 1B's acceptance benchmark — 13
                   deterministic test cases, no model involved (writes
                   facts directly through reconcile_fact() and checks
                   policy_comparison.py's output against exact expected
                   results). Currently 13/13 passing. See "Policy
                   Intelligence" below.

  kc_conflicts     a fact disagreement (contradiction) creates a
                   conflict row here, and the "current value" query for
                   that entity+fact_type refuses to pick a side while the
                   conflict is unresolved — no arbitrary "newest wins".
                   A human correction resolves it. See Results.md's
                   "Phase 1A hardening" section for the full writeup and
                   how each fix was verified.

frontend/
  src/views/  Upload . Carriers . Briefing . Clients . ClientDetail .
              Ask . Review . Health . Knowledge
```

**Data flow:** Postgres is the source of truth for everything -- carriers,
clients, matches, gap reports, drafts, the audit trail. ChromaDB holds only a
derived, rebuildable search index (Settings -> *Rebuild search index* restores
it from Postgres at any time with no model calls). The model itself is
stateless between requests; every call goes through `llm.py`, so swapping
Ollama for a hosted API later is a new sibling module, not a rewrite.

**Redaction boundary:** `tokenise.py`'s `Redactor` is the only thing that
turns real client data into placeholders and back. Every drafting and
matching function receives already-redacted text; nothing downstream of
`Redactor.redact()` ever sees a raw identifier.

---

## What's deliberately not here

- **No send path.** Nothing in this codebase emails, submits, or transmits a
  drafted document anywhere. That's a human action, always.
- **No fine-tuning.** Phase 5's "learning" is a short piece of editable text
  fed into a prompt, not a training loop.
- **No PDF export.** Word only -- it would mean LibreOffice in the backend
  image for ~500MB, and Word exports PDF in one click anyway.
- **No user accounts.** Approvers are identified by a typed name, recorded
  honestly as self-declared rather than backed by a real auth system that
  doesn't exist yet.

---

## Switching back to local after running the server version

If you've run `docker-compose.prod.yml` (or exported `PUBLIC_URL`,
`POSTGRES_PASSWORD`, `OLLAMA_HOST`, etc. in your terminal for it) and now
want to go back to running everything on your own machine with the plain
`docker-compose.yml`, two things need clearing — otherwise leftover shell
state silently overrides the local defaults.

**1. Unset anything you exported for the server run**, in the same terminal
you'll use to run the local version:

```bash
# macOS/Linux
unset PUBLIC_URL POSTGRES_PASSWORD OLLAMA_HOST CORS_ORIGINS OLLAMA_MODEL BACKEND_PORT FRONTEND_PORT
```

```powershell
# Windows PowerShell
Remove-Item Env:PUBLIC_URL, Env:POSTGRES_PASSWORD, Env:OLLAMA_HOST, Env:CORS_ORIGINS, Env:OLLAMA_MODEL, Env:BACKEND_PORT, Env:FRONTEND_PORT -ErrorAction SilentlyContinue
```

An exported variable stays set for the rest of that terminal session (and,
on Windows, sometimes across sessions if it was set as a *system* or *user*
environment variable rather than just in that PowerShell window) — Docker
Compose then uses that leftover value instead of the default baked into
`docker-compose.yml`, even though nothing in the file itself changed.

**2. Bring the containers down and back up on the plain compose file:**

```bash
docker compose down
docker compose up --build
```

(Not `docker-compose.prod.yml` — the file with no suffix.)

**If you're not sure whether something is still exported**, check before
running:

```bash
# macOS/Linux
echo $OLLAMA_HOST

# Windows PowerShell
echo $Env:OLLAMA_HOST
```

If either prints anything other than nothing/blank, that's a leftover export
overriding the default — unset it and re-run.

---

## Troubleshooting

**"Ollama is not reachable"** -- it runs on your machine, not in Docker.
`curl http://localhost:11434/api/tags` should return JSON.

**Error mentions a bare address like `0.0.0.0` with "missing a protocol"** --
`OLLAMA_HOST` is set to something without `http://` in front of it, almost
always a leftover shell export from an earlier server run. See "Switching
back to local" above -- unset it and restart.

**Matching/Ask says the index is empty but carriers are briefed** -- briefing
and indexing are separate steps. Check Settings for the real cause (usually
`nomic-embed-text` not pulled), fix it, then *Rebuild search index*.

**A carrier's briefing finds no requirements** -- its documents may not
actually be policy paperwork, or the parse produced too little text.
Regenerate the briefing; if it's still empty, check what was actually
extracted at `GET /documents/{id}/text`.

**Doc Intelligence upload fails immediately on any PDF** -- if the error
mentions Tesseract not being found, the backend image needs rebuilding after
the OCR packages were added: `docker compose up --build` (not just `up`),
since `docker compose up` alone reuses an already-built image.

**Ports clash with another project** -- everything's remapped off the common
defaults (5183 / 8100 / 5442 / 8101 instead of 5173 / 8000 / 5432 / 8001)
specifically so this can run alongside something else.

## v0.8.2 — deterministic entity normalization (2026-10-02)

`knowledge/entities.py` now normalizes names (case, punctuation, "&"/"and",
legal suffixes such as Pvt Ltd / Private Limited / LLP / Inc) before matching.
`get_or_create_entity`: exact name wins; otherwise exactly one normalized
match (name or recorded alias) is reused and the new spelling is stored in
`attributes.aliases`; two or more matches are ambiguous, so a new entity is
created rather than guessing. Never merges across entity types or tenants.
`find_possible_duplicates` lists look-alike pairs (token overlap >= 0.6) for
human review; nothing is merged automatically. No fuzzy auto-merge, no schema
change. Tests: 93/93 (5 new). Not run against real Qwen; it does not touch
extraction or scoring.

## v0.8.3 — coverage must be named as cover (2026-10-02)

The v0.8.1 real-Qwen `--limit 3` check failed the hard gate with 2 silently
wrong facts: in AUR-SYN-002 and AUR-SYN-003 the model read the endorsement line
"Plant and machinery values reviewed. Revised machinery declared value: ..." as
a `policy.coverage`. Every benchmark endorsement has this line, so the risk is
in all 20 companies. New rule `coverage_named_as_cover`: a coverage whose value
appears in the source but never on a line (or the line after) mentioning
cover / peril / section / sum insured / limit of indemnity is dropped
(`dropped_coverage_not_named_as_cover`). Deterministic check on the benchmark:
40/40 ground-truth coverages kept, the note rejected in 20/20 endorsements.
Scoring unchanged. Tests 98/98 (5 new).

## v0.9.0 — Knowledge explorer (2026-10-02)

On the Knowledge Core screen, below the analysis panel (opens on the analysed
business automatically):

- pick any entity (filter by type); click a relationship chip to move to the
  linked entity (business → policy → back);
- facts grouped by type, each with one label: Current, Historical, Needs
  review, Verified, Contradiction, Superseded; its stated policy period; and
  its evidence (document · page · quote);
- "View page" loads the document's real page text with the quoted line
  highlighted;
- "In force on" date: what applied on that day; undated facts are listed as
  not assumed in force;
- Verify / Reject with a reviewer name (backend rules unchanged: no verify
  without evidence, contradictions are settled by Correct, rejected facts are
  kept for audit);
- possible duplicate names, collapsed, review only, never merged.

Backend: `GET /knowledge/entities?entity_type=`, `GET /knowledge/possible-duplicates`;
historical facts in `/knowledge/entities/{id}/knowledge` now carry evidence.
`find_possible_duplicates` narrowed to "one name is the start of the other" after
word-overlap flagged every baseline/renewal pair in the comparison benchmark.
No extraction or scoring change, so no new Qwen run is needed for this release.

## v0.10.0 — Knowledge query layer (Phase 1B.5, 2026-10-06)

**Audit first (what existed):** vector search covers only CARRIER wordings
(Chroma, `vectorstore.py` / `chat.py`); client/submission documents are not
embedded. Policy comparison diffed current facts but returned no evidence and
no percentage. Nothing answered questions from the Knowledge Core.

**New: `knowledge/query.py` + `POST /knowledge/query`**
(`{question, entity_id?, as_of?, use_llm?}`).

1. *Interpret* (no model): fact type from the wording ("coverage limit",
   "deductible"/"excess", "exclusions", …), mode (current / as of a date named
   in the question / previous / change at renewal / policy period).
2. *Entity*: `entity_id`, or a business whose name or alias is in the question;
   two matches -> abstain with candidates. Follows `owns` / `has_policy` to the
   asset or policy that holds the fact.
3. *Answer deterministically*: a stated period covering the day wins; periods
   stated but none covering the day -> abstain (an expired policy is not
   "current"); undated facts answer a current question but are never assumed
   to apply on a date the question names; contradictions, or two different
   current values -> abstain and show both with evidence; facts without
   evidence are never used; rejected facts are never used.
4. *Change / previous*: compares the last two stated periods: old -> new,
   absolute and % change (singletons), added/removed (multi-valued), evidence
   for both sides.
5. *Fallback, only when no fact type applies*: keyword retrieval over this
   tenant's documents for the entity; optional local-model answer accepted only
   if its quote is in the cited passage AND every number in the answer and the
   quote is in that passage; otherwise abstain. Such answers are always
   marked unverified.

Response: `answer, answer_type, values, facts_used, entities_used,
relationships_used, evidence, source_documents, source_pages,
effective_period, review_status, authoritative, confidence, contradictions,
needs_review, reason, change, retrieved_passages, candidates`.
`authoritative` is true only for verified facts (and deterministic policy periods).

**Policy comparison:** each compared value now carries evidence; numeric
changes carry `absolute_change` / `percent_change`; `assessment` =
evidenced / contradictory / unknown / no_evidence. `change` is unchanged, so
the 13/13 benchmark is unaffected.

**Knowledge explorer:** an "Ask the Knowledge Core" box on the entity view.

**Real-Qwen check:** `eval_property_policy.py ... --query-check` asks one
question per ground-truth fact type against the facts the real model just
extracted and grades answers: correct / abstained / wrong (wrong must be 0).
Fact scoring is unchanged.

**Measured so far (no model):** 125/125 tests (25 new, covering the reviewer's
20 cases plus expiry, policy period, API, grading); comparison 13/13;
ground-truth simulation on AUR-SYN-001–004 40/40 answers correct with
evidence. **Not yet measured:** the same check on real Qwen extraction.

**Known limits:** question interpretation is phrase-based (English, insurance
wording above); multi-period comparison uses the last two stated periods;
retrieval fallback is keyword-only over client documents (carrier wordings
stay on the existing vector `/chat`); tenant isolation remains application-level.
See `backend/evaluations/phase1a/INDEX.md` for which result is historical,
current or pending.

## v0.10.1 — fixes from the first real-Qwen query check (2026-10-06)

Run: `--only AUR-SYN-001,AUR-SYN-002,AUR-SYN-003,AUR-SYN-004 --query-check`
(result-20261006T162353 on the developer's machine). The query layer
interpreted 40/40 questions correctly and attached evidence to every answer;
the failures came from extraction:

1. **1 silent-wrong fact + 1 wrong answer (AUR-SYN-002):** the model wrote an
   endorsement as "Endorsement SYN-END-0002-01: Additional named insured …",
   gluing the header line onto the change text — a string that exists nowhere
   in the document. Fix: `clean_endorsement_value` strips leading
   "Endorsement <ref>:" / "Change N:" labels (diagnostic `endorsement_label_stripped`).
2. **Coverage limit abstained in 4/4 companies** (safe, but useless): the
   model read "Revised machinery declared value: 90,000,000" (endorsement) or
   a schedule's per-category declared value as `policy.coverage_limit` /
   `property.sum_insured`, which the Knowledge Core flagged as a contradiction.
   Fix: `numeric_on_limit_line` — if the number is in the source, a line it is
   on (or the line before) must mention sum insured / limit / indemnity /
   cover / insured value (diagnostic `dropped_number_not_stated_as_limit`).

Checked without the model on all 20 companies (app parser): 60/60
ground-truth limits & sums insured kept, machinery value rejected in 40/40
places, endorsement cleanup 40/40. Scoring unchanged. The occupancy restatement
from risk surveys still abstains as a contradiction (known, safe).

## v0.10.2 — Phase 1B audit and hardening (2026-10-06)

Audit, fixes, test counts, security findings and the open validation step are
in **`PHASE_1B_STATUS.md`**. In short:

- Corrections: correcting a historical (last-year) value stays historical; a
  correction carries the corrected fact's evidence (`source=reviewer_correction`),
  so the query layer uses the corrected value instead of falling back to the
  corrected-away one; re-correcting an already replaced value is refused.
- Query routing: no bare "limit"/"built"; property location is not used for
  insurer/broker/agent/office questions; "what X were added" lists X instead of
  diffing periods; a qualifier ("deductible for flood") makes a singleton
  question abstain with the policy-wide value as a candidate, and turns a
  multi-valued question into a yes-with-evidence / "not among the recorded
  values" check. Questions are capped at 1,000 characters.
- `--query-check` adds 7 safety probes per company on real extracted facts
  (policy period, date before any period, previous with one period,
  peril-specific deductible, not-listed coverage, misleading "time limit",
  cross-tenant query). A failed probe fails the hard gate.
- Tenant isolation: every Knowledge Core / query / comparison / document
  endpoint tested from another organization (reads and writes) — no leaks.
  Application-level, not PostgreSQL RLS.

Phase status: **NOT READY until the v0.10.2 real-Qwen 4-company run passes**;
the 20-company acceptance is deliberately deferred.

## v0.10.3 — last Phase 1B fix (2026-10-07)

The v0.10.2 real-Qwen run (AUR-SYN-001–004) had 0 silent-wrong facts and 0
wrong answers but 1 failed safety probe: after an Ollama HTTP 500 cut short
AUR-SYN-001's policy extraction, the coverage limit existed only undated (from
the schedule) and was given for a date before the policy began. Fix: an undated
value of a period-bound fact type is never given for a day outside every policy
period known for the entity. New `recheck_query.py <dataset> <organization_id>`
re-grades the query layer and safety probes on facts already stored by a run —
no model, seconds. Recheck on 2026-10-07: 0 wrong answers, 28/28 probes passed. **Phase 1B status: READY FOR PHASE 1B ACCEPTANCE (4-company development benchmark; 20-company acceptance deferred).** Details: `PHASE_1B_STATUS.md`.

## v0.11.0 — Phase 1C Policy Intelligence (2026-10-07)

Built on the Phase 1B Knowledge Core (nothing rebuilt). Full audit, models,
APIs, validation and limits: **`PHASE_1C_STATUS.md`**.

- `policy_structure.py` — deterministic reader: document role, policy number,
  insurer, conditions, definitions, sub-limits, per-coverage limits, endorsement
  id / policy reference / effective date. Labelled lines only; never the model.
- `analyze_submission` writes them into the Knowledge Core (coverage and
  document entities, `provides` / `supports` links; a document naming another
  policy number is not attached). Phase 1A scoring is unchanged.
- `policy_intelligence.py` + `api/policies.py` — `GET /policies`,
  `GET /policies/{id}?as_of=`, `/history`, `/conflicts`, `POST /policies/compare`.
- Query layer: insurer, number, conditions, definitions, sub-limits, a named
  coverage's limit, highest/lowest-limit coverage, coverages in contradiction,
  endorsements effective in the period, in force / expired, what changed.
- Guards: a sub-limit line is never a coverage limit or sum insured; a
  percentage deductible is never turned into an amount.
- `--query-check` now also prints a **policy check (Phase 1C)** gate;
  `recheck_query.py` re-grades it on stored facts without the model.

Phase 1C was validated using the 3–4 company development benchmark (real Qwen,
2026-10-08: 0 silent wrong, 0 wrong answers, policy check 28/28, all probes
passed). Large-scale 20-company acceptance is deferred.

## v0.12.0 — Phase 1D Business Risk Intelligence (2026-10-08)

Built on the Knowledge Core (nothing rebuilt, no Neo4j). Full design, rules,
validation and limits: **`PHASE_1D_STATUS.md`**.

- `business_structure.py` — deterministic reader of what a document *states*
  about the business: locations, operations, workforce, surveyor risk rating,
  controls, declared asset values, loss count / total incurred, legal name,
  risk factors, explicit risks, explicit exposures. Never the model, never
  inferred from industry.
- New entity types (business_unit, operation, exposure, risk_factor, control)
  and relationships (has_location, performs_operation, has_control, has_risk,
  mitigated_by, insures, …); one DB migration widens the entity-type check.
- Every value is an *observation* dated by its document (survey / report /
  endorsement date, or policy start). Later observation = current, earlier =
  history; two different values on the same date = contradiction.
- `business_intelligence.py` + `api/businesses.py` — `GET /businesses`,
  `/{id}`, `/profile?as_of=`, `/locations`, `/assets`, `/operations`,
  `/exposures`, `/risks`, `/controls`, `/claims`, `/changes?from_date=&to_date=`,
  `/conflicts`, `/graph`. Another organisation's id → 404.
- Questions: business profile, locations, operations, assets, controls, losses,
  exposures, risks, "what changed", contradictions. "Is there flood risk?" with
  nothing stated → abstains ("No documented evidence…").
- Explorer: Business panel on business entities (profile with evidence,
  changes, conflicts).
- Dev supplement (synthetic, labelled): `backend/benchmark_phase1d/` — follow-up
  survey / loss run for AUR-SYN-001–004 to test change, no-change and
  contradiction, because the benchmark has one period and no stated risks.
- `--query-check` prints a 4th gate, **business check (Phase 1D)**.

Phase 1D validated on the 3–4 company development benchmark (real Qwen,
2026-10-08: 0 silent wrong, 0 wrong answers, business check 53/53, all probes
passed); 20-company acceptance deferred.

## v0.12.1 — Phase 1D final acceptance audit (2026-10-09)

No implementation change. Adds `backend/phase1d_acceptance_audit.py`: a
read-only, no-model audit of the real-Qwen run's stored business data —
evidence and page of every business fact, nothing invented (risks, exposures,
controls, claims, assets, locations), relationships and graph, as-of history,
contradictions, change detection, every `/businesses` API for the owner and
another tenant, no risk score, unsupported-question abstention. 198 checks on
AUR-SYN-001–004.

    docker-compose exec backend python3 phase1d_acceptance_audit.py /app/benchmark_data <organization_id of the real-Qwen run>

Result (2026-10-09, real-Qwen run's data): **198/198 passed**, 80 business facts
evidence-verified 100%, page accuracy 100%. **Phase 1D accepted using 4-company
validation — frozen. Ready for Phase 1E.**

## v0.13.0 — Phase 1E Risk → Coverage Intelligence (2026-10-09)

Built on Phase 1C (policies) and Phase 1D (business risk) — nothing rebuilt,
no graph database. Full design, rules, validation and limits:
**`PHASE_1E_STATUS.md`**.

- `coverage_intelligence.py` — coverage map, findings, changes, audit, graph,
  review; computed on read from the Knowledge Core.
- `api/coverage.py` — `/businesses/{id}/coverage-map | coverage-findings |
  unmapped-risks | coverage-conflicts | coverage-changes | coverage-audit |
  coverage-graph`, `POST /coverage-findings/{id}/review`,
  `POST /coverage-mappings/{id}/review`.
- One new table `kc_coverage_reviews` (broker decisions, append-only).
- Knowledge Core fix: a value for an earlier policy period read after a
  renewal is now checked against that period (was silently stored as history).
- `--query-check` prints a 5th gate, **coverage check (Phase 1E)**; expected
  outcomes in `backend/benchmark_phase1e/`.

Phase 1E validated on the 4-company development benchmark (real Qwen,
2026-10-10: 0 silent wrong, 0 wrong answers in all five gates, coverage check
33 correct + 3 correct abstentions on AUR-SYN-004's sum-insured conflict).
**Accepted using 4-company validation and frozen (2026-10-10).** 20-company validation not claimed.

## v0.14.0 — Phase 2A Broker Workspace (2026-10-11)

**Sign-in is now required for everything.** Create the first Owner once:

    docker-compose up -d --build backend
    docker-compose exec backend python3 manage_users.py create-owner --email you@firm.in --name "Your Name" --org-id 00000000-0000-0000-0000-000000000001

(`--org-id` of the existing default organization keeps every client and
document you already have; use `--org "New Brokerage"` for a fresh one. The
password is asked for and never shown). Everyone else joins through
Team & security → Invite (a one-time code you hand over). Roles:
Owner / Admin (everything; only an Owner manages Owners), Broker (clients,
documents, analysis, drafts, export), Reviewer (verify/correct facts, review
findings, approve drafts, export). The organization comes from the signed-in
membership; `X-Organization-Id` can no longer choose it. Set
`AUTH_COOKIE_SECURE=true` when the site is served over HTTPS. Details,
security findings and limits: **`PHASE_2A_STATUS.md`**.

