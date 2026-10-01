"""
v0.6.1 regression tests for the 4 silently wrong property.risk_factors facts
found by the 3-company Phase 1A quick check (2026-09-30): overall risk ratings
and positive protection statements from 04_risk_survey.pdf were extracted as
risk factors although the prompt says they are not.

These prove the deterministic validity rule; they say nothing about the
model. The scoring criteria are untouched.
"""
import pytest
import property_policy_intelligence as ppi

REJECTED = [
    "Moderate risk rating",
    "Low overall risk rating",
    "Low risk rating",
    "Overall risk rating: Moderate",
    "Fire protection: hydrants and portable extinguishers reported",
    "Protection: sprinklers installed",
]
KEPT = [
    "Machine shop operations with metalworking fluids present",
    "Clean-room HVAC dependency and solvent storage",
    "High-density pallet racking and forklift traffic",
    "Timber storage on site",
    "Fire protection: no hydrants on site",
    "Fire protection inadequate for stock height",
    "Flood zone",
]


@pytest.mark.parametrize("v", REJECTED)
def test_rating_or_protection_is_not_a_risk_factor(v):
    assert ppi.is_non_hazard_risk_factor(v)


@pytest.mark.parametrize("v", KEPT)
def test_real_hazards_are_kept(v):
    assert not ppi.is_non_hazard_risk_factor(v)


def _run(items, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": items})
    ppi.reset_extraction_diagnostics()
    text = "[Page 1]\nObservations\nOverall risk rating: Moderate.\nTimber storage on site\n"
    return ppi.extract_property_policy_facts(text=text)


def test_filter_applies_only_to_risk_factors_and_is_counted(monkeypatch):
    items = [
        {"fact_type": "property.risk_factors", "value": "Moderate risk rating",
         "evidence_snippet": "Overall risk rating: Moderate.", "page": 1},
        {"fact_type": "property.risk_factors", "value": "Timber storage on site",
         "evidence_snippet": "Timber storage on site", "page": 1},
    ]
    out = _run(items, monkeypatch)
    assert [f["value"] for f in out] == ["Timber storage on site"]
    assert ppi.snapshot_extraction_diagnostics()["counters"]["dropped_non_hazard_risk_factor"] == 1


# --- v0.7.1: policy conditions are not endorsements --------------------

POLICY_NO_ENDORSEMENT = (
    "[Page 1]\nProperty Policy\nConditions\n"
    "Fire protection systems must be maintained. Material changes in occupancy "
    "or operations must be notified.\n")
ENDORSEMENT_DOC = (
    "[Page 1]\nPolicy Endorsement\nChange 1:\nPlant and machinery values reviewed.\n")


def _extract(items, text, monkeypatch):
    monkeypatch.setattr(ppi, "generate_json", lambda *a, **k: {"facts": items})
    ppi.reset_extraction_diagnostics()
    return ppi.extract_property_policy_facts(text=text)


def test_conditions_in_policy_document_are_not_endorsements(monkeypatch):
    items = [{"fact_type": "policy.endorsement", "value": "Fire protection systems must be maintained.",
              "snippet": "Fire protection systems must be maintained.", "page": 1}]
    assert _extract(items, POLICY_NO_ENDORSEMENT, monkeypatch) == []
    assert ppi.snapshot_extraction_diagnostics()["counters"]["dropped_endorsement_without_endorsement_text"] == 1


def test_real_endorsement_in_endorsement_document_is_kept(monkeypatch):
    items = [{"fact_type": "policy.endorsement", "value": "Plant and machinery values reviewed.",
              "snippet": "Plant and machinery values reviewed.", "page": 1}]
    assert len(_extract(items, ENDORSEMENT_DOC, monkeypatch)) == 1
