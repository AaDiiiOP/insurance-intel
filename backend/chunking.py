"""
Document chunking, used by summarisation now and by vector indexing later.

Structure-first splitting: break on section headers (a policy wording's own
numbered clauses — "1. Product Overview", "2.1 Mandatory for All Sections" —
work exactly like the numbered sections this pattern was built for), falling
back to fixed-size splitting only for sections that are themselves too long,
or documents with no headings at all (a plain brochure, a rate card with no
real structure).

Built once here and reused — a later phase embeds these same chunks into a
vector store for RAG-based plan matching. That's why every chunk carries its
section title and page number: provenance is what makes a future "why did
the AI suggest this policy" citation possible, and it has to be captured now
rather than reconstructed later.

No LLM calls in this module. It's pure text handling, so it can be tested
without Ollama running. Carried over unchanged from the reference
RFP-Intelligence project — chunking a numbered/sectioned document doesn't
depend on what the document is about.
"""
import re
from dataclasses import dataclass, field

# ~1500 tokens with ~150 overlap, per CHUNKING_STRATEGY.md. Tokens aren't
# characters, so these are expressed in characters using a rough 4:1 ratio —
# good enough for chunking, and it avoids depending on a tokeniser that would
# have to match whichever model is loaded.
CHARS_PER_TOKEN = 4
TARGET_CHUNK_CHARS = 1500 * CHARS_PER_TOKEN  # ~6000
OVERLAP_CHARS = 150 * CHARS_PER_TOKEN  # ~600

# Every chunk costs one LLM call, and a call on a 20-character title line is
# a call wasted. Anything under this gets folded into the chunk after it.
MIN_CHUNK_CHARS = 400

# Separator used when a merged chunk covers several headings.
SECTION_TITLE_SEPARATOR = " · "

# A section header in a typical RFP: "1. Introduction", "2.1 Hard FM",
# "SECTION 3 - Requirements". Kept deliberately conservative: a false positive
# splits mid-sentence, which is worse than missing a heading and falling back
# to size-based splitting.
SECTION_PATTERNS = [
    # "1. Introduction" / "2.1 Scope of Services" / "3.4.1 Detail"
    re.compile(r"^\s{0,3}(\d+(?:\.\d+)*)\.?\s+(\S.{0,90})$"),
    # "SECTION 4 - Evaluation" / "Section 4: Evaluation"
    re.compile(r"^\s{0,3}(?:SECTION|Section|PART|Part)\s+(\d+[A-Za-z]?)\s*[-:—]?\s*(\S.{0,90})$"),
    # Markdown-style headings, which our DOCX/PPTX parsers can produce
    re.compile(r"^\s{0,3}#{1,4}\s+(\S.{0,90})$"),
]

# Page markers written by parsers.parse_pdf, e.g. "[Page 12]".
PAGE_MARKER = re.compile(r"^\[Page (\d+)\]$")

# Sheet/slide markers from the other parsers.
BLOCK_MARKER = re.compile(r"^\[(?:Sheet|Slide|Table)[:\s][^\]]*\]$")


@dataclass
class Chunk:
    """One piece of a document, with enough context to cite it later."""

    index: int
    text: str
    section_title: str | None = None
    page_start: int | None = None
    # True when this chunk came from the fixed-size fallback rather than a
    # real section boundary. Useful for judging how well a document sectioned.
    from_fallback: bool = False
    # Which source document this chunk came from. None when chunking a
    # single document in isolation (the original, still-supported case) —
    # only set when pooling chunks from several documents, where a section
    # name alone is no longer enough to tell them apart.
    document_id: str | None = None
    filename: str | None = None

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class ChunkedDocument:
    chunks: list[Chunk] = field(default_factory=list)
    # How the document was split, for logging and for telling Mann how well
    # the structure-first path is actually working on real RFPs.
    sectioned: bool = False

    @property
    def count(self) -> int:
        return len(self.chunks)


def _clean_title(title: str) -> str:
    """
    Strip markdown emphasis and trailing punctuation from a heading.

    Also drops a trailing page number left by a table of contents. A TOC line
    reads "2. Eligibility Criteria\t3" and matches the numbered-heading
    pattern just as well as the real heading it points at, so without this
    the same section appears twice with one copy labelled "...Criteria 3".
    The dot-leader form ("Scope .......... 12") is handled too.
    """
    title = re.sub(r"[*_`]+", "", title)
    title = re.sub(r"[\t.\u00b7\s]{2,}\d{1,4}\s*$", "", title)
    title = re.sub(r"\t\s*\d{1,4}\s*$", "", title)
    return title.strip(" -—:.,;\t").strip()


