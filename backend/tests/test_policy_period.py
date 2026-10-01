"""
v0.8.1: the policy period is read deterministically from document text and
attached to the period-bound facts of that document. No language model.
"""
from datetime import date

import pytest

import knowledge.service as kc
import property_policy_intelligence as ppi

P = ppi.extract_policy_period


def test_reads_the_period_line_used_by_the_benchmark_policies():
    assert P("Policy Number: X\nPeriod: 22 April 2026 to 31 March 2027\nCoverage:") == (
        date(2026, 4, 22), date(2027, 3, 31))


@pytest.mark.parametrize("text,expected", [
    ("Policy Period: 1 Jan 2025 - 31 Dec 2025", (date(2025, 1, 1), date(2025, 12, 31))),
    ("Period of Insurance: 2025-04-01 to 2026-03-31", (date(2025, 4, 1), date(2026, 3, 31))),
    ("Period: 1st April 2026 until 31st March 2027", (date(2026, 4, 1), date(2027, 3, 31))),
])
def test_other_common_period_wordings(text, expected):
    assert P(text) == expected


@pytest.mark.parametrize("text", [
    "",
    "no period here",
    "Period: 03/04/2026 to 02/04/2027",                      # numeric dates are ambiguous: not read
    "Period: 31 March 2027 to 22 April 2026",                # end before start
    "Period: 31 Smarch 2026 to 22 April 2027",               # unreadable month
    "Period: 1 April 2025 to 31 March 2026\nPeriod: 1 April 2026 to 31 March 2027",   # two different periods
    "Effective: 19 July 2026",                               # single date is not a period
])
def test_anything_unclear_is_unknown_not_guessed(text):
    assert P(text) is None


def test_the_same_period_stated_twice_is_still_that_period():
    t = "Period: 1 April 2026 to 31 March 2027\nPeriod: 1 April 2026 to 31 March 2027"
    assert P(t) == (date(2026, 4, 1), date(2027, 3, 31))


def test_only_period_bound_types_get_dated():
    assert "policy.coverage_limit" in ppi.PERIOD_BOUND_FACT_TYPES
    assert "property.sum_insured" in ppi.PERIOD_BOUND_FACT_TYPES
    for t in ("property.location", "property.occupancy", "property.construction", "property.risk_factors"):
        assert t not in ppi.PERIOD_BOUND_FACT_TYPES


def _submission(conn, org, documents):
    import uuid, db
    sub = db.create_submission(conn, organization_id=org, label=f"period-{uuid.uuid4().hex[:8]}")
    for filename, text in documents:
        row = db.create_document(
            conn, organization_id=org, carrier_id=None, filename=filename, file_type="pdf",
            size_bytes=len(text), pages=1, chars=len(text), status="parsed",
            error_message=None, extracted_text=text)
        db.set_document_submission(conn, str(row["id"]), str(sub["id"]))
    return str(sub["id"])


POLICY_DOC = ("[Page 1]\nProperty Policy\nPeriod: 22 April 2026 to 31 March 2027\n"
              "Property Damage Sum Insured: n500,000,000\nOccupancy: Warehouse\n")
SCHEDULE_DOC = "[Page 1]\nCoverage Schedule\nTotal Property Sum Insured: n500,000,000\n"


def test_end_to_end_period_dates_period_bound_facts_only(conn, test_org, monkeypatch):
    def model(prompt, *a, **k):
        if "Total Property Sum Insured" in prompt:
            return {"facts": [{"fact_type": "property.sum_insured", "value": "n500,000,000",
                               "snippet": "Total Property Sum Insured: n500,000,000", "page": 1}]}
        return {"facts": [
            {"fact_type": "property.sum_insured", "value": "n500,000,000",
             "snippet": "Property Damage Sum Insured: n500,000,000", "page": 1},
            {"fact_type": "property.occupancy", "value": "Warehouse",
             "snippet": "Occupancy: Warehouse", "page": 1}]}
    monkeypatch.setattr(ppi, "generate_json", model)

    sub = _submission(conn, test_org, [("01_property_policy.pdf", POLICY_DOC),
                                       ("02_coverage_schedule.pdf", SCHEDULE_DOC)])
    result = ppi.analyze_submission(conn, sub, test_org)

    by_type = {}
    for f in result["facts"]:
        by_type.setdefault(f["fact_type"], f)
    assert by_type["property.sum_insured"]["effective_from"] == date(2026, 4, 22)
    assert by_type["property.sum_insured"]["effective_to"] == date(2027, 3, 31)
    assert by_type["property.occupancy"]["effective_from"] is None       # not period-bound
    docs = {d["filename"]: d for d in result["documents_processed"]}
    assert docs["01_property_policy.pdf"]["policy_period"] == ["2026-04-22", "2027-03-31"]
    assert docs["02_coverage_schedule.pdf"]["policy_period"] is None     # none stated: unknown
    # the schedule restating the same value merges into the dated fact: one fact, two evidence rows
    sum_fact = by_type["property.sum_insured"]
    assert len(kc.get_evidence_for_fact(conn, str(sum_fact["id"]), test_org)) == 2


def test_undated_document_with_different_value_is_still_a_contradiction(conn, test_org, monkeypatch):
    def model(prompt, *a, **k):
        if "Total Property Sum Insured" in prompt:
            return {"facts": [{"fact_type": "property.sum_insured", "value": "n600,000,000",
                               "snippet": "Total Property Sum Insured: n600,000,000", "page": 1}]}
        return {"facts": [{"fact_type": "property.sum_insured", "value": "n500,000,000",
                           "snippet": "Property Damage Sum Insured: n500,000,000", "page": 1}]}
    monkeypatch.setattr(ppi, "generate_json", model)
    sched = SCHEDULE_DOC.replace("500,000,000", "600,000,000")
    sub = _submission(conn, test_org, [("01_property_policy.pdf", POLICY_DOC),
                                       ("02_coverage_schedule.pdf", sched)])
    result = ppi.analyze_submission(conn, sub, test_org)
    assert [c["fact_type"] for c in result["contradictions"]] == ["property.sum_insured"]
