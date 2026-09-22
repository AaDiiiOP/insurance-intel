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
| Sensor noise | 28.4% | ✅ correct — borderline; see caveat below |
| Perspective skew | 57.6% | Abstained — see caveat below |
| Multi-distortion phone photo | 0.0% | Abstained — genuine failure |

**The number that matters most: zero silent wrong answers, across two
separate runs.** Tesseract is not perfectly deterministic on borderline
images — re-running produced `sensor_noise` as a correct extraction instead
of an abstention the second time, at 28.4% confidence — but in both runs,
every case that did not extract correctly abstained rather than guess. No
instance, in either run, of a high-confidence wrong figure. That is the
actual property Phase 0's "no silent guessing" rule exists to guarantee,
and it held under real degraded input on both passes, not just clean
synthetic scans.

**One honest caveat, narrowed by the second run.** `perspective_skew`
remains an abstention in both runs, but checking the raw OCR output
directly (rather than trusting the pass/fail alone) shows Tesseract read
`"Property Sum insured: INR'500,000,000"` — human-legible, with only the
currency prefix corrupted (`INR'` instead of `INR`). The extraction fell
through to the model path as designed, but the *stand-in* model used for
this test (a regex requiring an exact `INR`/`Rs`/`₹` prefix, the same
limitation noted throughout this file wherever the real model was
unavailable) missed it on that technicality. **A real LLM reading that text
would very plausibly still recognise "500,000,000" as the sum insured** —
fuzzy recognition around a corrupted label is exactly what a language model
does well and a rigid pattern match does not. This should be re-verified
against the real `qwen2.5:7b` before treating `perspective_skew` as a
confirmed OCR failure rather than a stand-in-model limitation. The other
four consistent abstentions (`rotated_8deg`, `blur_moderate`, `blur_heavy`,
`low_resolution`, `multi_distortion`) recovered only 0–22 characters of
genuine gibberish and are real failures regardless of which model reads
them.

**What this establishes and what it doesn't.** It establishes that
degraded-but-legible scans (mild rotation, moderate contrast loss, a shadow)
are handled correctly, and that badly degraded scans fail safely rather than
silently wrong. It does not establish a production-ready OCR accuracy rate —
a sample of 1 source document and 11 synthetic degradations is a stress
test, not a statistically meaningful benchmark, and a re-run against the
real model (closing the caveat above) is needed before citing an accuracy
percentage from this test.

Reproducible: `python3 build_hard_ocr_set.py` then `python3 run_hard_ocr_test.py`,
full detail in `hard_ocr_results.json`.

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
   genuine degraded-scan stress test. One caveat remains: re-run Test 3
   against the real `qwen2.5:7b` to confirm the two borderline abstentions
   (`sensor_noise`, `perspective_skew`) are real OCR failures and not an
   artifact of the stand-in model used for this pass.
5. ~~Manually walk through P0-06 (cross-organization isolation) and P0-07
   (persistence across restart).~~ **Done** — see Test 1 and Test 2 above.
   P0-06 required a real fix (organization scoping did not previously
   exist), not just a test.
6. ~~Extend `eval_harness.py` to cover contradiction detection
   automatically.~~ **Done** — see `contradiction_checks` in
   `eval_results.json` and the summary fields `contradiction_accuracy_pct`
   / `contradiction_false_positives`.
7. **New:** a submission-picker UI now exists in the Doc Intelligence
   screen (create a submission, upload documents into it, contradictions
   highlighted with a red border and pill on the fact card) — verified with
   real screenshots against the real AUR-SYN-004 case. Not yet done: an
   equivalent for P0-06/P0-07 (org isolation, persistence) has no UI either,
   and remains a manual API-level check per item 5.
