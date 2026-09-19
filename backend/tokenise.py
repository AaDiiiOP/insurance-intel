"""
Redaction of client identifiers before text reaches the model or the index.

Phase 1 handled carriers' own paperwork, which carries nothing personal.
Phase 2 is the first point where a real business's details enter the system,
so a boundary is drawn here: exact values live in Postgres and nowhere else,
and everything downstream — the embedding call, the vector store, every
prompt — sees placeholders instead.

    "Sharma Hardware, PAN AAFCS1234K, turnover 4.2 crore"
      -> "{{BUSINESS_1}}, PAN {{PAN_1}}, turnover {{AMOUNT_1}}"

The map from placeholder back to the real value is stored per client in
Postgres and is applied only on the way out, after the model has finished
writing. That way a drafted proposal can still say "Sharma Hardware" without
the model ever having been shown the name.

Two honest limits, worth knowing before relying on this:

  * This is pattern matching, not understanding. It catches the identifier
    formats below and the fields the intake form marks sensitive. A business
    name written in an unexpected way inside free text can survive it.
    Treat it as a strong default, not a guarantee.
  * "Never leaves the machine" is what makes this safe today, since Ollama
    is local. The redaction exists so that swapping in a hosted API later is
    a config change rather than a privacy incident, but that swap should be
    reviewed on its own terms, not assumed safe because this module exists.

Nothing here is encrypted at rest. The mapping table sits in the same local
Postgres as everything else; protecting that database is a deployment
concern, and claiming otherwise in code would be worse than saying so here.
"""
import re

# Indian identifier formats, matched conservatively — a pattern that fires on
# ordinary prose would redact the text into uselessness.
#
# Order matters: PAN is a substring-compatible shape with parts of a GSTIN,
# so GSTIN is tried first and its match consumes those characters.
PATTERNS: list[tuple[str, re.Pattern]] = [
    # 27AAFCS1234K1Z5 — state code, PAN, entity digit, Z, checksum
    ("GSTIN", re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")),
    # AAFCS1234K
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # 1234 5678 9012 — only the spaced form is unambiguous. An unspaced
    # 12-digit run is caught by ACCOUNT below instead: both are redacted
    # either way, and guessing between them would only mislabel the map.
    ("AADHAAR", re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    # +91 98765 43210 / 9876543210
    ("PHONE", re.compile(r"(?:\+91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b")),
    # IFSC, e.g. HDFC0001234
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    # Bank account numbers: 9-18 digits standing alone.
    ("ACCOUNT", re.compile(r"\b\d{9,18}\b")),
]

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)_(\d+)\}\}")


