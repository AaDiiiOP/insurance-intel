"""v0.10.1 rules from the v0.10.0 real-Qwen 4-company run."""
import property_policy_intelligence as ppi

ENDORSEMENT = """Endorsement SYN-END-0002-01
Change 1:
Plant and machinery values reviewed. Revised machinery declared value: n90,000,000.
Policy Reference:
Property sum insured reference in this synthetic scenario: n300,000,000.
Change 2:
Additional named insured endorsement: facility manager added as a notice-of-loss contact.
"""
SCHEDULE = """Declared Values
Buildings and Civil Works: n165,000,000
Plant and Machinery: n90,000,000
Total Property Sum Insured: n300,000,000
"""


def test_endorsement_label_is_stripped():
    c = ppi.clean_endorsement_value
    assert c("Endorsement SYN-END-0002-01: Additional named insured endorsement: x") == \
        "Additional named insured endorsement: x"
    assert c("Change 2: Plant and machinery values reviewed") == "Plant and machinery values reviewed"
    assert c("Endorsement No. 3 - Flood cover added") == "Flood cover added"
    # the word "endorsement" inside the content is kept
    assert c("Additional named insured endorsement: x") == "Additional named insured endorsement: x"
    assert c("Endorsement SYN-END-0002-01:") == "Endorsement SYN-END-0002-01:"   # never emptied


def test_limit_must_be_stated_as_limit():
    f = ppi.numeric_on_limit_line
    assert f("300,000,000", ENDORSEMENT) and f("300,000,000", SCHEDULE)
    assert not f("90,000,000", ENDORSEMENT)          # valuation note
    assert not f("90,000,000", SCHEDULE)             # per-category declared value
    assert not f("165,000,000", SCHEDULE)
    assert f("720,000,000", SCHEDULE)                # not stated verbatim -> left to grounding


def test_rules_applied_in_extraction(monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": [
        {"fact_type": "policy.coverage_limit", "value": "90,000,000", "page": 1,
         "snippet": "Revised machinery declared value: n90,000,000."},
        {"fact_type": "policy.endorsement", "value": "Endorsement SYN-END-0002-01: Additional named insured endorsement: facility manager added as a notice-of-loss contact.",
         "page": 1, "snippet": "Additional named insured endorsement"},
    ]})
    out = ppi.extract_property_policy_facts(text="[Page 1]\n" + ENDORSEMENT)
    assert not [f for f in out if f["fact_type"] == "policy.coverage_limit"]
    ends = [f["value"] for f in out if f["fact_type"] == "policy.endorsement"]
    assert ends == ["Additional named insured endorsement: facility manager added as a notice-of-loss contact."]
