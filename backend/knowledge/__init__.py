"""
Knowledge Core (Phase 1) — entities, facts, evidence, relationships, and the
versioning built on top of them.

Everything outside this package should import `knowledge.service`, not the
individual entities/facts/evidence/relationships/versions modules directly —
service.py is the single public interface, the same way callers of the
Document Intelligence slice go through doc_intelligence.py rather than
reaching into db.py themselves. The submodules exist to keep each concern
(entities, facts, evidence, relationships, versioning) readable on its own;
service.py composes them.
"""
