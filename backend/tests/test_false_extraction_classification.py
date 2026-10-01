"""
v0.7.0 tests: numeric grounding of extracted values, the evaluator's
false-extraction classification (reporting only), and the one-fact-many-
evidence behaviour of singleton repeats. No language model involved.
"""
import uuid
import pytest

import db
import knowledge.service as kc
import property_policy_intelligence as ppi
import eval_property_policy as ev

ENDORSEMENT = ("[Page 1]\nChange 1:\nPlant and machinery values reviewed. Revised machinery "
               "declared value: n150,000,000.\nProperty sum insured reference: n500,000,000.\n")
POLICY = ("[Page 1]\nProperty Damage Sum Insured: n500,000,000\n"
          "Business Interruption Sum Insured: n100,000,000\nDeductible: n1,000,000\n")


def test_number_stated_in_source_is_grounded():
    assert ppi.numeric_value_grounded("n500,000,000", ENDORSEMENT)
    assert ppi.numeric_value_grounded("₹500000000", ENDORSEMENT)


def test_sum_of_stated_figures_is_grounded():
    assert ppi.numeric_value_grounded("n600,000,000", POLICY)


def test_the_real_invented_value_is_not_grounded():
    # AUR-SYN-003: n1,650,000,000 is in none of the company's documents.
    assert not ppi.numeric_value_grounded("n1,650,000,000", ENDORSEMENT)
    assert not ppi.numeric_value_grounded("n1,650,000,000", POLICY)


def test_non_numeric_value_is_not_grounded():
    assert not ppi.numeric_value_grounded("unlimited", POLICY)


def _extract(items, text, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": items})
    ppi.reset_extraction_diagnostics()
    return ppi.extract_property_policy_facts(text=text)


def test_ungrounded_number_is_rejected_and_counted(monkeypatch):
    items = [
        {"fact_type": "policy.coverage_limit", "value": "n1,650,000,000",
         "snippet": "Revised machinery declared value: n150,000,000.", "page": 1},
        {"fact_type": "property.sum_insured", "value": "n500,000,000",
         "snippet": "Property sum insured reference: n500,000,000.", "page": 1},
    ]
    out = _extract(items, ENDORSEMENT, monkeypatch)
    assert [f["value"] for f in out] == ["n500,000,000"]
    assert ppi.snapshot_extraction_diagnostics()["counters"]["dropped_numeric_not_in_source"] == 1


def test_combined_limit_from_stated_figures_is_kept(monkeypatch):
    items = [{"fact_type": "policy.coverage_limit", "value": "n600,000,000",
              "snippet": "Property Damage Sum Insured: n500,000,000", "page": 1}]
    assert len(_extract(items, POLICY, monkeypatch)) == 1


def test_classifier_unsupported_contradictory_additional():
    c = ev.classify_false_extraction
    assert c("policy.coverage_limit", "n1,650,000,000", "needs_review", ENDORSEMENT) == "UNSUPPORTED"
    assert c("policy.coverage_limit", "n150,000,000", "contradiction_flagged", ENDORSEMENT) == "CONTRADICTORY_FACT"
    assert c("policy.coverage_limit", "n150,000,000", "needs_review", ENDORSEMENT) == "LEGITIMATE_ADDITIONAL_FACT"
    assert c("property.occupancy", "precision components", "contradiction_flagged",
             "The insured operates precision components.") == "CONTRADICTORY_FACT"
    assert c("property.occupancy", "warehouse", "needs_review", None) == "UNSUPPORTED"
    assert c("property.occupancy", "warehouse and logistics operations", "needs_review",
             "The insured operates logistics and warehousing.") == "UNSUPPORTED"


def test_singleton_repeat_is_one_fact_with_two_evidence_rows(conn, test_org, make_document):
    """Same value stated in two documents -> ONE persisted fact, TWO evidence
    rows (not two fact rows). A different value is a separate, flagged fact."""
    d1 = make_document(test_org, filename="01_property_policy.pdf", extracted_text=POLICY)
    d2 = make_document(test_org, filename="03_endorsement.pdf", extracted_text=ENDORSEMENT)
    entity = kc.create_entity(conn, organization_id=test_org, entity_type="asset", name="repeat test")

    def write(value, doc, snippet):
        return kc.reconcile_fact(
            conn, organization_id=test_org, entity_id=str(entity["id"]),
            fact_type="property.sum_insured", value=value, normalized_value=ppi._numeric(value),
            confidence={"value": 0.9, "type": "model_self_reported", "method": "test", "note": None},
            produced_by="test", document_id=doc, evidence_page=1, evidence_section=None,
            evidence_snippet=snippet, evidence_source="text_layer")

    a = write("n500,000,000", d1, "Property Damage Sum Insured: n500,000,000")
    b = write("n500,000,000", d2, "Property sum insured reference: n500,000,000.")
    assert str(a["id"]) == str(b["id"])
    assert len(kc.get_evidence_for_fact(conn, str(a["id"]), test_org)) == 2

    c = write("n150,000,000", d2, "Revised machinery declared value: n150,000,000.")
    assert str(c["id"]) != str(a["id"])
