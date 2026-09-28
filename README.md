# Aurevia BrokerOS

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
tested; real-model extraction quality partially verified, one more
confirmed run still needed before calling this "accepted."**
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
  extracts all ten fact types correctly from real documents — has a real
  local run on record showing **zero silently-wrong facts, 67.5% fact
  recall, 1.7% false-extraction rate** across all 20 synthetic companies
  (`backend/eval_property_policy.py`). The zero-silent-wrong-facts result
  is the hard gate and it holds; 67.5% recall means roughly a third of
  ground-truth facts are not yet being extracted, which is not the same
  claim as "100% extraction accuracy" and should not be read as one. See
  `Results.md`'s Phase 1A section for that run's full detail, the dataset
  gap that was found and fixed along the way (some fact types weren't
  actually stated in the source documents for the model to find — a
  benchmark bug, not a model bug), and what's still open before this can
  be called formally accepted: **one more confirmed run against real,
  reachable Ollama/Qwen** (not a stub server) with the current dataset,
  its result recorded with the model name and reachability explicitly
  logged, superseding every earlier, now-historical result file.

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
