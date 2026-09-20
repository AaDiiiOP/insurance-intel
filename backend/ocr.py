"""
Document quality gate and OCR fallback.

Everything before this module treated a scanned PDF as unreadable and
rejected it outright — a real, common case (a photographed policy schedule,
a faxed endorsement) that the tool simply refused. This is the fix: when the
text layer is missing or too thin, fall back to OCR page by page rather than
giving up.

Deliberately local and offline. Tesseract is a system binary plus two Python
bindings (pytesseract, pdf2image) — no new service, no API, no cost. That
matters here specifically: this is the one place a "just call a cloud vision
model" shortcut would have been easiest, and it is exactly what the
vertical-slice instruction ruled out.

The rule this module exists to serve, verbatim from the plan it came from:
"If Aurevia doesn't know, Aurevia says it doesn't know. No silent guessing."
Concretely: every OCR'd page carries the engine's own confidence score, a
low-confidence page is flagged rather than silently trusted, and nothing
here ever invents text that OCR could not actually read.
"""
import io
import logging

import pdf2image
import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

# Below this, a page's OCR is unreliable enough to flag for review rather
# than trust silently. Tesseract's own confidence scale is 0-100; chosen
# conservatively, since a false "looks fine" is worse than an unnecessary
# flag — the flag costs a reviewer one glance, a missed misread costs a
# wrong sum insured on a submission.
LOW_CONFIDENCE_THRESHOLD = 60.0

# Rendering a PDF page to an image at higher DPI reads more accurately but
# costs more time/memory. 300 is the standard OCR-quality tradeoff point.
OCR_DPI = 300


class OCRError(Exception):
    """Raised when OCR cannot run at all — not for a low-confidence result,
    which is a valid outcome and reported rather than raised."""


def ocr_pdf_page(image: Image.Image) -> dict:
    """
    OCR one rendered page image.

    Returns the extracted text plus a genuine confidence figure — pytesseract
    reports one figure per recognised word; averaging them is what "page
    confidence" means here. A page with no recognised words returns 0.0
    confidence and empty text rather than raising, since an unreadable page
    is a real, expected outcome for a poor scan.
    """
    try:
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRError(
            "Tesseract is not installed in this container. Rebuild the "
            "backend image after adding tesseract-ocr to its Dockerfile."
        ) from exc

    words = []
    confidences = []
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        text = text.strip()
        if not text:
            continue
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        # Tesseract marks non-text regions with -1; exclude them rather than
        # letting them drag the average down for a reason unrelated to
        # recognition quality.
        if conf_value < 0:
            continue
        words.append(text)
        confidences.append(conf_value)

    text = " ".join(words)
    avg_confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0.0

    return {"text": text, "confidence": avg_confidence, "word_count": len(words)}


def ocr_pdf(data: bytes) -> dict:
    """
    Render every page of a PDF to an image and OCR each one.

    Returns {text, pages, page_confidences, low_confidence_pages} — text
    carries the same "[Page N]" markers the native PDF parser uses, so
    downstream chunking and citation work identically regardless of whether
    a document's text came from its text layer or from OCR.
    """
    try:
        images = pdf2image.convert_from_bytes(data, dpi=OCR_DPI)
    except Exception as exc:
        raise OCRError(f"Could not render PDF pages for OCR: {exc}") from exc

    if not images:
        raise OCRError("PDF has no pages to OCR.")

    parts = []
    page_confidences = []
    low_confidence_pages = []

    for index, image in enumerate(images, start=1):
        result = ocr_pdf_page(image)
        parts.append(f"[Page {index}]\n{result['text']}")
        page_confidences.append(result["confidence"])
        if result["confidence"] < LOW_CONFIDENCE_THRESHOLD:
            low_confidence_pages.append(index)

    avg_confidence = (
        round(sum(page_confidences) / len(page_confidences), 1)
        if page_confidences else 0.0
    )

    return {
        "text": "\n\n".join(parts),
        "pages": len(images),
        "page_confidences": page_confidences,
        "avg_confidence": avg_confidence,
        "low_confidence_pages": low_confidence_pages,
    }


def quality_check_and_ocr(data: bytes, native_result) -> dict:
    """
    Decide whether a parsed PDF needs OCR, and run it if so.

    `native_result` is the ParsedDocument the normal text-layer parser
    already produced. This function does not repeat that parse — it only
    judges whether the result is thin enough to fall back on, and if so
    replaces it with an OCR'd one.

    Returns a dict describing what happened, always, whether or not OCR ran
    — so the caller has one shape to persist as document_quality regardless
    of which path was taken.
    """
    from parsers import MIN_CHARS_PER_PAGE

    stripped = native_result.text.strip()
    marker_chars = sum(
        len(f"[Page {i}]\n") for i in range(1, native_result.pages + 1)
    )
    real_chars = max(len(stripped) - marker_chars, 0)
    chars_per_page = real_chars / native_result.pages if native_result.pages else real_chars

    if chars_per_page >= MIN_CHARS_PER_PAGE:
        # Native text layer is good enough. OCR never runs — running it
        # anyway would cost real time for no benefit and would give a
        # confidently-wrong "OCR confidence" figure for text that was never
        # actually OCR'd.
        return {
            "used_ocr": False,
            "text": native_result.text,
            "pages": native_result.pages,
            "avg_ocr_confidence": None,
            "low_confidence_pages": [],
            "notes": [],
        }

    logger.info(
        "Native text layer thin (%.0f chars/page) — falling back to OCR",
        chars_per_page,
    )

    try:
        ocr_result = ocr_pdf(data)
    except OCRError as exc:
        # OCR itself failed to run (missing binary, corrupt render) — this is
        # different from "OCR ran but read badly", which is a normal, valid
        # outcome reported below. Re-raised so parse_file's existing
        # ParseError path still fires with a clear reason.
        raise

    notes = []
    if ocr_result["low_confidence_pages"]:
        notes.append(
            f"{len(ocr_result['low_confidence_pages'])} of {ocr_result['pages']} "
            f"page(s) had low OCR confidence: pages "
            f"{', '.join(str(p) for p in ocr_result['low_confidence_pages'])}."
        )
    if not ocr_result["text"].strip():
        notes.append(
            "OCR ran but recognised no text at all — the scan quality may be "
            "too poor to read."
        )

    return {
        "used_ocr": True,
        "text": ocr_result["text"],
        "pages": ocr_result["pages"],
        "avg_ocr_confidence": ocr_result["avg_confidence"],
        "low_confidence_pages": ocr_result["low_confidence_pages"],
        "notes": notes,
    }