def _match_section(line: str) -> str | None:
    """Return a section title if this line looks like a heading."""
    stripped = line.strip()

    # A real heading is short. The previous limit (110) let PDF-wrapped body
    # text through: "2.1 Hard FM — mechanical, electrical, HVAC, and building
    # fabric" is a wrapped sentence, not a section title, and labelling a
    # chunk with it produced unreadable citations.
    if not stripped or len(stripped) > 70:
        return None

    # A line ending in a sentence-ending period is prose, not a heading —
    # "2. Scope of Services" is a heading, "2. The provider shall comply."
    # is not.
    if stripped.endswith((".", ";", ",")) and len(stripped.split()) > 8:
        return None

    # parsers.parse_xlsx flattens a spreadsheet row with " | " between
    # cells. A row that happens to start with a bare number — a "Week"
    # column reading "1", a row index — otherwise matches the numbered-
    # heading pattern below just as well as a real "1 Introduction" does,
    # since that pattern's trailing period is optional. A citation ending
    # up labelled after a data row instead of a real section title was the
    # actual symptom that led here — this rejects anything that is
    # unmistakably a flattened table row rather than prose.
    if " | " in stripped:
        return None

    for pattern in SECTION_PATTERNS:
        match = pattern.match(stripped)
        if match:
            groups = [g for g in match.groups() if g]
            title = _clean_title(" ".join(groups))
            return title or None
    return None


def _split_oversized(
    text: str,
    base_index: int,
    section_title: str | None,
    page_start: int | None,
    header: str | None = None,
) -> list[Chunk]:
    """
    Fixed-size split with overlap, for text too long to send in one prompt.

    Prefers to break at a paragraph or sentence boundary near the target size
    so chunks don't end mid-word.

    `header`, when given, is prepended to every piece after the first — the
    header row of a spreadsheet sheet, so a model reading a later piece in
    isolation still knows what each column means, rather than seeing bare
    numbers with no column labels. The first piece already has the header
    naturally, since it's the first line of the section's own text.
    """
    chunks: list[Chunk] = []
    position = 0
    index = base_index
    is_first_piece = True

    while position < len(text):
        end = min(position + TARGET_CHUNK_CHARS, len(text))

        if end < len(text):
            # Look backwards from the target for a clean break.
            window = text[position:end]
            for separator in ("\n\n", "\n", ". "):
                cut = window.rfind(separator)
                # Only accept a break in the last third, otherwise chunks end
                # up far too small.
                if cut > TARGET_CHUNK_CHARS * 0.6:
                    end = position + cut + len(separator)
                    break

        piece = text[position:end].strip()
        if piece:
            if header and not is_first_piece:
                piece = f"{header}\n{piece}"
            chunks.append(
                Chunk(
                    index=index,
                    text=piece,
                    section_title=section_title,
                    page_start=page_start,
                    from_fallback=True,
                )
            )
            index += 1
            is_first_piece = False

        if end >= len(text):
            break
        # Step back by the overlap so a requirement spanning the boundary
        # appears in both chunks and can't be missed by either.
        position = max(end - OVERLAP_CHARS, position + 1)

    return chunks


# Merged chunks can span several headings. Cap what gets kept so a label
# stays readable in a citation line — the full text is always in the chunk.
MAX_TITLE_PARTS = 3
MAX_TITLE_PART_CHARS = 60


def _join_titles(*titles: str | None) -> str | None:
    """
    Combine section headings from merged chunks, in order, without repeats.

    Keeping only the first heading (the previous behaviour) mislabels the
    chunk: a merged chunk holding Introduction, Scope and Pricing would be
    labelled just "Introduction", and the model then weights that thin label
    over the fuller text it actually received.

    Long or numerous headings are trimmed rather than dropped, so the label
    stays usable as a citation without losing the fact that several sections
    are covered.
    """
    seen: list[str] = []
    for title in titles:
        if not title:
            continue
        for part in title.split(SECTION_TITLE_SEPARATOR):
            part = part.strip()
            if not part:
                continue
            # A heading that runs on is really a first line of body text.
            if len(part) > MAX_TITLE_PART_CHARS:
                part = part[:MAX_TITLE_PART_CHARS].rstrip() + "…"
            if part not in seen:
                seen.append(part)

    if not seen:
        return None

    if len(seen) > MAX_TITLE_PARTS:
        extra = len(seen) - MAX_TITLE_PARTS
        seen = seen[:MAX_TITLE_PARTS] + [f"+{extra} more"]

    return SECTION_TITLE_SEPARATOR.join(seen)


def _merge_small(chunks: list[Chunk]) -> list[Chunk]:
    """
    Fold undersized chunks into the following one.

    Document title lines and one-line headings each become their own "section"
    otherwise, and each would cost a separate LLM call for almost no content.
    Merging forward keeps the heading attached to the text it introduces.
    """
    if not chunks:
        return chunks

    merged: list[Chunk] = []
    carry: Chunk | None = None

    for chunk in chunks:
        if carry is not None:
            chunk = Chunk(
                index=0,  # reindexed at the end
                text=f"{carry.text}\n\n{chunk.text}",
                # Every heading the merged text covers, not just the first.
                section_title=_join_titles(carry.section_title, chunk.section_title),
                page_start=carry.page_start
                if carry.page_start is not None
                else chunk.page_start,
                from_fallback=carry.from_fallback or chunk.from_fallback,
            )
            carry = None

        if len(chunk.text) < MIN_CHUNK_CHARS:
            carry = chunk
            continue

        merged.append(chunk)

    # A trailing small chunk has nothing to merge forward into, so append it
    # to the previous chunk instead — or keep it if it's the only one.
    if carry is not None:
        if merged:
            last = merged[-1]
            merged[-1] = Chunk(
                index=last.index,
                text=f"{last.text}\n\n{carry.text}",
                section_title=_join_titles(last.section_title, carry.section_title),
                page_start=last.page_start,
                from_fallback=last.from_fallback or carry.from_fallback,
            )
        else:
            merged.append(carry)

    for position, chunk in enumerate(merged):
        chunk.index = position

    return merged


