# Phase 0 Evaluation Results — Aurevia Synthetic Insurance World v1

Run against all 20 companies in the dataset, through the real pipeline
(`parsers.parse_file_with_quality` → `doc_intelligence.extract_coverage_limit`
— the same code `/doc-intelligence/upload` calls in the running app).

**The numbers below are from a real, complete run** — real `qwen2.5:7b` via
local Ollama, real Tesseract OCR, real Poppler. `backend/eval_results.json`
is that exact run's full per-company output; nothing in this repo is a
stand-in or simulation. (An earlier draft of this evaluation used a
regex-based stand-in for the model before a real Ollama instance was
available — that run and its output file no longer exist in this repo,
superseded entirely by the real one described here.)

**Harness script (re-runnable):** `backend/eval_harness.py`

---

## Read this before the numbers

One thing worth knowing about how this run got here: the first real attempt
came back with `ocr_trigger_accuracy_pct: 0.0` and 6 documents returning
nothing at all. That traced to Tesseract and Poppler not actually being
installed on the machine running the harness — Windows needs both as system
binaries, not just the Python wrapper packages `pytesseract`/`pdf2image`
call into. Once both were installed and put on PATH, the same run produced
the numbers below. Worth remembering if this harness is ever run on a fresh
machine: a clean pip install is not enough, and a broken OCR setup produces
a real result (correctly refusing to guess), not an error — easy to mistake
for a model-accuracy problem when it is actually an environment problem.

## A real finding worth flagging: the model's confidence scores barely vary

Across the 14 companies that used the model path (rather than the
deterministic rule), `qwen2.5:7b`'s self-reported confidence was **0.90 for
13 of them, and 0.95 for the remaining one** — checked directly against
`eval_results.json`, not assumed. On a dataset this clean, that is a
plausible outcome on its own. But it is also exactly the failure mode the
schema's design already warned about: *"an LLM's stated plausibility, never
a calibrated probability."* A model reporting near-identical high confidence
across genuinely different documents is not distinguishing "I am sure"
from "I am somewhat sure" in any way a reviewer could act on — the number
is present, but it is not doing useful discriminating work yet.

**This does not need fixing before anything else** — it does not affect
correctness in this run, and the confidence-type labelling already tells a
reviewer this number is a model's opinion, not a measurement. It is worth
watching once the deterministic regex covers fewer cases (real carrier
wording will vary far more than this synthetic set), since a model that
always says "0.9" is not actually helping anyone prioritise review queues
by confidence.

---

## Headline numbers (real run — `qwen2.5:7b`, real OCR)

| Metric | Result |
|---|---|
| Companies scored | 20 / 20 |
| Value accuracy | 100.0% |
| Evidence page accuracy | 100.0% |
| A fact was found at all | 100.0% |
| OCR triggered correctly | 100.0% (6/6 scanned-variant companies) |
| Silent guesses detected | 0 |
| Extraction method split | 6 deterministic-rule, 14 real model (`model_self_reported`), 0 OCR-engine-confidence, 0 null |

This is a genuinely clean result on this dataset — real model, real OCR, no
stand-in. It is still not the whole picture: see the two structural
limitations below (contradiction detection, and how easy this dataset's OCR
cases actually are), which are about what the dataset and current code do
and don't test, not about the trustworthiness of the numbers above.

---

## Real findings — things that need attention, in priority order

### 1. Contradiction detection — was missing, now built and verified against all 4 real cases

The dataset deliberately builds 4 companies (`AUR-SYN-004`, `AUR-SYN-009`,
`AUR-SYN-015`, `AUR-SYN-019`) where the property policy states one sum
insured and the coverage schedule states a different one — testing exactly
the acceptance criterion: *"Cross-document contradiction: Do not silently
merge conflicting values; flag for review."*

**Status: fixed.** Three pieces were added:

- A `submissions` table (`db.py`) — a named group of documents believed to
  belong to the same case. This did not exist before: every upload was
  previously its own unrelated document with no way to express "these five
  files are the same company." `POST /doc-intelligence/submissions` creates
  one; `POST /doc-intelligence/upload` now takes an optional
  `submission_id` to attach a document to it.
- `doc_intelligence.find_contradiction()` — after a new fact is extracted,
  it is compared against every other fact already on record for the same
  submission (skipping facts from the same document). Two values are
  treated as agreeing if they are within 1% of each other, which absorbs
  rounding/formatting differences without missing a real mismatch. Values
  are compared numerically after stripping currency symbols and formatting
  (`"₹350,000,000"` vs `"INR 350,000,000"` correctly agree).
- `db.flag_contradiction()` — when a mismatch is found, **both** facts are
  moved to a new `contradiction_flagged` status (not just the one uploaded
  second), each carrying a note naming the other document, its value, and
  the percentage difference. A reviewer opening either fact sees the
  dispute.

**Verified against real data, not just unit tests.** Ran all 4 deliberate-
contradiction companies through the real `/doc-intelligence/submissions` +
`/doc-intelligence/upload` endpoints (a stand-in extraction model was used
for this check, same caveat as the confidence-clustering finding below —
the comparison LOGIC itself does not depend on which model produced the
values being compared):