class Redactor:
    """
    Builds a placeholder map for one client, across many pieces of text.

    Stateful on purpose: the same business name or PAN appearing in the
    intake form and again in a free-text note must map to the SAME
    placeholder, or the model sees two unrelated entities where there is one
    and reasons badly about it.

    Seed it with an existing map (from Postgres) to keep placeholders stable
    across separate requests for the same client.
    """

    def __init__(self, mapping: dict[str, str] | None = None):
        # placeholder -> real value
        self.mapping: dict[str, str] = dict(mapping or {})
        # fingerprint -> placeholder, for reuse across texts
        self._seen: dict[str, str] = {}
        # placeholder -> literal text, for the substring pass in redact()
        self._literals: dict[str, str] = {}
        self._counters: dict[str, int] = {}

        for key, value in self.mapping.items():
            match = PLACEHOLDER_RE.fullmatch(key)
            kind = match.group(1) if match else "OTHER"
            if match:
                self._counters[kind] = max(
                    self._counters.get(kind, 0), int(match.group(2))
                )
            self._seen[self._fingerprint(kind, value)] = key
            self._literals[key] = value.strip()

    def _fingerprint(self, kind: str, value: str) -> str:
        """
        The key under which a value is remembered, so the same real-world
        thing written two ways gets ONE placeholder.

        Namespaced by kind, because two different identifier types can share
        digits — an account number and an Aadhaar number of the same twelve
        digits are not the same thing, and collapsing them would restore one
        of them to the other's formatting.

        Phone numbers are the one case normalised aggressively: a form field
        holding "+91 98765 43210" and a note saying "9876543210" are the same
        number, but compared literally they mint PHONE_1 and PHONE_2, and the
        model then reasons about two contacts where there is one. Comparing
        the last ten digits collapses every Indian format onto one key.

        Everything else is compared case-insensitively but otherwise exactly,
        so restoring always returns the text that was actually there.
        """
        raw = value.strip()
        if kind == "PHONE":
            digits = re.sub(r"\D", "", raw)
            return f"PHONE:{digits[-10:]}" if len(digits) >= 10 else f"PHONE:{digits}"
        return f"{kind}:{raw.casefold()}"

    def _placeholder_for(self, kind: str, value: str) -> str:
        """Reuse the placeholder for a value already seen, else mint one."""
        fingerprint = self._fingerprint(kind, value)
        if not fingerprint:
            return value
        if fingerprint in self._seen:
            return self._seen[fingerprint]

        self._counters[kind] = self._counters.get(kind, 0) + 1
        placeholder = f"{{{{{kind}_{self._counters[kind]}}}}}"
        self.mapping[placeholder] = value.strip()
        self._seen[fingerprint] = placeholder
        # Kept separately from the fingerprint index: redact() needs the
        # literal text to search for, which a fingerprint has discarded.
        self._literals[placeholder] = value.strip()
        return placeholder

    def register(self, kind: str, value: str) -> str:
        """
        Redact one known-sensitive field by name.

        Used for intake fields the form already knows are sensitive — the
        business name, the PAN — where there is no need to guess from shape.
        """
        if not value or not str(value).strip():
            return ""
        return self._placeholder_for(kind, str(value))

    def redact(self, text: str) -> str:
        """
        Replace every recognised identifier in free text with a placeholder.

        Values already registered are replaced by their existing placeholder,
        so a business name typed into the form and mentioned again in a note
        comes out as the same token both times.
        """
        if not text:
            return ""

        result = text

        # Shape patterns run FIRST, longest-form identifier before its
        # substrings (GSTIN before PAN). If registered literals ran first, a
        # registered PAN would be substituted out of the middle of a GSTIN
        # containing it — "07AAFCS1234K1Z5" becoming "07{{PAN_1}}1Z5", which
        # leaves the state code and checksum in the clear and is trivially
        # reversible. Matching the GSTIN whole avoids that entirely.
        for kind, pattern in PATTERNS:
            def swap(match: re.Match, _kind: str = kind) -> str:
                return self._placeholder_for(_kind, match.group(0))

            result = pattern.sub(swap, result)

        # Registered values second, for anything with no recognisable shape —
        # chiefly the business name. Longest first, so "Sharma Hardware Pvt
        # Ltd" wins over "Sharma". Values already replaced above are simply
        # absent from the text by now, so this is a no-op for them.
        for placeholder, literal in sorted(
            self._literals.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            if len(literal) < 4:
                continue  # too short to match safely inside prose
            result = re.sub(
                re.escape(literal), placeholder, result, flags=re.IGNORECASE
            )

        return result

    def restore(self, text: str) -> str:
        """
        Put the real values back, after the model has finished writing.

        Any placeholder with no entry in the map is left visible rather than
        blanked: a document that shows "{{PAN_1}}" is obviously wrong and gets
        fixed, whereas one silently missing the PAN could be sent as-is.
        """
        if not text:
            return ""
        result = text
        for placeholder, value in self.mapping.items():
            result = result.replace(placeholder, value)
        return result


def restore_with(mapping: dict[str, str], text: str) -> str:
    """Restore without building a Redactor — for one-off output rewriting."""
    return Redactor(mapping).restore(text)


def find_unrestored(text: str) -> list[str]:
    """
    Placeholders still present after a restore pass.

    Used to warn on a drafted document before a human signs it off, rather
    than letting a stray token reach an insurer.
    """
    return sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(text or "")})
