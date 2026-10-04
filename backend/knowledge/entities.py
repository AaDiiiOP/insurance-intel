"""
Knowledge Core entities — thin, organization-scoped wrappers around
repositories/knowledge.py's entity functions.

Nothing here talks SQL directly; that lives in repositories/knowledge.py,
same layering as doc_intelligence.py -> repositories/extraction.py.
"""
import re

import db

ENTITY_TYPES = {
    "business", "carrier", "policy", "document", "location", "asset",
    "coverage", "claim", "risk", "person",
}


def create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                   attributes: dict | None = None) -> dict:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"Unknown entity_type '{entity_type}'. Must be one of: {sorted(ENTITY_TYPES)}")
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Entity name cannot be empty.")
    return db.create_entity(
        conn, organization_id=organization_id, entity_type=entity_type,
        name=name, attributes=attributes,
    )


# --- Deterministic name normalization -------------------------------------
# Only strips things that never change identity: case, punctuation, "&"/"and",
# and common legal-form suffixes. It does NOT do fuzzy matching: names that are
# merely similar are surfaced for human review, never merged automatically.
_LEGAL_SUFFIXES = (
    "private limited", "pvt ltd", "pvt limited", "p ltd", "limited", "ltd",
    "llp", "llc", "inc", "incorporated", "corporation", "corp", "co",
    "company", "plc", "gmbh",
)
_SUFFIX_RE = re.compile(r"(?:\s+(?:" + "|".join(re.escape(x) for x in _LEGAL_SUFFIXES) + r"))+$")


def normalize_name(name: str) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^\w\s]", " ", n)
    n = " ".join(n.split())
    stripped = _SUFFIX_RE.sub("", n).strip()
    return stripped or n


def _aliases(entity: dict) -> list[str]:
    attrs = entity.get("attributes") or {}
    return list(attrs.get("aliases") or [])


def find_matching_entities(conn, *, organization_id: str, entity_type: str,
                            name: str) -> list[dict]:
    """Entities of this type whose name or recorded alias normalizes the same."""
    key = normalize_name(name)
    out = []
    for e in db.list_entities(conn, organization_id, entity_type):
        names = [e["name"], *_aliases(e)]
        if any(normalize_name(n) == key for n in names):
            out.append(e)
    return out


def find_possible_duplicates(conn, *, organization_id: str, entity_type: str | None = None,
                              threshold: float = 0.4) -> list[dict]:
    """
    Pairs of same-type entities where one normalized name is the start of the
    other ("Sunrise Textile Mills" / "Sunrise Textile Mills Unit 2") but they
    do not normalize equal. For human review only; nothing is merged.

    Word-overlap scoring was tried first and flagged every "X baseline — policy"
    / "X renewal — policy" pair in the comparison benchmark (different things
    that share most words), so the rule is deliberately narrow.
    `similarity` = shorter name's word count / longer name's.
    """
    ents = db.list_entities(conn, organization_id, entity_type)
    pairs = []
    for i, a in enumerate(ents):
        ta = normalize_name(a["name"]).split()
        for b in ents[i + 1:]:
            if a["entity_type"] != b["entity_type"]:
                continue
            tb = normalize_name(b["name"]).split()
            short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if not short or short == long_ or long_[:len(short)] != short:
                continue
            score = len(short) / len(long_)
            if score >= threshold:
                pairs.append({"a": a["id"], "a_name": a["name"], "b": b["id"],
                              "b_name": b["name"], "similarity": round(score, 2)})
    return pairs


def get_or_create_entity(conn, *, organization_id: str, entity_type: str, name: str,
                          attributes: dict | None = None) -> dict:
    """
    Find an existing entity of this type/name in the organization, or create
    one. An exact (case-insensitive) match wins. Otherwise a single entity
    whose normalized name or alias matches is reused and the new spelling is
    recorded as an alias. Two or more normalized matches are ambiguous, so a
    new entity is created rather than guessing.
    """
    existing = db.find_entity_by_name(conn, organization_id, entity_type, name)
    if existing:
        return existing
    clean = " ".join((name or "").split())
    matches = find_matching_entities(
        conn, organization_id=organization_id, entity_type=entity_type, name=clean) if clean else []
    if len(matches) == 1:
        m = matches[0]
        attrs = dict(m.get("attributes") or {})
        aliases = list(attrs.get("aliases") or [])
        if clean.lower() != m["name"].lower() and clean not in aliases:
            aliases.append(clean)
            attrs["aliases"] = aliases
            m = db.update_entity(conn, m["id"], organization_id, attributes=attrs) or m
        return m
    return create_entity(
        conn, organization_id=organization_id, entity_type=entity_type,
        name=name, attributes=attributes,
    )


def get_entity(conn, entity_id: str, organization_id: str) -> dict | None:
    return db.get_entity(conn, entity_id, organization_id)


def list_entities(conn, organization_id: str, entity_type: str | None = None) -> list[dict]:
    return db.list_entities(conn, organization_id, entity_type)


def update_entity(conn, entity_id: str, organization_id: str, *,
                   name: str | None = None, attributes: dict | None = None) -> dict | None:
    return db.update_entity(conn, entity_id, organization_id, name=name, attributes=attributes)
