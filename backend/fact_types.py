"""
Fact-type registry — the single source of truth for what each
property.*/policy.* fact type IS, so that answer isn't re-derived
independently in extraction, reconciliation, the API, the frontend, and
(soon) the real-model evaluation harness.

Before this module existed, the same handful of facts about each fact type
were encoded three separate times:
  - facts.py:        SINGLETON_FACT_TYPES / MULTI_VALUED_FACT_TYPES
                      (does contradiction detection apply?)
  - property_policy_intelligence.py: NUMERIC_FACT_TYPES, _target_entity_kind(),
                      _ALL_FACT_TYPES (is it a number?, which entity does it
                      attach to?, what's the full allowed set?)
That worked while there were 10 fact types and one vertical slice, but it's
exactly the kind of duplication that silently drifts: add an 11th fact type
to one list and forget the other two, and you get a fact type that
extracts fine but never gets contradiction-checked, or gets checked against
the wrong entity. Centralizing it here means every one of those call sites
— including the upcoming real-Qwen benchmark's ground-truth schema — reads
the same definitions instead of each maintaining its own copy.

Adding a new fact type is now one line in FACT_DEFINITIONS below, not an
edit to three-plus files.
"""
from dataclasses import dataclass
from typing import Literal

Cardinality = Literal["singleton", "multi"]
ValueType = Literal["money", "text", "location"]
EntityKind = Literal["asset", "policy"]


@dataclass(frozen=True)
class FactTypeDefinition:
    fact_type: str
    cardinality: Cardinality
    value_type: ValueType
    entity: EntityKind

    @property
    def is_singleton(self) -> bool:
        return self.cardinality == "singleton"

    @property
    def is_numeric(self) -> bool:
        return self.value_type == "money"


# The full registry. `entity` says which Knowledge Core entity a fact of
# this type attaches to (see property_policy_intelligence.py's module
# docstring for why "asset" rather than a dedicated "Property" entity type).
# `cardinality` says whether reconcile_fact() should compare a new
# extraction against the current value for that entity+fact_type
# ("singleton" — one true current value) or treat it as an independent,
# simultaneously-true addition ("multi" — never contradiction-checked).
# `value_type` says how the raw string value should be parsed for
# normalized_value ("money" -> numeric comparison; "text"/"location" ->
# compared as text — see knowledge/service.py's _values_agree).
FACT_DEFINITIONS: dict[str, FactTypeDefinition] = {
    d.fact_type: d for d in [
        FactTypeDefinition("property.sum_insured", "singleton", "money", "asset"),
        FactTypeDefinition("property.location", "singleton", "location", "asset"),
        FactTypeDefinition("property.occupancy", "singleton", "text", "asset"),
        FactTypeDefinition("property.construction", "singleton", "text", "asset"),
        FactTypeDefinition("property.risk_factors", "multi", "text", "asset"),
        FactTypeDefinition("policy.coverage", "multi", "text", "policy"),
        FactTypeDefinition("policy.coverage_limit", "singleton", "money", "policy"),
        FactTypeDefinition("policy.deductible", "singleton", "money", "policy"),
        FactTypeDefinition("policy.exclusion", "multi", "text", "policy"),
        FactTypeDefinition("policy.endorsement", "multi", "text", "policy"),
    ]
}

ALL_FACT_TYPES: frozenset[str] = frozenset(FACT_DEFINITIONS)

SINGLETON_FACT_TYPES: frozenset[str] = frozenset(
    ft for ft, d in FACT_DEFINITIONS.items() if d.is_singleton
)
MULTI_VALUED_FACT_TYPES: frozenset[str] = frozenset(
    ft for ft, d in FACT_DEFINITIONS.items() if not d.is_singleton
)
NUMERIC_FACT_TYPES: frozenset[str] = frozenset(
    ft for ft, d in FACT_DEFINITIONS.items() if d.is_numeric
)


def is_singleton_fact_type(fact_type: str) -> bool:
    """
    Whether fact_type has one true current value at a time.

    Defaults to True (singleton) for an unregistered fact type — the safer
    default is to still run contradiction detection on something new rather
    than silently skip it, matching the existing "never silently agree
    without checking" rule the Knowledge Core follows elsewhere.
    """
    definition = FACT_DEFINITIONS.get(fact_type)
    if definition is None:
        return True
    return definition.is_singleton


def is_numeric_fact_type(fact_type: str) -> bool:
    """Whether this fact type's value should be parsed into normalized_value
    as a number. Defaults to False (treat as text) for an unregistered fact
    type, since assuming a number exists where none was declared risks
    silently misparsing free text as 0 or None."""
    definition = FACT_DEFINITIONS.get(fact_type)
    if definition is None:
        return False
    return definition.is_numeric


def target_entity_kind(fact_type: str) -> EntityKind:
    """Which Knowledge Core entity a fact of this type attaches to.

    Raises ValueError for an unregistered fact type — unlike the cardinality
    and numeric checks above, there is no safe default here: writing a fact
    to the wrong entity (or failing to pick one) is worse than refusing to
    proceed, so an unknown fact_type must be added to FACT_DEFINITIONS
    before it can be persisted."""
    definition = FACT_DEFINITIONS.get(fact_type)
    if definition is None:
        raise ValueError(f"Unrecognised fact_type: {fact_type!r}. Add it to FACT_DEFINITIONS in knowledge/fact_types.py.")
    return definition.entity