| Company | Policy value | Schedule value | Flagged? |
|---|---|---|---|
| AUR-SYN-004 | ₹350,000,000 | ₹370,000,000 | ✅ both facts `contradiction_flagged` |
| AUR-SYN-009 | ₹400,000,000 | ₹420,000,000 | ✅ both facts `contradiction_flagged` |
| AUR-SYN-015 | ₹400,000,000 | ₹420,000,000 | ✅ both facts `contradiction_flagged` |
| AUR-SYN-019 | ₹250,000,000 | ₹270,000,000 | ✅ both facts `contradiction_flagged` |

**False-positive check, also verified:** ran `AUR-SYN-001` (no deliberate
contradiction in its ground truth) through the same two-document flow —
policy and schedule both extracted ₹600,000,000, and correctly stayed
`needs_review` with no contradiction flagged. The 1% tolerance does not
fire on agreement.

**What this does not yet cover:** `eval_harness.py`'s main per-document loop
still calls `doc_intelligence.extract_coverage_limit()` directly, one
document at a time, and does not go through the `/doc-intelligence/upload`
endpoint — so the extraction accuracy numbers above don't themselves
exercise the submission/upload wiring. Contradiction detection specifically
is now covered separately (see next paragraph): the harness calls
`doc_intelligence.find_contradiction()` directly with the same fact shapes
the real endpoint builds, which tests the actual comparison logic without
needing a running server.

**Now covered by the automated run, not just the manual check above.**
`eval_harness.py` runs a dedicated contradiction check for all 4 test
companies plus a negative control (checking that a genuinely agreeing pair
is never flagged), reported in its own section of `eval_results.json`
(`contradiction_checks`) and summarised as `contradiction_accuracy_pct` and
`contradiction_false_positives`. Latest run: **4/4 caught (100%), 0 false
positives** — matching the manual verification above exactly, now
re-runnable on demand rather than a one-off check.

### 2. The regex used for the deterministic extraction path was too narrow — partially fixed, one risk found and reverted

The dataset's actual property policy wording is `"Property Damage Sum
Insured: n600,000,000"` — a specific label the original
`doc_intelligence._CURRENCY_AMOUNT` regex did not anticipate (it only
matched a bare `"sum insured"` substring), and a broken currency glyph
(`n` instead of `₹`, a font-encoding artifact in this dataset's PDF
generation — confirmed by inspecting the raw extracted text, not assumed).

**Fixed:** the label alternation now explicitly matches "Property/Building/
Contents/Stock Damage Sum Insured", "Insured Amount", and a few other
common phrasings, not just a bare "sum insured" substring.

**Found and reverted:** the first attempt also added `n` as an accepted
currency prefix, to catch the glyph issue. Testing this immediately found a
false positive — ordinary text like *"...continues on n 2024 filings..."*
matched and was scored with **confidence 1.0 (deterministic)**, which is
worse than missing the real value, because a deterministic score claims
certainty a plain letter cannot support. This was reverted. Documents with
the `n`-glyph rendering issue now correctly fall through to the model path
instead of the regex path — the model reads them as ordinary text with no
special-casing needed, since it never has to treat the broken symbol as a
special currency character in the first place. **This is the
better fix.** The updated regex and this note are already in
`doc_intelligence.py`.

### 3. OCR was not actually stress-tested

All 6 "scanned" documents in this dataset are computer-rendered text turned
into an image, not a real photographed or faxed document. Tesseract read
them at high confidence with no skew, no shadow, no handwriting, no
coffee stains. **This is a much easier OCR case than what a real broker will
upload.** The 100% OCR-trigger accuracy is real (the quality gate correctly
detected "this PDF has no text layer" every time), but it says nothing about
extraction *accuracy* on a genuinely poor scan.

**Suggested addition to a v2 of this dataset:** a handful of documents run
through an actual noise/blur/rotation pipeline, or real photographs of
printed pages, to see where OCR confidence actually starts dropping and
whether the `LOW_CONFIDENCE_THRESHOLD = 60.0` cutoff in `ocr.py` is set
sensibly.

### 4. Cross-organization isolation (P0-06) and persistence (P0-07) — untested by this harness

The acceptance test list includes two scenarios this script does not touch
at all: uploading as a second organization and confirming no visibility into
the first org's data, and confirming facts/review state survive a restart.
Both are testable today against the running app (not this offline script) —
they need a second `organization_id` created and a container restart, which
is out of scope for a pure extraction-accuracy harness. **Worth a separate,
short manual test pass before calling Phase 0 done**, not a coding gap.

---

## Test 1 — Multi-organization isolation: fixed, then verified

