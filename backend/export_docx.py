"""
Turning a draft into a file a broker can attach to an email.

python-docx is already a dependency (the parsers read .docx), so writing one
costs nothing extra. PDF is deliberately not offered: it would mean putting
LibreOffice in the backend image for roughly half a gigabyte, and anyone with
Word can export a PDF in one click.

The one rule that matters here: a draft that has NOT been signed off is
stamped as such, in the document itself. A file leaves the tool and travels
on its own — the status badge in the UI does not travel with it, and an
unapproved letter arriving at an underwriter looking identical to an approved
one is exactly the confusion the review step exists to prevent.
"""
import io
import re
from datetime import date

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

INK = RGBColor(0x1C, 0x1C, 0x1A)
MUTED = RGBColor(0x5C, 0x5C, 0x57)
FLAG = RGBColor(0x9A, 0x3B, 0x2E)

# Blocks are separated by BLANK lines, so a single newline inside one is
# always deliberate — an enclosures list, or the two lines of a sign-off.
# Joining those into prose turned "Kind regards," and the signature line into
# one line, so line breaks within a block are preserved as written.


def _para(doc, text, *, size=10.5, bold=False, colour=INK, after=8, align=None):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(after)
    paragraph.paragraph_format.space_before = Pt(0)
    if align is not None:
        paragraph.alignment = align
    run = paragraph.add_run(text)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = colour
    run.font.name = "Calibri"
    return paragraph


# Heading lines in a risk summary — capitals, short, no sentence punctuation.
# Rendered bold so the sections are scannable, which is the whole point of
# that kind of document.
HEADING = re.compile(r"^[A-Z][A-Z &/,'()-]{3,60}$")


def build(
    *,
    title: str,
    body: str,
    status: str,
    client_name: str | None,
    carrier_name: str | None,
    approved_by: str | None,
    approved_at: str | None,
    kind: str = "submission",
) -> bytes:
    """
    Render one draft as a .docx and return the bytes.

    Takes the draft's text as given. Nothing is rewritten or re-generated
    here — what the broker read and signed off is exactly what gets written
    to the file.
    """
    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)

    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(56)
        section.left_margin = section.right_margin = Pt(56)

    # An unapproved letter says so on its face. Removing this line is a
    # deliberate act; mistaking a draft for a signed letter should not be.
    if status != "approved":
        _para(
            doc,
            "DRAFT — NOT YET APPROVED FOR SENDING",
            size=10,
            bold=True,
            colour=FLAG,
            after=14,
        )

    _para(doc, date.today().strftime("%d %B %Y"), size=10, colour=MUTED, after=14)

    if kind == "chase":
        # Addressed to the client, so a "To: carrier" line would be wrong —
        # and worse, misleading if the file were forwarded.
        if client_name:
            _para(doc, f"To: {client_name}", size=10.5, bold=True, after=12)
    else:
        if carrier_name:
            _para(doc, f"To: {carrier_name}", size=10.5, bold=True, after=2)
        if client_name:
            _para(doc, f"Re: {client_name}", size=10.5, colour=MUTED, after=12)

    if title:
        _para(doc, title, size=12.5, bold=True, after=14)

    # The body is written as the broker left it: blank lines become paragraph
    # breaks, single newlines become line breaks within a paragraph. Nothing
    # is reflowed, so what was reviewed is what gets written.
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        for position, line in enumerate(lines):
            last = position == len(lines) - 1
            if kind == "proposal" and HEADING.match(line):
                _para(doc, line, size=10, bold=True, after=4)
            else:
                # Tight between lines of one block, roomier between blocks.
                _para(doc, line, after=10 if last else 2)

    if status == "approved" and approved_by:
        doc.add_paragraph()
        stamp = f"Reviewed and approved by {approved_by}"
        if approved_at:
            stamp += f" on {approved_at[:10]}"
        _para(doc, stamp, size=9, colour=MUTED, after=0)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


FILENAME_PREFIX = {
    "submission": "Submission",
    "proposal": "Risk_Summary",
    "chase": "Documents_Needed",
}


def filename_for(*, client_name: str | None, carrier_name: str | None,
                 status: str, kind: str = "submission") -> str:
    """A filename that is obvious in a downloads folder six weeks later."""

    def slug(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
        return cleaned[:40] or "untitled"

    parts = [FILENAME_PREFIX.get(kind, "Document"), slug(client_name or "client")]
    if carrier_name and kind != "chase":
        parts.append(slug(carrier_name))
    if status != "approved":
        parts.append("DRAFT")
    parts.append(date.today().strftime("%Y%m%d"))
    return "_".join(parts) + ".docx"
