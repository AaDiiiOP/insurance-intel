"""
Regression tests #8 and #9 from the audit's requested suite:

8. Page-specific OCR confidence: a fact's confidence matches its own
   evidence page's confidence, not a document-wide average.
9. Long-document chunked extraction: a fact placed past character 12,000 in
   a long document is still correctly extracted (no silent truncation).

These need something that behaves like a real model call
(property_policy_intelligence.extract_property_policy_facts ->
llm.generate_json -> a real HTTP POST). A real Ollama instance is not
reachable in this sandbox (confirmed: connections to both localhost:11434
and host.docker.internal:11434 fail), so these run against the SAME kind of
Ollama-compatible stub server used in the prior Phase 1A hardening pass
(tests/stub_ollama.py) — a real HTTP server, deterministic regex extraction
instead of a language model, exercising the REAL extraction code path
(chunking, HTTP call, JSON parsing, page/confidence resolution) end to end,
never a Python-level mock of property_policy_intelligence itself.
"""
import llm
from property_policy_intelligence import (
    MAX_CHUNK_CHARS,
    extract_property_policy_facts,
    split_into_chunks,
)


def test_page_specific_ocr_confidence_not_document_average(monkeypatch, stub_ollama):
    """A fact from a clean page and a fact from a barely-legible page must
    carry THEIR OWN page's OCR confidence, not the shared document-wide
    average — built with deliberately different per-page confidences
    (95% vs. 40%, average 67.5%), matching the original Issue 7 test case."""
    monkeypatch.setattr(llm, "OLLAMA_HOST", "http://127.0.0.1:11434")

    text = (
        "[Page 1]\n"
        "Location: 42 Industrial Estate, Chennai.\n"
        "[Page 2]\n"
        "Overall risk rating: Moderate\n"
    )
    page_confidences = {"1": 95.0, "2": 40.0}
    document_average = 67.5

    facts = extract_property_policy_facts(
        text=text, ocr_confidence=document_average, page_confidences=page_confidences,
        full_text=text,
    )
    assert facts, "the stub must have found at least one fact for this test to mean anything"

    by_type = {f["fact_type"]: f for f in facts}
    assert "property.location" in by_type
    assert "property.risk_factors" in by_type

    location_fact = by_type["property.location"]
    risk_fact = by_type["property.risk_factors"]

    assert location_fact["evidence"]["page"] == 1
    assert risk_fact["evidence"]["page"] == 2

    # Confidence values are stored as a 0..1 fraction of the percentage.
    assert location_fact["confidence"]["value"] == 0.95, (
        "page 1's fact must carry page 1's own 95% confidence"
    )
    assert risk_fact["confidence"]["value"] == 0.40, (
        "page 2's fact must carry page 2's own 40% confidence, not the "
        "document-wide 67.5% average"
    )
    assert location_fact["confidence"]["value"] != document_average / 100.0
    assert risk_fact["confidence"]["value"] != document_average / 100.0


def test_long_document_fact_past_12000_chars_is_extracted(monkeypatch, stub_ollama):
    """
    The Issue 6 regression: a document whose target fact sits well past the
    old `text[:12000]` truncation point must still be found. Built with the
    fact deliberately placed past character 22,000, matching the original
    fix's own test case, and run through split_into_chunks() +
    extract_property_policy_facts() per chunk exactly as
    property_policy_intelligence.analyze_submission() does.
    """
    monkeypatch.setattr(llm, "OLLAMA_HOST", "http://127.0.0.1:11434")

    filler = "This paragraph is unrelated filler text about general policy terms. " * 40
    pages = [f"[Page {i}]\n{filler}" for i in range(1, 10)]
    target_page = "[Page 10]\nTotal property sum insured n999,000,000 for this location.\n"
    document_text = "\n".join(pages) + "\n" + target_page

    # Confirm the test actually places the fact past the old truncation
    # point — otherwise this would not exercise the bug at all.
    target_pos = document_text.index("Total property sum insured")
    assert target_pos > 22000, f"test setup: target only at {target_pos}, need > 22000"
    assert len(document_text) > MAX_CHUNK_CHARS, "test setup must exceed one chunk"

    chunks = split_into_chunks(document_text)
    assert len(chunks) > 1, "test setup must actually produce multiple chunks"

    extracted = []
    for chunk in chunks:
        extracted.extend(extract_property_policy_facts(
            text=chunk, ocr_confidence=None, page_confidences=None,
            full_text=document_text,
        ))

    matches = [f for f in extracted if f["fact_type"] == "property.sum_insured"]
    assert matches, "the fact placed past character 22,000 must still be extracted"
    assert any("999,000,000" in m["value"] for m in matches)
    assert any(m["evidence"]["page"] == 10 for m in matches)


def test_split_into_chunks_never_drops_text():
    """A direct check on the chunker itself: every character of the input
    must appear in exactly one output chunk — the property this whole fix
    depends on."""
    filler = "X" * 50000
    pages = [f"[Page {i}]\n{filler}\n" for i in range(1, 6)]
    text = "".join(pages)

    chunks = split_into_chunks(text)
    assert "".join(chunks) == text
    assert len(chunks) > 1
