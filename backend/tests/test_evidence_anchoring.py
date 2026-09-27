"""
Regression test #5 from the audit's requested suite: evidence snippet
anchoring, including the chunk-boundary case Issue 2's fix addresses.

Calls property_policy_intelligence.verify_snippet() directly — a pure,
deterministic function with no LLM call, so this proves the exact bug and
fix without needing a real (or stubbed) model.
"""
from property_policy_intelligence import split_into_chunks, verify_snippet


def test_fabricated_snippet_is_rejected():
    source = "The property damage sum insured is n350,000,000 for this location."
    fabricated = "The building was completely destroyed by a meteor strike."
    assert verify_snippet(fabricated, source) is False


def test_real_verbatim_snippet_is_accepted():
    source = "The property damage sum insured is n350,000,000 for this location."
    real = "The property damage sum insured is n350,000,000 for this location."
    assert verify_snippet(real, source) is True


def test_snippet_with_whitespace_noise_is_accepted_via_fuzzy_match():
    source = "The property damage sum insured is n350,000,000 for this location."
    noisy = "The  property damage   sum insured is\nn350,000,000  for this location."
    assert verify_snippet(noisy, source) is True


def test_snippet_spanning_a_chunk_boundary_is_verified_against_full_document():
    """
    The exact scenario Issue 2 fixes: a real sentence that legitimately
    starts in one chunk and continues into the next. Checked against a
    single chunk alone, it must fail (proving the old bug); checked against
    the full document text, it must succeed (proving the fix).
    """
    page1_body = "Filler text. " * 800
    target_sentence = (
        "The total property damage sum insured under this policy is "
        "confirmed at INR 999,000,000 across all locations combined."
    )
    page1 = f"[Page 1]\n{page1_body}\n{target_sentence[:40]}"
    page2 = f"[Page 2]\n{target_sentence[40:]}\nMore filler after."
    full_text = page1 + "\n" + page2

    chunks = split_into_chunks(full_text, max_chars=len(page1) + 10)
    assert len(chunks) == 2, "test setup must actually produce two chunks"

    claimed_snippet = target_sentence

    # Before the fix: verification only ever saw the chunk a fact was
    # extracted from. The sentence starts in chunk 0, so that is what the
    # old code path checked against — and it must fail, since the chunk
    # does not contain the whole sentence.
    assert verify_snippet(claimed_snippet, chunks[0]) is False, (
        "sanity check: the chunk-only check must fail for a boundary-"
        "spanning snippet, or this test isn't exercising the real bug"
    )

    # After the fix: property_policy_intelligence.extract_property_policy_facts
    # is called with full_text=<the whole document>, so verification runs
    # against the whole document instead of the one chunk.
    assert verify_snippet(claimed_snippet, full_text) is True, (
        "verifying against the full document text must succeed for a "
        "legitimate snippet that spans a chunk boundary"
    )


def test_empty_snippet_or_source_is_never_verified():
    assert verify_snippet("", "some source text") is False
    assert verify_snippet("some snippet", "") is False
    assert verify_snippet(None, "some source text") is False
