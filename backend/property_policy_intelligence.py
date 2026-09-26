"""
Property Policy Intelligence — the first vertical slice built on top of the
Knowledge Core (backend/knowledge/).

Proves the Knowledge Core's shapes (entities, facts, evidence, relationships,
reconciliation) hold up against a real multi-document submission, the same
way Document Intelligence's coverage-limit extraction proved out
extraction_facts before anything else was built on it.

    submission (Property Policy + Coverage Schedule + Endorsement +
    Risk Survey, as separate documents)
      -> for each document: extract property.* / policy.* facts
      -> get-or-create a Business entity and an Asset entity (the property)
         and a Policy entity, linked Business-[owns]->Asset,
         Business-[has_policy]->Policy
      -> every fact attaches to the Asset entity (property.*) or the Policy
         entity (policy.*), with kc_evidence pointing at the real
         document/page/snippet
      -> a fact that disagrees with the current fact already on record for
         the same entity+fact_type goes through the SAME
         reconcile_fact()/contradiction-flagging path as everything else in
         the Knowledge Core — reused, not reimplemented.

This module deliberately does NOT touch extraction_facts, doc_intelligence.py,
or the coverage-limit slice — additive only, per the project's Phase 1 scope.
"""
import logging
import re

from llm import LLMError, generate_json, load_prompt

import db
import knowledge.service as kc

logger = logging.getLogger(__name__)

# Which fact types are comparable numeric quantities — normalized_value is
# populated for these so reconciliation compares numbers, not strings. Every
# other fact type in FACT_TYPES is free text, compared as text (see
# knowledge.service._values_agree).
NUMERIC_FACT_TYPES = {"property.sum_insured", "policy.coverage_limit", "policy.deductible"}

# Which entity a fact_type's namespace belongs to. Anything under "policy."
# attaches to the Policy entity; anything under "property." attaches to the
# Asset entity (the physical property itself — see the module docstring in
# knowledge/entities.py for why "Asset" rather than a dedicated "Property"
# entity type: Property is not in the fixed, deliberately-small entity list).
def _target_entity_kind(fact_type: str) -> str:
    if fact_type.startswith("policy."):
        return "policy"
    if fact_type.startswith("property."):
        return "asset"
    raise ValueError(f"Unrecognised fact_type namespace: {fact_type}")


def _numeric(value: str) -> float | None:
    """Same stripping rule as doc_intelligence._numeric_value — pulls a
    comparable number out of a free-text figure like "₹350,000,000"."""
    if not value:
        return None
    digits = re.sub(r"[^\d.]", "", value)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def extract_property_policy_facts(*, text: str, ocr_confidence: float | None = None) -> list[dict]:
    """
    Extract every property/policy fact this ONE document's text states,
    using the same LLM-calling convention as
    doc_intelligence.extract_coverage_limit (prompt file, generate_json,
    OCR-vs-model confidence typing).

    Returns [] when nothing usable was found or the model call failed — no
    fact is invented, same "no silent guessing" rule as Phase 0. Each
    returned dict has: fact_type, value, normalized_value, unit, evidence
    ({page, section, snippet, source}), confidence ({value, type, method,
    note}), produced_by.
    """
    try:
        data = generate_json(
            load_prompt("extract_property_facts", document_text=text[:12000])
        )
    except LLMError as exc:
        logger.warning("Property/policy extraction failed: %s", exc)
        return []

    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        return []

    results = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        fact_type = str(item.get("fact_type") or "").strip()
        value = str(item.get("value") or "").strip()
        if fact_type not in _ALL_FACT_TYPES or not value:
            continue

        self_confidence = item.get("self_confidence")
        try:
            self_confidence = max(0.0, min(1.0, float(self_confidence)))
        except (TypeError, ValueError):
            self_confidence = 0.5

        claimed_page = item.get("page")
        page = None
        if claimed_page:
            try:
                claimed_page = int(claimed_page)
                if f"[Page {claimed_page}]" in text:
                    page = claimed_page
            except (TypeError, ValueError):
                pass

        reasoning = str(item.get("reasoning") or "").strip()

        if ocr_confidence is not None:
            confidence = {
                "value": round(ocr_confidence / 100.0, 3),
                "type": "ocr_engine",
                "method": "tesseract",
                "note": (
                    f"Page OCR confidence {ocr_confidence:.0f}%. Model reasoning: {reasoning}"
                    if reasoning else f"Page OCR confidence {ocr_confidence:.0f}%."
                ),
            }
        else:
            confidence = {
                "value": self_confidence,
                "type": "model_self_reported",
                "method": "qwen2.5:7b_property_policy_extraction_v1",
                "note": reasoning or None,
            }

        results.append({
            "fact_type": fact_type,
            "value": value[:300],
            "normalized_value": _numeric(value) if fact_type in NUMERIC_FACT_TYPES else None,
            "evidence": {
                "page": page,
                "section": (str(item.get("section")).strip()[:200] or None) if item.get("section") else None,
                "snippet": str(item.get("snippet") or "").strip()[:500] or None,
                "source": "ocr" if ocr_confidence is not None else "text_layer",
            },
            "confidence": confidence,
            "produced_by": "model:qwen2.5:7b_property_policy_extraction_v1",
        })
    return results