**Before this could even be tested, it had to be built.** Every
doc-intelligence endpoint was hardcoded to a single default organization —
there was no way to act as a second broker at all, so "does Org B see Org
A's data" was not a test that could be run; it was a missing feature. Fixed
with an `X-Organization-Id` request header, resolved once per request
(`main.py`'s `_resolve_organization`) and checked in the database query
itself for every read and write — `db.get_submission`, `db.get_extraction_fact`,
`db.list_submission_documents`, `db.list_submission_facts`, and
`db.review_extraction_fact` all now require and filter on `organization_id`,
rather than fetching by id alone and checking afterward. A submission or
fact belonging to a different organization now returns the same 404 as one
that does not exist — a caller cannot even use the error message to tell
"not yours" apart from "never existed."

**Verified with real HTTP calls against the real running app, not
inspected as code:**

| Check | Result |
|---|---|
| Org B lists submissions — should not see Org A's | ✅ empty list |
| Org B fetches Org A's submission by ID directly | ✅ 404 |
| Org B fetches Org A's fact by ID directly | ✅ 404 |
| Org B attempts to **correct** Org A's fact | ✅ 404 (not a silent success) |
| Org A can still access its own data (control) | ✅ 200 |
| No header sent at all still works (control — the whole existing UI and every earlier test in this file relies on this) | ✅ 200, default organization |

The fourth row is the one that mattered most to get right — a read
returning nothing is a minor leak; a write silently succeeding against
another organization's data would not be.

**Still application-level, not database-enforced.** This is a correctly
implemented boundary, not Postgres row-level security — a caller that sends
an `X-Organization-Id` it should not have is still trusted by this layer.
That is the documented pilot-stage decision, not a gap introduced here; what
changed is that the boundary is now actually enforced everywhere, rather
than not existing at all.

**Follow-up: the isolation above was real but invisible — now it's usable.**
The table above was verified with raw HTTP calls; there was no way for an
actual person to try this in the app itself. Added an "Acting as"
organization switcher to the Doc Intelligence screen — create an
organization, switch to it, and every request `api.js` makes from then on
carries that organization automatically (nothing else in the app needed to
change). Verified with a real browser test, not just the backend calls
above:

| Step | Result |
|---|---|
| Create "Broker A" through the UI | ✅ created, auto-selected |
| Create a submission while acting as Broker A | ✅ created, visible in the list |
| Create "Broker B" through the UI (auto-switches to it) | ✅ switched |
| Submission list after switching to Broker B | ✅ empty — Broker A's submission is gone from view |

Screenshots of all four steps taken during development. The switch also
clears the on-screen document list immediately, rather than risk showing a
fetch error where the previous organization's data used to be.

---

## Test 2 — Persistence across a restart: verified, no code changes needed

Uploaded a real document, extracted a fact, reviewed it (to exercise the
audit trail specifically, the part most likely to be silently lost if
anything were held only in memory), recorded the exact state, then **killed
the running backend process entirely** and started a completely fresh one
against the same Postgres data.

| Checked after restart | Result |
|---|---|
| Organization | ✅ present |
| Submission | ✅ present, correct label |
| Extracted fact value | ✅ unchanged: `₹600,000,000` |
| Review status | ✅ unchanged: `verified` |
| Audit trail (history) | ✅ 1 event intact, with reviewer name and timestamp |

Expected to pass, since Postgres was always the source of truth and nothing
in this app holds state only in memory — but now actually proven against a
real killed-and-restarted process rather than assumed from reading the code.

---

## Test 3 — Hard OCR stress test: real degraded documents, one honest caveat

The dataset's existing "scanned" companies are clean computer-rendered text
turned into an image — useful for proving OCR triggers correctly, useless
for testing OCR *accuracy* under real conditions. Built 11 variants from one
real source page instead, each a genuine image-level degradation (not
merely relabelled): 3–8° rotation, moderate and heavy Gaussian blur, 65%
contrast reduction, one-third resolution, a diagonal shadow gradient,
Gaussian sensor noise, an affine perspective skew, and a combined
multi-distortion "phone photo" case — every non-baseline variant also passed
through JPEG recompression at a random quality between 35–60, since a real
scan is essentially never a pristine lossless image by the time it reaches
an inbox. Ran all 11 through the actual pipeline
(`parsers.parse_file_with_quality` → `doc_intelligence.extract_coverage_limit`).

| Variant | OCR confidence | Result |
|---|---|---|
| Clean baseline | 88.7% | ✅ correct |
| Rotated 3° | 81.2% | ✅ correct |
| Rotated 8° | 0.0% | Abstained — genuine failure, 8 chars recovered |
| Blur (moderate) | 34.7% | Abstained — genuine failure, OCR output was gibberish |
| Blur (heavy) | 0.0% | Abstained — genuine failure, 8 chars recovered |
| Low contrast | 80.9% | ✅ correct |
| Low resolution | 26.5% | Abstained — genuine failure, 16 chars recovered |
| Shadow band | 87.2% | ✅ correct |
| Sensor noise | 28.4% | ✅ correct — borderline; confirmed below |
| Perspective skew | 55.5% | ✅ correct — confirmed against the real model, see below |
| Multi-distortion phone photo | 0.0% | Abstained — genuine failure |

**Re-run against the real `qwen2.5:7b` (local Ollama) — caveat closed.**
Every earlier run of this test used a stand-in for the extraction model,
which left one genuine open question: was `perspective_skew` a real OCR
failure, or an artifact of the stand-in's rigid regex? That question is now
answered. Run against the actual local `qwen2.5:7b`
(`Model check: OLLAMA_MODEL='qwen2.5:7b', reachable=True` — confirmed
reachable, not assumed), `perspective_skew` now **extracts correctly**:
`"INR'500,000,000"`, OCR confidence 55.5%, `confidence.type = ocr_engine`.
This confirms the hypothesis exactly as predicted — the corrupted currency
prefix (`INR'` instead of `INR`) that defeated the stand-in's exact-match
regex was trivially readable by an actual language model, which is exactly
the kind of fuzzy recognition a real model is for and a rigid pattern match
isn't.

**Final real-model tally: 6 correct, 5 correctly abstained, 0 silent wrong
answers, 0 parse failures**, across all 11 variants, with the model
confirmed reachable for the entire run. The remaining five abstentions
(`rotated_8deg`, `blur_moderate`, `blur_heavy`, `low_resolution`,
`multi_distortion`) recovered only 0–22 characters of genuine gibberish —
real failures regardless of which model reads them, and correctly reported
as abstentions rather than guesses.

**The number that matters most, now doubly confirmed: zero silent wrong
answers.** Across the stand-in runs and this real-model run, no case ever
produced a high-confidence wrong figure — every case that couldn't be read
correctly abstained instead of guessing. That is the actual property
Phase 0's "no silent guessing" rule exists to guarantee, and it now holds
under real degraded input against the real production model, not just a
stand-in.

**What this establishes and what it doesn't.** It establishes that
degraded-but-legible scans (mild rotation, moderate contrast loss, a shadow,
sensor noise, and now perspective skew) are handled correctly by the real
model, and that badly degraded scans fail safely rather than silently
wrong. It still does not establish a statistically meaningful production
accuracy *rate* — a sample of 1 source document and 11 synthetic
degradations is a stress test, not a large benchmark — but the specific,
previously-open question (real model vs. stand-in artifact) is now closed.

**Follow-up note (from the prior stand-in runs, still in effect): the
script states which model actually ran, in the output itself.**
`run_hard_ocr_test.py` checks `llm.is_available()` before running anything,
prints an explicit warning if the model is not reachable, and records
`model_used`, `model_was_reachable`, and
`run_is_authoritative_for_model_path_results` directly in
`hard_ocr_results.json`. This run's output file carries
`model_used: "qwen2.5:7b"`, `model_was_reachable: true` — so this specific
results file is verifiably the real-model run, not a stand-in artifact
mislabeled as one.

Reproducible: `python3 build_hard_ocr_set.py <path-to-source-pdf>` then
`python3 run_hard_ocr_test.py`, full detail in `hard_ocr_results.json`.

---

## What to actually do with this

1. ~~Get a real Ollama instance in front of this harness and re-run it.~~
   **Done.** The numbers above are from a real `qwen2.5:7b` run.
2. ~~Build contradiction detection.~~ **Done and verified against all 4
   real contradiction cases in this dataset**, plus a false-positive check
   against a clean company. See item 1 above.
3. The deterministic regex fix (item 2 above) is already in the codebase.
4. ~~Treat this dataset's OCR cases as "proves the wiring works," not
   "proves OCR is production-ready."~~ **Done** — see Test 3 above for a
   genuine degraded-scan stress test, now re-run and confirmed against the
   real `qwen2.5:7b`. Both borderline cases (`sensor_noise`,
   `perspective_skew`) are confirmed correct extractions under the real
   model, not stand-in artifacts.
5. ~~Manually walk through P0-06 (cross-organization isolation) and P0-07
   (persistence across restart).~~ **Done** — see Test 1 and Test 2 above.
   P0-06 required a real fix (organization scoping did not previously
   exist), not just a test.
6. ~~Extend `eval_harness.py` to cover contradiction detection
   automatically.~~ **Done** — see `contradiction_checks` in
   `eval_results.json` and the summary fields `contradiction_accuracy_pct`
   / `contradiction_false_positives`.
7. ~~A submission-picker UI... Not yet done: an equivalent for P0-06/P0-07
   (org isolation, persistence) has no UI either.~~ **The org-isolation half
   is now done too** — see the organization switcher added to Doc
   Intelligence, verified with a real browser test above. Persistence has
   no UI equivalent by nature (there is nothing to click to test a
   restart) and remains a manual check, which is the correct way to test it.
8. ~~Re-run Test 3 against a real, reachable `qwen2.5:7b` to close the
   `perspective_skew` caveat for real.~~ **Done.** Run against the actual
   local `qwen2.5:7b` (confirmed reachable), `perspective_skew` now extracts
   correctly. Final tally: 6 correct, 5 correctly abstained, 0 silent wrong
   answers, 0 parse failures — see Test 3 above.

**All eight items on this list are now closed.** Per the most recent
outside review, this was the last item standing between the current state
and formally freezing Phase 0. See "Phase 0 status" below for what that
means and what comes next.

---

## Phase 0 status: ready to freeze

Every acceptance-criterion gap identified across three rounds of outside
review is now closed: contradiction detection (built and verified against
all 4 real cases), multi-organization isolation (built — it didn't exist
before — and proven via both real HTTP calls and a real browser UI test),
persistence across a process restart (proven against a real kill/restart),
and the hard-OCR stress test (built from genuine image-level degradations
and now run against the real `qwen2.5:7b`, zero silent wrong answers).

The next real decision is not another Phase 0 fix — it's whether to start
scoping **Phase 1 — the Insurance Knowledge Core**: turning
`documents + extracted facts` into structured, versioned insurance
knowledge with entities and relationships, per the outside review's
suggested shape:

```
Document → Evidence → Insurance Facts → Entities → Relationships
   → Policy/Coverage Knowledge → Reasoning → Broker Workflow
```

The explicit recommendation from that review: don't throw away the current
`extraction_facts + evidence + confidence + review + audit + contradiction`
structure — it becomes the foundation Phase 1 builds on, not something
Phase 1 replaces. This is a scoping conversation to have before writing
Phase 1 code, not a decision to make unilaterally.

---

## Foundation refactor — completed before starting Phase 1

A second, independent audit of the repository (after Phase 0 was confirmed
frozen) flagged that the *code* was solid but the *architecture* had not
caught up: `main.py` had grown to 2,910+ lines and `db.py` to 1,789 lines,
holding six generations of features (carrier briefing, client intake, gap
analysis, drafting, feedback, document intelligence) in two files, and —
more importantly — organization scoping was inconsistent: only the newer
Document Intelligence endpoints checked `organization_id`; the older
carrier/client/match/gap/draft code mostly didn't. Rather than start Phase 1
on that foundation, this was fixed first.

**What was actually broken, and is now fixed:**

1. **Organization isolation gaps in the older features.** `get_carrier`,
   `list_carriers`, `list_documents_for_carrier`, `get_document_with_text`,
   `delete_carrier`, `get_client`, `list_clients`, `get_draft`,
   `list_drafts`, `get_draft_events`, and the match/gap/correction/
   house-style functions did not filter by organization at all — any caller
   knowing an id could read or modify any organization's carrier, client,
   match, gap report, or draft. Fixed the same way Document Intelligence
   already did it: every function now takes `organization_id` and checks it
   *inside* the SQL query, so a record belonging to another organization is
   indistinguishable from one that doesn't exist — same pattern, same 404,
   no exceptions. `house_style` had an extra bug of its own: its primary key
   was just `(kind)`, meaning one organization's learned drafting style
   could silently overwrite another's; it's now `(kind, organization_id)`.
   Missing `organization_id` columns (on `documents`, `gap_reports`,
   `drafts`, `client_documents`, `corrections`, `house_style`) were added
   via migration, backfilled from the parent record or the default
   organization so existing single-org installs keep working unchanged.

2. **An unscoped Document Intelligence endpoint.** `GET
   /doc-intelligence/documents/{document_id}/facts` did not apply the same
   ownership check that `GET /doc-intelligence/facts/{fact_id}` already
   had — a caller who knew a `document_id` from another organization could
   list its facts even though fetching one of those same facts directly by
   `fact_id` was correctly rejected. Fixed to resolve and check the
   organization the same way.

3. **The fake "(unfiled) carrier" workaround, removed.** Every Document
   Intelligence upload previously called `create_carrier("(unfiled) " +
   filename)` purely to satisfy `documents.carrier_id`'s NOT NULL
   constraint — meaning every single doc-intelligence upload silently
   created a junk carrier row with no real purpose. `documents.carrier_id`
   is now nullable via migration, and doc-intelligence uploads leave it
   `NULL` instead of manufacturing a carrier. Verified against a live
   database: `SELECT COUNT(*) FROM carriers WHERE name LIKE '%unfiled%'`
   returns `0` after uploads that previously would have created one.

4. **Stale schema documentation.** `db.py`'s header comment still said
   *"Phase 1 has two tables: carriers and documents"* — no longer remotely
   true given everything the schema now holds. Rewritten to actually
   describe the current schema and the organization-scoping convention, so
   a future reader trusts the comment rather than being misled by it.

5. **Structural split — `main.py` and `db.py` broken into feature modules,
   zero logic change.** `main.py` (was 3,001 lines after the fixes above)
   is now a 101-line FastAPI app shell — CORS config, startup schema init,
   and `include_router()` calls — with every endpoint's actual logic moved
   into `api/<feature>.py` (health, organizations, carriers, clients,
   matching, gaps, drafting, feedback, ask, submissions, doc_intelligence).
   `db.py` is now a 19-line compatibility shim (`from repositories import
   *`) so nothing importing `db.xxx(...)` had to change; the real functions
   live in `repositories/<feature>.py`, with shared connection/schema setup
   in `repositories/connection.py`. This was a pure move-and-rewire
   operation — no SQL, endpoint path, or request/response shape changed.

**How this was verified — real, not assumed, at every stage:**
- After the isolation fixes: booted the real backend against real local
  Postgres, created two organizations via real HTTP calls, created a
  carrier under org A, and confirmed org B's list is empty, org B's direct
  fetch-by-id is `404`, org B's delete attempt is `404` and does not delete
  the row, org A still sees and can access its own carrier, and a
  no-header request still resolves to the default organization exactly as
  before (backward compatibility for the existing single-org UI).
- Same test repeated, independently, after the file split, against a fresh
  boot of the restructured code, on a different port — identical results:
  `404`/`404`/`200`/`200`. The split did not reintroduce or fix anything —
  behavior is byte-for-byte the same before and after.
- The fake-carrier removal was checked directly against Postgres (`SELECT
  COUNT(*) FROM carriers WHERE name LIKE '%unfiled%'` → `0`) after a real
  document-intelligence upload, both before and after the file split.
- `eval_harness.py` was re-run end-to-end against the full 20-company
  synthetic dataset after the split: 20/20 scored, contradiction detection
  4/4 correct with zero false positives, deterministic-rule extractions
  still correct. The model-dependent extractions couldn't be exercised in
  this environment (no Ollama reachable here) but failed gracefully with a
  clear "could not reach Ollama" message rather than crashing or guessing —
  the same behavior the pre-refactor code had in the same environment, so
  this is an environment limitation, not a regression.

**What this does not change:** the pilot-stage tenant boundary is still
application-level (the `X-Organization-Id` header is trusted, not
authenticated) — this refactor made that boundary *consistent* everywhere
it applies, not stronger in kind. Real user authentication behind that
header remains future work, same as before.

---

## Second security gap found and fixed: ChromaDB retrieval had no tenant boundary at all

A follow-up audit of the just-refactored code found something the previous
pass genuinely missed: **the Postgres-side organization scoping above never
touched ChromaDB.** `vectorstore.py` had zero references to
`organization_id` anywhere in it. Carrier matching (`matching.py`) and the
`/chat` endpoint (`chat.py`, `api/ask.py`) both search Chroma directly, and
`/chat` specifically was not even resolving an organization at all — so
organization B could ask a question and get back chunks from organization
A's carrier documents. This is a real gap the file-split refactor did not
close, because it only touched `main.py`/`db.py`; the retrieval path lives
in separate modules that were never in scope for that pass.

**What was fixed:**

- `vectorstore.index_document()` now stores `organization_id` on every
  chunk's Chroma metadata alongside `carrier_id`/`document_id`/etc.
- `vectorstore.search()` and `vectorstore.count_for_carrier()` now both
  *require* `organization_id` and apply it as a hard Chroma `where` filter
  (combined with the existing `carrier_id` filter via `$and` when both
  apply, never silently dropping one for the other).
- `matching.py` (`match_client`, `gather_extracts`) and `chat.py` (`ask`)
  now require `organization_id` and pass it into every `search()` call they
  make — there is no code path left that calls Chroma without it.
- `api/ask.py`'s `/chat` endpoint — which previously did not resolve an
  organization at all — now calls the same `_resolve_organization()` every
  other endpoint uses, and passes the result through.
- `api/matching.py`'s match endpoint now threads its already-resolved
  organization into `matching.match_client()`.
- `/reindex` (which rebuilds the whole Chroma index from Postgres) now
  reads each document's real `organization_id` from Postgres and tags each
  rebuilt chunk with it correctly, instead of rebuilding an index with no
  tenant information at all.

**How this was verified — including proving the vulnerability was real,
not just theoretical:**

- Chroma isn't reachable in this sandbox by default (no Docker), so a real
  local Chroma server was started (`chroma run`) rather than skipping the
  test. A standalone script first proved the **old, unscoped** query
  genuinely leaked organization A's chunk to a query made in organization
  B's context — confirming this was a real, exploitable gap and not a
  theoretical one. The same script then proved the **new, filtered** query
  returns only the calling organization's own chunk in both directions,
  and that the combined `carrier_id` + `organization_id` filter and
  `count_for_carrier` are isolated too.
- A full real-HTTP test followed: real FastAPI app, real local Postgres,
  real local Chroma server, two real organizations, a real `.docx`
  uploaded and briefed (indexed) under each. `/chat` as organization B
  returned sources citing only organization B's own carrier — asserted
  directly, no organization A carrier id present in the response — and the
  same check the other direction for organization A. The carrier-matching
  endpoint under organization B returned results sourced only from
  organization B's own carrier, with organization A's much larger indexed
  carrier correctly invisible. A no-header request still resolved to the
  default organization and worked normally, preserving backward
  compatibility. `/reindex` was run for real afterward, rebuilding every
  chunk from Postgres, and the same isolation checks were re-run and still
  held.

**A smaller bug fixed alongside it:** the frontend's `downloadDraft()`
bypassed the central `request()` helper that attaches
`X-Organization-Id` to every other API call, meaning a draft download
never sent the active organization. Fixed to send the header while still
handling the binary file response correctly; verified by running the real
function directly and confirming the header is present when an
organization is active and absent (falling back to default) when it
isn't, matching every other endpoint's behavior.

**What this means for the earlier refactor's claims.** The previous
"foundation refactor" section above is still accurate for what it covered
— the Postgres-backed carrier/client/match/gap/draft functions really were
fixed and really were verified. It just didn't cover the vector-search
path, because that code lives outside `main.py`/`db.py`. Both gaps are now
closed, independently verified, in the same repository.

---

# Phase 1 — Insurance Knowledge Core (first vertical slice)

With Phase 0 frozen and both isolation gaps above closed, this is the start
of the next stage every outside review pointed toward: turning
`extraction_facts` into a real, general Knowledge Core rather than one
fact type hardcoded to one table. Per explicit prior instruction, nothing
from Phase 0 was deleted or altered to build this — `extraction_facts`,
`doc_intelligence.py`, and everything downstream of them are untouched.
This is additive.

## What was built

Four new tables (`kc_entities`, `kc_facts`, `kc_evidence`,
`kc_relationships`), a `backend/knowledge/` package (`entities.py`,
`facts.py`, `evidence.py`, `relationships.py`, `versions.py`, `service.py`
as the single interface everything else calls), and the first concrete
feature built on top of it: **Property Policy Intelligence**
(`backend/property_policy_intelligence.py`) — takes a `submission` (the
same grouping concept Phase 0 built for contradiction detection: a Property
Policy + Coverage Schedule + Endorsement + Risk Survey believed to be the
same case) and extracts a small, fixed set of property and policy facts
(`property.location`, `property.occupancy`, `property.construction`,
`property.sum_insured`, `property.risk_factors`, `policy.coverage`,
`policy.coverage_limit`, `policy.deductible`, `policy.exclusion`,
`policy.endorsement`) — deliberately not a 300-field ontology, per the
explicit instruction not to repeat Phase 0's "keep it narrow" discipline
in Phase 1.

Every extracted value becomes a `kc_facts` row pointing at a `kc_entities`
row (`Business`, `Asset`, `Policy` entity types used here; `Location`,
`Carrier`, `Coverage`, `Claim`, `Risk`, `Person`, `Document` exist in the
schema for later use, not invented ahead of need) with a `kc_evidence` row
carrying the real document, page, and snippet — the same
fact-must-point-at-real-evidence discipline Phase 0 established, now
generalised past one hardcoded fact type. Relationships (`Business
-owns-> Asset`, `Business -has_policy-> Policy`) make the entities into an
actual small graph, not just a flat fact table. Versioning is
`valid_from`/`valid_to` on `kc_facts` — a corrected or superseded fact
closes the old row's `valid_to` and opens a new one, rather than
overwriting history, so `get_historical_values()` can show what the
system believed at any earlier point.

**Contradiction detection carries over unchanged, not reimplemented.**
When two documents in the same submission disagree on a fact for the same
entity beyond tolerance, `service.reconcile_fact()` flags both as
`contradiction_flagged` — this calls the exact same tolerance/comparison
logic `doc_intelligence.find_contradiction()` already used in Phase 0,
rather than writing a second, possibly-inconsistent copy of that rule.

**Every table and endpoint is organization-scoped from the first line of
schema, not fixed after the fact.** Phase 0 spent two full rounds of
outside audits finding organization-scoping gaps added after code already
shipped (first in the older carrier/client/match/gap/draft code, then in
Chroma retrieval). This time the constraint was explicit and non-negotiable
from the start: every `kc_*` query filters by `organization_id` inside the
SQL itself, and every new `/knowledge/*` endpoint resolves the organization
before doing anything else — the same pattern, applied up front instead of
patched in afterward.

## How this was verified

- Schema migrations tested against the **existing** database with real
  Phase 0 data already in it (documents, extraction_facts, submissions
  from prior testing) — the new tables were added with no error and no
  existing data touched, confirmed by comparing row counts before and
  after. Also tested against a **freshly created** database to confirm the
  base schema alone (not just the migration path) produces a working end
  state, same standard as the earlier refactor.
- Real HTTP, two organizations: created a submission under organization A
  with four real documents that together describe one property/policy
  case, ran the new `/knowledge/submissions/{id}/analyze-property-policy`
  endpoint, and confirmed the resulting Business/Asset/Policy entities and
  their facts/evidence were created correctly with real evidence pointing
  at real document pages.
- **Cross-tenant isolation, checked independently after the subagent that
  built this reported it fixed:** created an entity under organization A,
  confirmed organization B's `GET /knowledge/entities/{id}` for that
  entity returns `404` and organization A's own fetch returns `200` — the
  same test run personally, not just trusted from the build report, given
  this project's history of exactly this kind of bug slipping through
  once before.
- **Contradiction detection re-verified on the two known Phase 0
  contradiction cases** (`AUR-SYN-004`, `AUR-SYN-009`) run through the new
  Knowledge Core path instead of the old one: both correctly produced a
  `property.sum_insured` contradiction with both sides flagged
  `contradiction_flagged`, and a negative-control case with agreeing values
  correctly produced zero contradictions and merged evidence onto one
  current fact rather than duplicating it.
- Persistence: killed the backend process mid-test and restarted it
  against the same Postgres database — a previously corrected fact's
  value, status, and reviewer were unchanged after restart.

## What this doesn't yet establish

- **The LLM extraction path ran against a stub, not the real local
  model**, because Ollama isn't reachable in the sandbox this was built
  and tested in — the same environment limitation Phase 0 repeatedly hit,
  and the same fix applies: this needs a re-run against the real
  `qwen2.5:7b`, the same way the Phase 0 hard-OCR caveat was closed, before
  trusting the actual extraction *quality* (as opposed to the entity/fact/
  evidence/relationship/contradiction *plumbing*, which was verified with
  real code paths and real data, just not a real model's actual judgment
  calls).
- The frontend `Knowledge.jsx` view was verified by exercising the same
  HTTP calls the UI makes, not by an actual browser click-through — same
  caveat the organization-switcher UI carried before it was manually
  confirmed working.
- This is one vertical slice (Property Policy Intelligence), not a general
  extraction framework — deliberately, per the same "prove one thing
  before generalising" discipline Phase 0 was built under. Expanding fact
  types, entity types, or relationship types should follow real broker
  workflow needs, not get built ahead of them.

---

## Phase 1A hardening — three design bugs fixed, plus four robustness items

A follow-up audit of the Knowledge Core above found real, confirmed
correctness problems — not style preferences — before this was safe to
build further features on. All were fixed in one pass.

**1. Contradiction detection didn't actually prevent a wrong "current
value" from being read.** Before this fix, a contradiction correctly
flagged both disagreeing facts `contradiction_flagged`, but
`get_current_fact()` still answered "what's the current value?" with
`ORDER BY created_at DESC LIMIT 1` — silently treating whichever fact
happened to be newer as authoritative, exactly the outcome the
contradiction flag was supposed to prevent. Fixed with a new `kc_conflicts`
table: when facts disagree, a conflict row opens (or extends) alongside
the existing per-fact flagging, and a new
`get_current_value_or_conflict()` checks for an unresolved conflict
*first* — if one exists for that entity+fact_type, it returns
`{"current_value": null, "unresolved_conflict": true, "conflicting_facts":
[...]}` rather than picking a side. Verified with a real conflict: both
facts flagged, a `kc_conflicts` row opened `unresolved`, and the
current-value query correctly refused to name either as authoritative.

**2. Human corrections overwrote history instead of versioning it.**
`PUT /knowledge/facts/{id}` was updating the fact row in place — the
original value was gone the moment someone corrected it, even though a
`supersede_fact()` function already existed and did this the right way
(close the old row's `valid_to`, open a new row with the corrected value).
The correction endpoint just wasn't calling it. Fixed to route every
correction through `supersede_fact()`, and — since a correction is exactly
the moment a human resolves a disagreement — a correction that touches a
fact involved in an unresolved conflict now also marks that `kc_conflicts`
row `resolved`, pointing at the new corrected fact as the resolution.
Verified over real HTTP: correcting a fact produces a *new* row with a
different id, the original row's value is untouched with `valid_to` now
set, and the history endpoint shows the full chain — original →
superseded → corrected, plus confirmed the correction actually flips a
real open conflict to `resolved`.

**3. Evidence and relationships didn't verify cross-organization
ownership at the database layer.** `create_evidence()` and
`create_relationship()` accepted an `organization_id` argument but never
checked that the fact/document (for evidence) or both entities (for a
relationship) actually belonged to that organization — they just
inserted. The API layer happened to make this safe in the normal request
flow, but the repository functions themselves didn't enforce it, meaning
any future caller that skipped the API layer could create evidence or
relationships that silently crossed the tenant boundary. Fixed by
checking ownership inside the repository functions directly, raising a
clear error when it fails. Verified directly (bypassing the API, calling
the repository functions themselves) with two real organizations: linking
evidence to another organization's fact, another organization's document,
and a relationship between two entities in different organizations were
all rejected; same-organization calls still worked normally.

**4. Singleton vs. multi-valued facts were being treated identically.**
`policy.exclusion`, `policy.endorsement`, and `property.risk_factors` can
legitimately have several true values at once ("Flood excluded", "War
excluded", "Nuclear contamination excluded" are not contradictions, they
are three facts) — but reconciliation was comparing every same-type fact
as if it were one value, which would have flagged them against each other.
Fixed with a small registry marking which fact types are singleton
(`property.sum_insured`, `property.occupancy`, `property.construction`,
`property.location`, `policy.coverage_limit`, `policy.deductible` — one
true current value) versus multi-valued (the exclusion/endorsement/
risk-factor types above — several simultaneously true values, never
reconciled against each other). Verified with a real test: three
different `policy.exclusion` values on the same policy correctly persisted
as three separate current facts with zero contradictions flagged, while a
repeated negative control confirmed `property.sum_insured` still correctly
triggers a contradiction when it disagrees — proving the fix narrowed
contradiction detection rather than disabling it.

**5. Evidence snippets weren't checked against the real document text.**
Only the claimed page number was verified (checking for a `[Page N]`
marker); the model's claimed quote itself was trusted without checking it
actually appeared in the source document, which is exactly the kind of
gap the evidence requirement exists to close. Fixed with a snippet
verification step: an exact substring match first, falling back to an
anchored fuzzy match for whitespace/OCR noise, and a fact whose snippet
can't be verified either way is marked `needs_review` with a clear note
rather than silently accepted. Verified directly against the verification
function itself: a fabricated snippet correctly reported not-found, a
real verbatim quote correctly verified, and a real quote with whitespace
noise correctly verified through the fuzzy path (a bug in the first
attempt at the fuzzy matcher was found and fixed during this same testing
pass, before it shipped).

**6. Long documents silently lost facts past the first 12,000
characters.** Extraction was truncating each document's text to
`text[:12000]` before sending it to the model — a fact stated on page 30
of a long policy simply never reached the model. Fixed by chunking the
document (using the existing `[Page N]` markers already present in
parsed text) and running extraction per chunk, aggregating the results
rather than truncating. Verified with a real test document built
specifically to place its target fact past character 22,000 — the fact
was correctly extracted after this fix, which is the one test that
actually proves the truncation bug is gone, not just that the code
changed.

**7. OCR confidence was document-average, not page-specific.** A fact
extracted from a specific OCR'd page was getting the whole document's
average OCR confidence rather than that page's own confidence — a fact
found on a clean page 1 and a fact found on a barely-legible page 7 could
carry the same reported confidence despite being nothing alike in
reliability. Fixed by threading per-page OCR confidence through instead
of the document average. Verified with a real test case built with
deliberately different per-page confidences (95% vs. 40%, average 67.5%)
— a fact from each page correctly carried its own page's confidence, not
the shared average.

**How all of this was verified:** real Postgres, real HTTP calls, two
real organizations where cross-tenant behavior mattered, and — since
Ollama isn't reachable in this sandbox — a stub Ollama server exercised
over real HTTP (the same approach used earlier in this project, never a
Python-level mock of the code under test) so the extraction code path
itself ran for real. Every item above has its own specific real test
described next to it, not a shared claim of "it works." The same
limitation as the rest of Phase 1 applies: this proves the entity/fact/
evidence/relationship/conflict/versioning *plumbing* is correct; it does
not yet prove the real `qwen2.5:7b` model's actual extraction judgment on
these new fact types, which — like the Phase 0 hard-OCR caveat before it
— needs a real run before being trusted for extraction quality.
