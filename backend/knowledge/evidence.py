"""
Knowledge Core evidence — where a fact's value came from. Thin,
organization-scoped wrappers around repositories/knowledge.py.
"""
import db


def create_evidence(conn, *, organization_id: str, fact_id: str, document_id: str,
                     page: int | None = None, section: str | None = None,
                     snippet: str | None = None, source: str | None = None) -> dict:
    return db.create_evidence(
        conn, organization_id=organization_id, fact_id=fact_id, document_id=document_id,
        page=page, section=section, snippet=snippet, source=source,
    )


def get_evidence_for_fact(conn, fact_id: str, organization_id: str) -> list[dict]:
    return db.get_evidence_for_fact(conn, fact_id, organization_id)