def chunk_documents(documents: list[tuple[str, str, str]]) -> ChunkedDocument:
    """
    Chunk several documents and pool the results into one list.

    `documents` is a list of (document_id, filename, text) triples. Each
    document is chunked independently — never concatenated first — because
    page numbers and section headings are only meaningful within their own
    document; running two files together as one string would produce
    nonsense like "page 3" meaning different things depending on where the
    first document ended.

    Every chunk in the result carries document_id and filename, so a
    citation can always say which of several RFP-side files an answer came
    from, not just which section.

    A bid with a single RFP document still goes through this same path, so
    there is one code path for "one document" and "several", not two to
    keep in sync.
    """
    pooled: list[Chunk] = []
    any_sectioned = False

    for document_id, filename, text in documents:
        result = chunk_text(text)
        any_sectioned = any_sectioned or result.sectioned

        for chunk in result.chunks:
            chunk.document_id = document_id
            chunk.filename = filename
            pooled.append(chunk)

    # Re-index across the whole pool, in the order documents were given —
    # a per-document index would collide (every document's first chunk is
    # index 0), and citations need one that's unambiguous across the pool.
    for position, chunk in enumerate(pooled):
        chunk.index = position

    return ChunkedDocument(chunks=pooled, sectioned=any_sectioned)


def chunk_text(text: str) -> ChunkedDocument:
    """
    Split a document into chunks, preferring section boundaries.

    Returns chunks in document order, each tagged with the section it came
    from and the page it starts on where that's knowable.
    """
    if not text or not text.strip():
        return ChunkedDocument()

    lines = text.split("\n")

    # Walk the document once, tracking the current page and grouping lines
    # under whichever heading was seen most recently.
    sections: list[dict] = []
    current = {"title": None, "lines": [], "page": None, "header_row": None}
    current_page: int | None = None
    found_heading = False

    for line in lines:
        page_match = PAGE_MARKER.match(line.strip())
        if page_match:
            current_page = int(page_match.group(1))
            if current["page"] is None:
                current["page"] = current_page
            # The marker itself isn't content worth summarising.
            continue

        block_match = BLOCK_MARKER.match(line.strip())
        if block_match:
            # A sheet/slide/table marker is a real boundary, the same as a
            # heading — a dense spreadsheet has no numbered headings at all,
            # so without this every sheet would get silently merged into one
            # undifferentiated blob, or worse, split blindly mid-row with no
            # heading to carry the column labels forward.
            found_heading = True
            if any(l.strip() for l in current["lines"]):
                sections.append(current)
            # Strip the brackets for a readable citation label — "[Sheet:
            # Rate Card]" becomes "Rate Card", matching how a real section
            # title looks elsewhere.
            label = line.strip().strip("[]")
            current = {
                "title": label,
                "lines": [line],
                "page": current_page,
                # Filled in below, from the very next line — a spreadsheet's
                # first content row after its marker is its header row.
                "header_row": None,
                "awaiting_header": True,
            }
            continue

        if current.get("awaiting_header"):
            current["header_row"] = line if line.strip() else None
            current["awaiting_header"] = False
            current["lines"].append(line)
            continue

        title = _match_section(line)
        if title:
            found_heading = True
            # Close the previous section if it has any content.
            if any(l.strip() for l in current["lines"]):
                sections.append(current)
            current = {"title": title, "lines": [line], "page": current_page, "header_row": None}
        else:
            current["lines"].append(line)
            if current["page"] is None:
                current["page"] = current_page

    if any(l.strip() for l in current["lines"]):
        sections.append(current)

    # No headings found at all — treat the whole document as one block and let
    # the size-based path handle it. CHUNKING_STRATEGY.md expects this case.
    if not found_heading:
        return ChunkedDocument(
            chunks=_merge_small(_split_oversized(text.strip(), 0, None, None)),
            sectioned=False,
        )

    chunks: list[Chunk] = []
    for section in sections:
        body = "\n".join(section["lines"]).strip()
        if not body:
            continue

        if len(body) <= TARGET_CHUNK_CHARS:
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=body,
                    section_title=section["title"],
                    page_start=section["page"],
                )
            )
        else:
            # Section is itself too long — fall back to size-based splitting
            # within it, keeping the section title on every piece, and the
            # header row too if this section came from a sheet marker.
            chunks.extend(
                _split_oversized(
                    body,
                    len(chunks),
                    section["title"],
                    section["page"],
                    header=section.get("header_row"),
                )
            )

    return ChunkedDocument(chunks=_merge_small(chunks), sectioned=True)
