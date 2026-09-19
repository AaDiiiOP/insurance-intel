# Insurance Intelligence

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

## Architecture

```
backend/
  main.py         FastAPI app — every endpoint
  db.py           Postgres schema + queries (raw SQL, no ORM)
  parsers.py      pdf / docx / xlsx / pptx -> text
  chunking.py     structure-aware splitting, page/section provenance kept
  summarise.py    map-reduce briefing generation (Phase 1)
  tokenise.py     client redaction -- the boundary the whole design leans on
  vectorstore.py  ChromaDB indexing and retrieval (Phase 2)
  matching.py     multi-query retrieval + carrier ranking (Phase 2)
  chat.py         question answering with a similarity floor + strict prompt
  extraction.py   reading a client's own submission into intake fields
  gaps.py         requirement checklist with a deterministic backstop (Phase 3)
  verify.py       document verification + checksums + scoring (Phase 3.5)
  drafting.py     the four document generators (Phase 4)
  export_docx.py  draft -> real .docx
  feedback.py     house-style distillation with a safety guardrail (Phase 5)
  llm.py          the one place that talks to Ollama
  prompts/*.txt   every prompt, kept out of the code

frontend/
  src/views/  Upload . Carriers . Briefing . Clients . ClientDetail .
              Ask . Review . Health
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

## Troubleshooting

**"Ollama is not reachable"** -- it runs on your machine, not in Docker.
`curl http://localhost:11434/api/tags` should return JSON.

**Matching/Ask says the index is empty but carriers are briefed** -- briefing
and indexing are separate steps. Check Settings for the real cause (usually
`nomic-embed-text` not pulled), fix it, then *Rebuild search index*.

**A carrier's briefing finds no requirements** -- its documents may not
actually be policy paperwork, or the parse produced too little text.
Regenerate the briefing; if it's still empty, check what was actually
extracted at `GET /documents/{id}/text`.

**Ports clash with another project** -- everything's remapped off the common
defaults (5183 / 8100 / 5442 / 8101 instead of 5173 / 8000 / 5432 / 8001)
specifically so this can run alongside something else.
