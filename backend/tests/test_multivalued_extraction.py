"""
Regression tests for the multi-valued extraction gap found by the first
authoritative real-Qwen Phase 1A run (2026-09-29): policy.coverage came back
0/40 and property.risk_factors 0/20 (policy.endorsement 3/40) even though the
values are stated verbatim in the source documents.

The fix is a focused "recovery" pass (property_policy_intelligence.
extract_with_recovery), a rewritten main prompt, and evaluator evidence
scoring that reads what the Knowledge Core actually persisted.

WHAT THESE TESTS PROVE, AND WHAT THEY DO NOT.
They prove STRUCTURE: given a model that answers a certain way, the pipeline
keeps every distinct multi-valued item, never overwrites singleton facts,
never calls the model again when it does not need to, abstains rather than
inventing, and links real evidence. The "model" here is a scripted HTTP server
(tests/stub_ollama.py's responder hook) — real HTTP, real extraction code,
real Postgres, but NOT a language model. They say nothing about how well
qwen2.5:7b itself extracts coverage or endorsements; only a real-model run of
eval_property_policy.py can show that.
"""
import json
import uuid

import pytest

import db
import knowledge.service as kc
import llm
import property_policy_intelligence as ppi
from tests.stub_ollama import StubOllamaServer

PORT = 11439

POLICY_TEXT = (
    "[Page 1]\n"
    "Property Policy\n"
    "Location: 12 Harbour Road, Kochi\n"
    "Perils Covered: Fire and Allied Perils, Theft\n"
    "Sum Insured: n500,000,000\n"
    "Risk Factors: Timber storage on site\n"
)

ENDORSEMENT_TEXT = (
    "[Page 1]\n"
    "Policy Endorsement\n"
    "Change 1:\n"
    "Machinery values reviewed. Revised machinery declared value: n90,000,000.\n"
    "Change 2:\n"
    "Additional contact added: the site manager is a notice-of-loss contact.\n"
)


def _fact(fact_type, value, snippet=None, page=1):
    return {"fact_type": fact_type, "value": value, "page": page, "section": None,
            "snippet": snippet if snippet is not None else value,
            "self_confidence": 0.9, "reasoning": "scripted"}


class Scripted:
    """Records every prompt the model receives and answers per stage."""

    def __init__(self, stage_a=None, recovery=None):
        self.stage_a = stage_a or []
        self.recovery = recovery or []
        self.stage_a_calls = 0
        self.recovery_calls = 0
        self.recovery_targets = []

    def __call__(self, prompt):
        if "TARGET FACT TYPES" in prompt:
            self.recovery_calls += 1
            block = prompt.split("TARGET FACT TYPES", 1)[1].split("What each one means", 1)[0]
            self.recovery_targets.append(
                {ln.strip() for ln in block.splitlines() if ln.strip().startswith(("policy.", "property."))}
            )
            return {"facts": self.recovery}
        if 'Valid values for "fact_type"' in prompt:
            self.stage_a_calls += 1
            return {"facts": self.stage_a}
        return None


@pytest.fixture
def scripted_model(monkeypatch):
    """Factory: start a scripted Ollama-compatible server, point llm.py at it."""
    servers = []

    def _start(scripted: Scripted):
        server = StubOllamaServer(port=PORT, responder=scripted)
        server.start()
        servers.append(server)
        monkeypatch.setattr(llm, "OLLAMA_HOST", f"http://127.0.0.1:{PORT}")
        return scripted

    yield _start
    for server in servers:
        server.stop()


def _extract(text):
    return ppi.extract_with_recovery(text=text, full_text=text)


def _values(facts, fact_type):
    return [f["value"] for f in facts if f["fact_type"] == fact_type]


# --- 1 & 2: every distinct multi-valued item is preserved ---------------

def test_multiple_coverages_each_preserved(scripted_model):
    model = scripted_model(Scripted(
        stage_a=[_fact("property.location", "12 Harbour Road, Kochi",
                       "Location: 12 Harbour Road, Kochi")],
        recovery=[
            _fact("policy.coverage", "Fire and Allied Perils",
                  "Perils Covered: Fire and Allied Perils, Theft"),
            _fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft"),
        ],
    ))
    facts = _extract(POLICY_TEXT)
    assert sorted(_values(facts, "policy.coverage")) == ["Fire and Allied Perils", "Theft"]
    assert model.recovery_calls == 1


