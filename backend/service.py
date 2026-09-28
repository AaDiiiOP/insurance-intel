"""
Compatibility shim — the real Knowledge Core public interface lives in
knowledge/service.py (see that module's docstring for why it, not
knowledge/facts.py or knowledge/entities.py directly, is the intended
single entry point for everything outside the `knowledge` package). This
file used to be a byte-for-byte duplicate of it — two copies of the same
orchestration logic (reconcile_fact() and friends), silently able to
drift if one were edited and the other forgotten.

Nothing in this codebase imports this top-level module directly (checked:
no `from service import ...` / `import service` anywhere outside
knowledge/service.py itself), so this now exists only so an external
caller or an old import path does not break outright, rather than
maintaining a second copy.

Prefer `from knowledge.service import ...` (or `import knowledge.service
as kc`, the convention used throughout api/ and property_policy_
intelligence.py) in new code.
"""
from knowledge.service import *  # noqa: F401,F403
from knowledge.service import (  # noqa: F401
    ENTITY_TYPES,
    correct_fact,
    create_entity,
    create_fact,
    create_relationship,
    find_facts,
    find_relationships,
    get_current_value,
    get_current_value_or_conflict,
    get_entity,
    get_evidence_for_fact,
    get_fact,
    get_historical_values,
    get_or_create_entity,
    link_evidence,
    list_entities,
    reconcile_fact,
    update_entity,
    update_fact,
)