_ALL_FACT_TYPES = {
    "property.location", "property.occupancy", "property.construction",
    "property.sum_insured", "property.risk_factors",
    "policy.coverage", "policy.coverage_limit", "policy.deductible",
    "policy.exclusion", "policy.endorsement",
}


def analyze_submission(conn, submission_id: str, organization_id: str) -> dict:
    """
    Run Property Policy Intelligence over every document in one submission.

    Creates (or reuses) a Business entity and an Asset entity ("the
    property") and a Policy entity, all named from the submission's label,
    linked Business-[owns]->Asset and Business-[has_policy]->Policy. Every
    fact extracted from every document in the submission is written through
    knowledge.service.reconcile_fact(), which links kc_evidence back to the
    real document/page/snippet and flags BOTH sides of any cross-document
    contradiction rather than picking one silently.

    Raises ValueError if the submission does not exist for this
    organization — caller (api/knowledge.py) turns that into a 404, same
    convention as every other org-scoped lookup in this codebase.
    """
    submission = db.get_submission(conn, submission_id, organization_id)
    if not submission:
        raise ValueError("Submission not found.")

    documents = db.list_submission_documents(conn, submission_id, organization_id)

    business = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="business",
        name=submission["label"],
    )
    asset = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="asset",
        name=f"{submission['label']} — insured property",
    )
    policy = kc.get_or_create_entity(
        conn, organization_id=organization_id, entity_type="policy",
        name=f"{submission['label']} — policy",
    )

    kc.create_relationship(
        conn, organization_id=organization_id, from_entity_id=str(business["id"]),
        to_entity_id=str(asset["id"]), relationship_type="owns",
    )
    kc.create_relationship(
        conn, organization_id=organization_id, from_entity_id=str(business["id"]),
        to_entity_id=str(policy["id"]), relationship_type="has_policy",
    )

    entity_by_kind = {"asset": asset, "policy": policy}

    facts_written = []
    contradictions = []
    documents_processed = []

    for document in documents:
        document_id = str(document["id"])
        text = document.get("extracted_text") or ""
        if not text.strip():
            documents_processed.append({
                "document_id": document_id, "filename": document["filename"],
                "facts_found": 0, "note": "No extracted text on this document.",
            })
            continue

        quality = db.get_document_quality(conn, document_id, organization_id)
        ocr_confidence = quality.get("avg_ocr_confidence") if quality else None

        extracted = extract_property_policy_facts(text=text, ocr_confidence=ocr_confidence)
        documents_processed.append({
            "document_id": document_id, "filename": document["filename"],
            "facts_found": len(extracted),
        })

        for item in extracted:
            entity = entity_by_kind[_target_entity_kind(item["fact_type"])]
            fact = kc.reconcile_fact(
                conn,
                organization_id=organization_id,
                entity_id=str(entity["id"]),
                fact_type=item["fact_type"],
                value=item["value"],
                normalized_value=item["normalized_value"],
                confidence=item["confidence"],
                produced_by=item["produced_by"],
                document_id=document_id,
                evidence_page=item["evidence"]["page"],
                evidence_section=item["evidence"]["section"],
                evidence_snippet=item["evidence"]["snippet"],
                evidence_source=item["evidence"]["source"],
            )
            facts_written.append(fact)
            if fact.get("_contradiction"):
                contradictions.append({
                    "fact_id": str(fact["id"]),
                    "fact_type": fact["fact_type"],
                    "value": fact["value"],
                    "document_id": document_id,
                    "document_filename": document["filename"],
                    **fact["_contradiction"],
                })

    return {
        "submission_id": submission_id,
        "business": business,
        "asset": asset,
        "policy": policy,
        "documents_processed": documents_processed,
        "facts": facts_written,
        "contradictions": contradictions,
    }