def test_multiple_endorsements_each_preserved(scripted_model):
    scripted_model(Scripted(
        stage_a=[],
        recovery=[
            _fact("policy.endorsement", "Machinery values reviewed. Revised machinery declared value: n90,000,000.",
                  "Machinery values reviewed. Revised machinery declared value: n90,000,000."),
            _fact("policy.endorsement", "Additional contact added: the site manager is a notice-of-loss contact.",
                  "Additional contact added: the site manager is a notice-of-loss contact."),
        ],
    ))
    facts = _extract(ENDORSEMENT_TEXT)
    assert len(_values(facts, "policy.endorsement")) == 2


# --- 3: recovery must not replace or suppress singleton facts ------------

def test_recovery_does_not_replace_or_suppress_singleton_facts(scripted_model):
    scripted_model(Scripted(
        stage_a=[
            _fact("property.location", "12 Harbour Road, Kochi", "Location: 12 Harbour Road, Kochi"),
            _fact("property.sum_insured", "n500,000,000", "Sum Insured: n500,000,000"),
        ],
        recovery=[_fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft")],
    ))
    facts = _extract(POLICY_TEXT)
    assert _values(facts, "property.location") == ["12 Harbour Road, Kochi"]
    assert _values(facts, "property.sum_insured") == ["n500,000,000"]
    assert _values(facts, "policy.coverage") == ["Theft"]


# --- recovery is only used when needed -----------------------------------

def test_recovery_not_called_when_stage_a_already_found_the_type(scripted_model):
    model = scripted_model(Scripted(
        stage_a=[_fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft"),
                 _fact("property.risk_factors", "Timber storage on site", "Risk Factors: Timber storage on site")],
    ))
    facts = _extract(POLICY_TEXT)
    assert model.recovery_calls == 0
    assert len(facts) == 2


def test_recovery_not_called_when_text_has_no_labelled_section(scripted_model):
    model = scripted_model(Scripted(stage_a=[]))
    _extract("[Page 1]\nInvoice for consulting services. Amount due: n5,000.\n")
    assert model.recovery_calls == 0


def test_recovery_only_targets_missing_types(scripted_model):
    model = scripted_model(Scripted(
        stage_a=[_fact("property.risk_factors", "Timber storage on site", "Risk Factors: Timber storage on site")],
    ))
    _extract(POLICY_TEXT)
    assert model.recovery_calls == 1
    assert model.recovery_targets[0] == {"policy.coverage"}  # risk_factors already found


# --- 5: abstain rather than invent ---------------------------------------

def test_unsupported_recovered_item_is_dropped_not_written(scripted_model):
    ppi.reset_extraction_diagnostics()
    scripted_model(Scripted(
        recovery=[
            _fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft"),
            _fact("policy.coverage", "Flood and Earthquake",
                  "Perils Covered: Flood and Earthquake up to n900,000,000"),  # not in the document
        ],
    ))
    facts = _extract(POLICY_TEXT)
    assert _values(facts, "policy.coverage") == ["Theft"]
    assert ppi.EXTRACTION_DIAGNOSTICS["recovery_unverified_dropped"] == 1


def test_recovery_returning_nothing_invents_nothing(scripted_model):
    scripted_model(Scripted(stage_a=[], recovery=[]))
    assert _extract(POLICY_TEXT) == []


def test_recovery_ignores_types_it_was_not_asked_for(scripted_model):
    ppi.reset_extraction_diagnostics()
    scripted_model(Scripted(
        recovery=[_fact("property.location", "12 Harbour Road, Kochi", "Location: 12 Harbour Road, Kochi"),
                  _fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft")],
    ))
    facts = _extract(POLICY_TEXT)
    assert _values(facts, "property.location") == []
    assert _values(facts, "policy.coverage") == ["Theft"]
    assert ppi.EXTRACTION_DIAGNOSTICS["dropped_out_of_scope_type"] == 1


def test_duplicate_recovered_items_are_collapsed(scripted_model):
    scripted_model(Scripted(
        recovery=[_fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft"),
                  _fact("policy.coverage", "theft ", "Perils Covered: Fire and Allied Perils, Theft")],
    ))
    assert _values(_extract(POLICY_TEXT), "policy.coverage") == ["Theft"]


# --- diagnostics: silent drops must now be visible ------------------------

def test_wrongly_named_fact_type_is_dropped_but_counted_by_name(scripted_model):
    ppi.reset_extraction_diagnostics()
    scripted_model(Scripted(stage_a=[_fact("policy.perils", "Theft", "Perils Covered: Fire and Allied Perils, Theft")]))
    _extract("[Page 1]\nLocation: somewhere\n")
    snap = ppi.snapshot_extraction_diagnostics()
    assert snap["counters"]["dropped_unknown_fact_type"] == 1
    assert snap["dropped_fact_type_names"] == {"policy.perils": 1}


# --- 4: end to end through the Knowledge Core: every fact has evidence ---

def _make_submission(conn, org, documents):
    submission = db.create_submission(conn, organization_id=org, label=f"multi-{uuid.uuid4().hex[:8]}")
    for filename, text in documents:
        row = db.create_document(
            conn, organization_id=org, carrier_id=None, filename=filename, file_type="pdf",
            size_bytes=len(text), pages=1, chars=len(text), status="parsed",
            error_message=None, extracted_text=text,
        )
        db.set_document_submission(conn, str(row["id"]), str(submission["id"]))
    return str(submission["id"])


def test_recovered_facts_reach_knowledge_core_with_real_evidence(conn, test_org, scripted_model):
    scripted_model(Scripted(
        stage_a=[_fact("property.sum_insured", "n500,000,000", "Sum Insured: n500,000,000")],
        recovery=[
            _fact("policy.coverage", "Fire and Allied Perils", "Perils Covered: Fire and Allied Perils, Theft"),
            _fact("policy.coverage", "Theft", "Perils Covered: Fire and Allied Perils, Theft"),
            _fact("property.risk_factors", "Timber storage on site", "Risk Factors: Timber storage on site"),
        ],
    ))
    submission_id = _make_submission(conn, test_org, [("01_property_policy.pdf", POLICY_TEXT)])
    analysis = ppi.analyze_submission(conn, submission_id, test_org)

    by_type = {}
    for fact in analysis["facts"]:
        by_type.setdefault(fact["fact_type"], []).append(fact)
    assert len(by_type["policy.coverage"]) == 2
    assert len(by_type["property.risk_factors"]) == 1
    assert len(by_type["property.sum_insured"]) == 1

    for fact in analysis["facts"]:
        evidence = kc.get_evidence_for_fact(conn, str(fact["id"]), test_org)
        assert evidence, f"{fact['fact_type']} was written with no evidence"
        assert evidence[0]["document_id"] is not None
        assert (evidence[0]["snippet"] or "").strip()


# --- evaluator evidence scoring reads persisted evidence -----------------

def test_evaluator_scores_persisted_evidence_and_rejects_fabricated_snippet(conn, test_org, make_document):
    import eval_property_policy as ev

    doc_id = make_document(test_org, filename="01_property_policy.pdf", extracted_text=POLICY_TEXT)
    entity = kc.create_entity(conn, organization_id=test_org, entity_type="policy", name="scoring test")

    def write(value, snippet):
        return kc.reconcile_fact(
            conn, organization_id=test_org, entity_id=str(entity["id"]), fact_type="policy.coverage",
            value=value, normalized_value=None,
            confidence={"value": 0.9, "type": "model_self_reported", "method": "test", "note": None},
            produced_by="test", document_id=doc_id, evidence_page=1, evidence_section=None,
            evidence_snippet=snippet, evidence_source="text_layer",
        )

    good = write("Theft", "Perils Covered: Fire and Allied Perils, Theft")
    fabricated = write("Flood", "Perils Covered: Flood and Earthquake up to n900,000,000")
    gt = {"fact_type": "policy.coverage", "value": "Theft",
          "source_document": "AUR-SYN-001_01_property_policy.pdf", "page": 1}

    cache = {}
    scored_good = ev.score_evidence(conn, test_org, good, gt, cache)
    scored_bad = ev.score_evidence(conn, test_org, fabricated, None, cache)

    assert scored_good["evidence_exists"] and scored_good["evidence_has_document"]
    assert scored_good["snippet_verified"] is True
    assert scored_good["evidence_page"] == 1
    assert scored_good["evidence_page_correct"] is True

    assert scored_bad["evidence_exists"] is True
    assert scored_bad["snippet_verified"] is False  # not treated as verified
    assert scored_bad["evidence_page_correct"] is None  # nothing to judge it against


def test_evaluator_counts_fact_with_no_evidence_as_not_verified(conn, test_org, make_entity):
    import eval_property_policy as ev

    entity_id = make_entity(test_org, entity_type="policy")
    fact = kc.create_fact(
        conn, organization_id=test_org, entity_id=entity_id, fact_type="policy.coverage",
        value="Theft", normalized_value=None,
        confidence={"value": 0.5, "type": "model_self_reported", "method": "test", "note": None},
        produced_by="test",
    )
    scored = ev.score_evidence(conn, test_org, fact, None, {})
    assert scored["evidence_exists"] is False
    assert scored["snippet_verified"] is False
