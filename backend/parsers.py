"""
Document parsing for Phase 1.

One function per supported format, plus a dispatcher that picks the right one.
Deliberately has no knowledge of FastAPI or the database — this module takes
bytes in and gives text out, so it can be tested from a plain Python shell
without starting the server.

No AI is involved here. Phase 1 is pure file handling.

Domain-agnostic: this file is carried over unchanged from the reference
RFP-Intelligence project, since parsing a PDF/DOCX/XLSX/PPTX has nothing to
do with what the document is about. The documents this project feeds it are
insurance carriers' own paperwork — policy wordings, eligibility rules, rate
cards, brochures — never a real customer's file.
"""
import io
import logging
from dataclasses import dataclass, field

import openpyxl
import pdfplumber
from docx import Document as DocxDocument
from pptx import Presentation

logger = logging.getLogger(__name__)

# Formats we accept. Anything else is rejected before parsing is attempted.
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}

# Reject anything larger than this. RFPs are big but not this big; a file
# over the limit is more likely a mistake than a real tender.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# A scanned PDF is images of pages — it parses "successfully" but yields
# almost no text. If average characters per page falls below this, we treat
# the document as having no text layer rather than silently storing nothing.
# Real text pages run 1,500-3,000 chars; this threshold is deliberately low
# so we only flag documents that are clearly empty.
MIN_CHARS_PER_PAGE = 50


class ParseError(Exception):
    """Raised when a file cannot be parsed. Message is shown to the user."""


@dataclass
class ParsedDocument:
    """Result of parsing one file."""

    text: str
    pages: int  # pages / slides / sheets, depending on format
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)


# --------------------------------------------------------------------------
# Per-format parsers
# --------------------------------------------------------------------------


def parse_pdf(data: bytes) -> ParsedDocument:
    """
    Extract text page by page.

    pdfplumber returns None (not "") for a page with no extractable text,
    so each page is coerced to a string before joining.
    """
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            # Tag each page so Phase 3 can cite "page 12" rather than guessing.
            parts.append(f"[Page {index}]\n{text}")

    return ParsedDocument(text="\n\n".join(parts), pages=page_count)


def parse_docx(data: bytes) -> ParsedDocument:
    """
    Extract paragraphs *and* tables.

    python-docx keeps tables in a separate collection from paragraphs. Looping
    only over paragraphs silently drops every table — which in an RFP is often
    exactly where the requirements and pricing live.
    """
    doc = DocxDocument(io.BytesIO(data))

    parts: list[str] = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)

    for table_index, table in enumerate(doc.tables, start=1):
        rows: list[str] = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"[Table {table_index}]\n" + "\n".join(rows))

    # Word has no real page concept until it's rendered, so page count is
    # unknowable here. 0 means "not applicable" rather than "empty".
    return ParsedDocument(text="\n\n".join(parts), pages=0)


def parse_xlsx(data: bytes) -> ParsedDocument:
    """
    Flatten each sheet into delimited rows.

    read_only speeds up large workbooks; data_only returns the cached result
    of a formula rather than the formula text itself, which is what a reader
    actually wants.
    """
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)

    parts: list[str] = []
    sheet_count = 0
    try:
        for sheet in workbook.worksheets:
            sheet_count += 1
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if value is None else str(value).strip() for value in row]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"[Sheet: {sheet.title}]\n" + "\n".join(rows))
    finally:
        workbook.close()

    return ParsedDocument(text="\n\n".join(parts), pages=sheet_count)


def parse_pptx(data: bytes) -> ParsedDocument:
    """Extract text from every shape on every slide, plus speaker notes."""
    presentation = Presentation(io.BytesIO(data))

    parts: list[str] = []
    slide_count = 0
    for index, slide in enumerate(presentation.slides, start=1):
        slide_count += 1
        lines: list[str] = []

        for shape in slide.shapes:
            # Not every shape holds text — images and lines don't.
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())

        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"(Notes) {notes}")

        if lines:
            parts.append(f"[Slide {index}]\n" + "\n".join(lines))

    return ParsedDocument(text="\n\n".join(parts), pages=slide_count)


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

_PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".xlsx": parse_xlsx,
    ".pptx": parse_pptx,
}


def extension_of(filename: str) -> str:
    """Lowercased extension including the dot, e.g. '.pdf'."""
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def parse_file(filename: str, data: bytes) -> ParsedDocument:
    """
    Parse one uploaded file.

    Raises ParseError with a user-facing message on any failure — unsupported
    type, oversized, corrupt, or a PDF with no text layer. Callers store that
    message against the document so the UI can show why it failed.
    """
    if not data:
        raise ParseError("File is empty.")

    if len(data) > MAX_FILE_BYTES:
        size_mb = len(data) / (1024 * 1024)
        limit_mb = MAX_FILE_BYTES // (1024 * 1024)
        raise ParseError(
            f"File is {size_mb:.1f} MB, over the {limit_mb} MB limit."
        )

    extension = extension_of(filename)
    if extension not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ParseError(
            f"Unsupported file type '{extension or 'unknown'}'. Supported: {supported}."
        )

    parser = _PARSERS[extension]
    try:
        result = parser(data)
    except ParseError:
        raise
    except Exception as exc:
        # Corrupt file, password-protected PDF, wrong format behind a right
        # extension. Log the real traceback, show the user something short.
        logger.exception("Failed to parse %s", filename)
        raise ParseError(
            f"Could not read this file — it may be corrupt or password-protected. ({type(exc).__name__})"
        ) from exc

    _reject_if_no_text_layer(result, extension)
    return result


def _reject_if_no_text_layer(result: ParsedDocument, extension: str) -> None:
    """
    Catch scanned PDFs, which parse without error but yield no text.

    This is the failure mode worth guarding: without it, an empty document is
    stored as a success, and Phase 2 summarises nothing.
    """
    stripped = result.text.strip()

    if not stripped:
        raise ParseError(
            "No text could be extracted. If this is a scanned document, OCR is "
            "not supported in this prototype."
        )

    if extension == ".pdf" and result.pages > 0:
        # Page markers we added ourselves shouldn't count as content.
        marker_chars = sum(len(f"[Page {i}]\n") for i in range(1, result.pages + 1))
        real_chars = max(len(stripped) - marker_chars, 0)

        if real_chars / result.pages < MIN_CHARS_PER_PAGE:
            raise ParseError(
                f"Only {real_chars} characters found across {result.pages} pages — "
                "this looks like a scanned PDF with no text layer. OCR is not "
                "supported in this prototype."
            )
