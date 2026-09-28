"""
Compatibility shim — the real Knowledge Core facts module lives in
knowledge/facts.py. This file used to be a byte-for-byte duplicate of it.
Nothing in this codebase imports this top-level module directly (checked:
no `from facts import ...` / `import facts` anywhere outside
knowledge/facts.py itself), so this now exists only so an external caller
or an old import path does not break outright, rather than maintaining a
second copy that can silently drift from the real one.

Prefer `from knowledge.facts import ...` (or, for nearly everything,
`from knowledge.service import ...` — see knowledge/service.py's module
docstring for why knowledge.service is the intended public interface, not
even knowledge.facts directly) in new code.
"""
from knowledge.facts import *  # noqa: F401,F403
from knowledge.facts import (  # noqa: F401
    VALID_CONFIDENCE_TYPES,
    VALID_STATUSES,
    create_fact,
    get_fact,
    is_singleton_fact_type,
    list_facts_for_entity,
    set_fact_status,
    supersede_fact,
)
