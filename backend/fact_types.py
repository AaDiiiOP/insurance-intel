"""
Compatibility shim — the real fact-type registry lives in
knowledge/fact_types.py. This file used to be a byte-for-byte duplicate of
it (two copies of the same implementation, drifting silently whenever one
was edited and the other forgotten). Nothing in this codebase imports this
top-level module directly (checked: no `from fact_types import ...` /
`import fact_types` anywhere outside knowledge/fact_types.py itself), so
this now exists only so an external caller or an old import path does not
break outright — it re-exports everything from the real module rather than
maintaining a second copy.

Prefer `from knowledge.fact_types import ...` in new code.
"""
from knowledge.fact_types import *  # noqa: F401,F403
from knowledge.fact_types import (  # noqa: F401
    ALL_FACT_TYPES,
    FACT_DEFINITIONS,
    MULTI_VALUED_FACT_TYPES,
    NUMERIC_FACT_TYPES,
    SINGLETON_FACT_TYPES,
    FactTypeDefinition,
    is_numeric_fact_type,
    is_singleton_fact_type,
    target_entity_kind,
)
