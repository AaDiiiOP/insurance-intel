"""
Learning from what people correct.

Nothing here trains a model. The corrections a broker makes ARE the signal,
and they are useful in two different ways:

  * As NUMBERS, on the Review page. How often an attached document turned out
    to belong to someone else; which carriers produced a briefing with no
    requirements in it; which requirements keep getting overridden. These are
    plain counts over real rows, so a figure that looks wrong can be traced
    back to the rows that made it.

  * As GUIDANCE, fed into future drafts. If a broker rewrites every
    submission the same way, the next one should start closer to what they
    would have written.

The guidance is deliberately kept small, visible and editable. A learned rule
that nobody can see or change is worse than no rule: it quietly shapes every
document from then on, and when it is wrong the only symptom is drafts that
are subtly off for reasons nobody can find.

Two safeguards on what may become a rule:

  * A pattern must appear at least twice. One rewrite is a one-off.
  * Nothing that would make a draft less complete or less honest. A broker
    who trims a disclosure on one awkward case must not turn that into a
    standing instruction to trim disclosures — that is exactly the kind of
    rule that is invisible later and costly at claim time.
"""
import difflib
import logging
import re

from llm import LLMError, generate_json, load_prompt

logger = logging.getLogger(__name__)

# Edits fed to the distiller. Enough to see a pattern, few enough that a 7B
# model still reads them all carefully.
MAX_EDITS = 12
MAX_EDIT_CHARS = 1400

# Words that suggest a rule would suppress something a document needs. A
# model is told not to produce these, and the result is checked again here —
# a guardrail stated only in a prompt is one an off-day model can walk past.
UNSAFE_GUIDANCE = re.compile(
    r"\b(omit|remove|leave out|drop|don'?t mention|do not mention|avoid mentioning|"
    r"exclude|hide|downplay|soften|understate|skip)\b.{0,60}\b"
    r"(disclos|incident|claim|fire|risk|outstanding|exclusion|loss|hazard|"
    r"basement|welding|warning|gap|missing)",
    re.I,
)


def change_ratio(before: str, after: str) -> float:
    """
    How much of a draft was rewritten, 0 to 100.

    A similarity ratio rather than a word count, so reordering a paragraph
    registers as a change but reflowing whitespace does not.
    """
    before, after = (before or "").strip(), (after or "").strip()
    if not before and not after:
        return 0.0
    if not before or not after:
        return 100.0
    similarity = difflib.SequenceMatcher(None, before, after).ratio()
    return round((1 - similarity) * 100, 1)


def summarise_edit(before: str, after: str) -> dict:
    """Stats stored alongside a draft edit, for the Review page."""
    return {
        "changed_pct": change_ratio(before, after),
        "words_before": len((before or "").split()),
        "words_after": len((after or "").split()),
    }


def _trim(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:MAX_EDIT_CHARS]


def _is_unsafe(line: str) -> bool:
    return bool(UNSAFE_GUIDANCE.search(line))


def distil(edits: list[dict]) -> dict:
    """
    Turn a set of draft edits into guidance for future drafts.

    `edits` is a list of {before, after} — what was drafted, and what the
    broker changed it to.
    """
    usable = [
        e for e in edits
        if _trim(e.get("before_text")) and _trim(e.get("after_text"))
        # A rewrite of under 3% is a typo fix and carries no style signal.
        and (e.get("meta") or {}).get("changed_pct", 100) >= 3
    ][:MAX_EDITS]

    if len(usable) < 2:
        return {
            "guidance": "",
            "observations": [],
            "derived_from": len(usable),
            "note": (
                "Not enough substantive edits yet. At least two are needed "
                "before a pattern can be told apart from a one-off."
            ),
        }

    blocks = []
    for position, edit in enumerate(usable, start=1):
        blocks.append(
            f"--- EDIT {position} ---\n"
            f"DRAFTED:\n{_trim(edit['before_text'])}\n\n"
            f"CHANGED TO:\n{_trim(edit['after_text'])}"
        )

    data = generate_json(load_prompt("house_style", edits="\n\n".join(blocks)))

    lines = []
    dropped = []
    raw = data.get("guidance")
    if isinstance(raw, list):
        raw = "\n".join(str(r) for r in raw)
    for line in str(raw or "").splitlines():
        line = re.sub(r"^\s*[-•*\d.)\s]+", "", line).strip()
        if not line:
            continue
        # The safety check the prompt also asks for, enforced rather than
        # trusted. A rule that suppresses a disclosure must never be stored:
        # it would apply silently to every document afterwards.
        if _is_unsafe(line):
            dropped.append(line)
            logger.warning("Dropped unsafe learned guidance: %s", line[:90])
            continue
        lines.append(line)

    observations = []
    for item in data.get("observations") or []:
        text = _trim(str(item))[:240]
        if text:
            observations.append(text)

    if dropped:
        observations.append(
            f"{len(dropped)} suggested rule(s) were discarded because they would "
            "have made future drafts less complete or less candid."
        )

    return {
        "guidance": "\n".join(lines[:6]),
        "observations": observations[:6],
        "derived_from": len(usable),
        "note": "",
    }
