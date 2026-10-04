"""v0.8.3: a policy.coverage must be named as cover in its source."""
import property_policy_intelligence as ppi

POLICY = """Coverage:
Perils Covered: Property Damage, Business Interruption
Property Damage Sum Insured: 300,000,000
"""
ENDORSEMENT = """Change 1:
Plant and machinery values reviewed. Revised machinery declared value: 90,000,000.
Change 2:
No change to sum insured or coverage terms.
"""


def test_real_coverages_kept():
    assert ppi.coverage_named_as_cover("Property Damage", POLICY)
    assert ppi.coverage_named_as_cover("Business Interruption", POLICY)


def test_valuation_note_is_not_a_coverage():
    assert not ppi.coverage_named_as_cover("Plant and machinery", ENDORSEMENT)


def test_wrapped_list_uses_previous_line():
    assert ppi.coverage_named_as_cover("Machinery Breakdown", "Perils Covered: Fire,\nMachinery Breakdown\n")


def test_value_not_in_text_is_left_to_evidence_checks():
    assert ppi.coverage_named_as_cover("Fire", ENDORSEMENT)


def test_rule_applied_in_extraction(monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": [
        {"fact_type": "policy.coverage", "value": "Plant and machinery",
         "evidence": {"page": 1, "snippet": "Plant and machinery values reviewed."}}]})
    before = ppi.EXTRACTION_DIAGNOSTICS["dropped_coverage_not_named_as_cover"]
    out = ppi.extract_property_policy_facts(text="[Page 1]\n" + ENDORSEMENT)
    assert not [f for f in out if f["fact_type"] == "policy.coverage"]
    assert ppi.EXTRACTION_DIAGNOSTICS["dropped_coverage_not_named_as_cover"] == before + 1
