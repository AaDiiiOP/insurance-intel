"""
Drafting the paperwork a broker sends to an underwriter.

Everything before this phase read documents. This one writes one — a
submission letter setting out the risk, built from what is already on record
rather than from a broker retyping it.

The flow, and the order matters:

    dossier (redacted)  ->  model writes  ->  real values restored  ->  DRAFT
                                                                        |
                                                          a human reviews it

The model never sees the client's name, PAN, GSTIN or contact details. It
writes about {{BUSINESS_1}} and {{PAN_1}}, and the backend substitutes the
real values back afterwards. So a finished letter can say "Sharma Hardware &
Fittings Pvt Ltd" without the model ever having been told that name.

Nothing here sends anything, and there is no code path that could. A licensed
human is accountable for what reaches an insurer, so the output is a draft,
the status says so, and approval is a separate human act.

Facts come from the record — verified attachments, the matched plan, the
client's own intake — and the prompt forbids inventing any that are not
there. A submission with an invented turnover figure is worse than one with a
gap in it, because the gap is visible.
"""
import logging
import re

from llm import LLMError, generate_json, load_prompt
from tokenise import Redactor, find_unrestored

logger = logging.getLogger(__name__)

MAX_DOSSIER_CHARS = 9000


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_dossier(
    *,
    client: dict,
    redactor: Redactor,
    match: dict | None,
    carrier_name: str | None,
    attachments: list[dict],
    gap_items: list[dict],
) -> str:
    """
    Everything known about this client, redacted, as the model's source.

    Assembled rather than handed the raw row so that what the model may use
    is explicit: if a fact is not in here, it cannot end up in the letter.

    Identifiers go in as placeholders on purpose. The letter needs to REFER
    to the PAN — an underwriter expects to see one — so the placeholder
    survives into the draft and is replaced with the real value afterwards.
    """
    lines = [f"Business: {redactor.register('BUSINESS', client.get('business_name') or 'the applicant')}"]

    for label, field in (
        ("Entity type", "entity_type"),
        ("Industry", "industry"),
        ("Annual turnover", "turnover"),
        ("Years trading", "years_trading"),
        ("Employees", "employee_count"),
        ("Sum insured sought", "sum_insured"),
    ):
        value = _text(client.get(field))
        if value:
            lines.append(f"{label}: {value}")

    location = ", ".join(x for x in (_text(client.get("city")), _text(client.get("state"))) if x)
    if location:
        lines.append(f"Location: {location}")

    for label, field, kind in (
        ("PAN", "pan", "PAN"),
        ("GSTIN", "gstin", "GSTIN"),
    ):
        value = _text(client.get(field))
        if value:
            lines.append(f"{label}: {redactor.register(kind, value)}")

    categories = client.get("categories") or []
    if categories:
        lines.append(f"Cover requested: {', '.join(categories)}")

    notes = _text(client.get("risk_notes"))
    if notes:
        lines.append("")
        lines.append("RISK DETAIL FROM THE BROKER'S NOTES:")
        lines.append(redactor.redact(notes)[:2500])

    if carrier_name and match:
        for result in match.get("results") or match.get("matches") or []:
            if _text(result.get("carrier")).casefold() != carrier_name.casefold():
                continue
            lines.append("")
            lines.append("THE COVER BEING SOUGHT FROM THIS CARRIER:")
            lines.append(f"- Plan: {_text(result.get('plan'))}")
            if _text(result.get("category")):
                lines.append(f"- Class: {_text(result.get('category'))}")
            if _text(result.get("why")):
                lines.append(f"- Why it suits: {_text(result.get('why'))}")
            for watch in result.get("watch_outs") or []:
                lines.append(f"- Underwriter will want to note: {_text(watch)}")
            break

    verified = [a for a in attachments if a.get("verdict") == "verified"]
    if verified:
        lines.append("")
        lines.append("DOCUMENTS HELD AND VERIFIED (enclosed with this submission):")
        for attachment in verified:
            detail = _text(attachment.get("doc_type")) or _text(attachment.get("filename"))
            facts = attachment.get("key_facts") or []
            extra = "; ".join(
                f"{_text(f.get('label'))}: {_text(f.get('value'))}" for f in facts[:3]
            )
            lines.append(f"- {detail}{f' ({extra})' if extra else ''}")

    outstanding = [i for i in gap_items if i.get("status") == "missing"]
    if outstanding:
        lines.append("")
        lines.append("STILL OUTSTANDING (not yet held):")
        for item in outstanding[:10]:
            lines.append(f"- {_text(item.get('requirement'))}")

    return "\n".join(lines)[:MAX_DOSSIER_CHARS]


# The one bracket placeholder the prompt asks for, because the broker signs
# the letter themselves. Excluded from the check below: a warning that fires
# on every single draft is one people stop reading.
SIGNATURE_PLACEHOLDER = re.compile(r"\[broker name\]", re.I)


def _warnings_for(body: str) -> list[dict]:
    """
    Checks run on the finished text, before a human is asked to review it.

    A stray placeholder is the failure mode that matters most here: a letter
    reading "PAN {{PAN_1}}" is obviously broken and gets fixed, but it would
    be embarrassing to send, and it means the restoration step missed
    something.
    """
    warnings = []

    stray = find_unrestored(body)
    if stray:
        warnings.append(
            {
                "level": "error",
                "text": (
                    f"{len(stray)} placeholder(s) were not replaced with real "
                    f"values ({', '.join(stray[:4])}). Fix these before sending."
                ),
            }
        )

    # A model asked to write from a dossier sometimes fills a gap with a
    # plausible-looking blank instead of leaving it out. Checked against the
    # body with the intended signature line removed.
    without_signature = SIGNATURE_PLACEHOLDER.sub("", body)
    for pattern, text in (
        (r"\[[A-Za-z /]{2,30}\]", "square-bracket placeholders like [insert date]"),
        (r"\bXX+\b|\bTBD\b|\bTBC\b", "TBD / XX markers"),
    ):
        if re.search(pattern, without_signature):
            warnings.append(
                {
                    "level": "warning",
                    "text": f"The draft contains {text} — fill these in before sending.",
                }
            )

    return warnings


def _style_block(house_style: str | None) -> str:
    """
    Learned guidance, as a prompt section.

    Placed as an ADDITION to the rules rather than a replacement, and
    explicitly subordinate to them: the rules forbid inventing facts and
    require disclosure, and no habit picked up from a broker's edits may
    override that. A style preference that contradicts a safety rule is a
    bug, not a preference.
    """
    guidance = (house_style or "").strip()
    if not guidance:
        return ""
    return (
        "\n\nHOUSE STYLE — how this team writes, learned from their own edits.\n"
        "Follow these ON TOP OF the rules above. Where one appears to conflict "
        "with a rule above, the rule wins — never drop a disclosure, an "
        "outstanding item or a risk detail to satisfy a style preference.\n"
        + guidance
    )


def draft_submission(
    *,
    client: dict,
    carrier_name: str | None,
    match: dict | None,
    attachments: list[dict],
    gap_items: list[dict],
    token_map: dict,
    house_style: str | None = None,
) -> dict:
    """
    Write a submission letter for one client to one carrier.

    `token_map` seeds the redactor so placeholders match the ones used
    everywhere else for this client, and so restoration has every value it
    needs.
    """
    redactor = Redactor(token_map)

    dossier = build_dossier(
        client=client,
        redactor=redactor,
        match=match,
        carrier_name=carrier_name,
        attachments=attachments,
        gap_items=gap_items,
    )

    data = generate_json(
        load_prompt(
            "draft_submission",
            carrier_name=carrier_name or "the underwriter",
            dossier=dossier,
        )
        + _style_block(house_style)
    )

    title = _text(data.get("subject")) or f"Submission — {carrier_name or 'underwriter'}"
    body_redacted = str(data.get("body") or "").strip()

    if not body_redacted:
        raise LLMError("The model returned an empty draft.")

    # Real values go back in only now, after the model has finished.
    body = redactor.restore(body_redacted)
    title = redactor.restore(title)

    return {
        "title": title[:200],
        "body": body,
        "body_redacted": body_redacted,
        "warnings": _warnings_for(body),
        "stats": {
            "words": len(body.split()),
            "documents_enclosed": len(
                [a for a in attachments if a.get("verdict") == "verified"]
            ),
            "outstanding": len([i for i in gap_items if i.get("status") == "missing"]),
        },
    }


# --------------------------------------------------------------------------
# Other document kinds
# --------------------------------------------------------------------------

# What each kind is called in the UI, and what it is for. Kept here so the
# API, the UI and the exporter all agree on one list.
KINDS = {
    "submission": "Submission to underwriter",
    "proposal": "Risk summary for underwriter",
    "chase": "Chase email to client",
    # Unlike the others, this one needs a blank form uploaded to fill in —
    # it answers somebody else's document rather than writing its own.
    "form": "Fill in a proposal form",
}


def draft_chase(*, client: dict, gap_items: list[dict], token_map: dict,
                house_style: str | None = None) -> dict:
    """
    Write the email asking a client for the paperwork still outstanding.

    The item list is assembled here, not by the model: which requirements are
    missing is already known exactly from the gap report, and letting a model
    decide would risk it dropping one to keep the email tidy. The model's job
    is only to turn a known list into something readable.
    """
    redactor = Redactor(token_map)

    outstanding = [
        item for item in gap_items
        if item.get("status") in {"missing", "unclear"}
    ]
    if not outstanding:
        raise LLMError(
            "Nothing is outstanding for this client, so there is nothing to "
            "chase. Run Check paperwork first if you have not."
        )

    lines = []
    for item in outstanding:
        requirement = _text(item.get("requirement"))
        if not requirement:
            continue
        # The ask written during gap analysis is more specific than the bare
        # requirement, and the reason often carries what the client already
        # told us. Both are passed through as notes.
        notes = []
        if _text(item.get("ask")):
            notes.append(_text(item.get("ask")))
        if _text(item.get("reason")):
            notes.append(_text(item.get("reason")))
        suffix = f" [{' | '.join(notes)}]" if notes else ""
        lines.append(f"- {requirement}{suffix}")

    context = [f"Client: {redactor.register('BUSINESS', client.get('business_name') or 'the client')}"]
    contact = _text(client.get("contact_name"))
    if contact:
        context.append(f"Contact: {redactor.register('PERSON', contact)}")
    categories = client.get("categories") or []
    if categories:
        context.append(f"Cover being arranged: {', '.join(categories)}")

    data = generate_json(
        load_prompt(
            "draft_chase",
            context="\n".join(context),
            items="\n".join(lines)[:4000],
        )
        + _style_block(house_style)
    )

    title = _text(data.get("subject")) or "Documents still needed"
    body_redacted = str(data.get("body") or "").strip()
    if not body_redacted:
        raise LLMError("The model returned an empty draft.")

    body = redactor.restore(body_redacted)

    return {
        "title": redactor.restore(title)[:200],
        "body": body,
        "body_redacted": body_redacted,
        "warnings": _warnings_for(body),
        "stats": {"words": len(body.split()), "items_requested": len(lines)},
    }


def draft_proposal(
    *,
    client: dict,
    carrier_name: str | None,
    match: dict | None,
    attachments: list[dict],
    gap_items: list[dict],
    token_map: dict,
    house_style: str | None = None,
) -> dict:
    """
    Write the structured risk summary that travels with a proposal.

    Same dossier as the submission letter — the difference is entirely in the
    shape of the output, so the facts available to each are identical and the
    two can never disagree about the risk.
    """
    redactor = Redactor(token_map)

    dossier = build_dossier(
        client=client,
        redactor=redactor,
        match=match,
        carrier_name=carrier_name,
        attachments=attachments,
        gap_items=gap_items,
    )

    data = generate_json(
        load_prompt("draft_proposal", dossier=dossier) + _style_block(house_style)
    )

    title = _text(data.get("subject")) or "Risk summary"
    body_redacted = str(data.get("body") or "").strip()
    if not body_redacted:
        raise LLMError("The model returned an empty draft.")

    body = redactor.restore(body_redacted)

    return {
        "title": redactor.restore(title)[:200],
        "body": body,
        "body_redacted": body_redacted,
        "warnings": _warnings_for(body),
        "stats": {
            "words": len(body.split()),
            "documents_enclosed": len(
                [a for a in attachments if a.get("verdict") == "verified"]
            ),
            "outstanding": len([i for i in gap_items if i.get("status") == "missing"]),
        },
    }


def fill_form(
    *,
    form_text: str,
    client: dict,
    carrier_name: str | None,
    match: dict | None,
    attachments: list[dict],
    gap_items: list[dict],
    token_map: dict,
) -> dict:
    """
    Fill in a blank proposal form from what is on file.

    Different from the other drafters in one important way: those WRITE a
    document, this one ANSWERS a document somebody else wrote. So the output
    is field-and-value pairs rather than prose, and every field carries where
    its answer came from.

    Fields the file cannot answer are returned EMPTY and listed separately,
    never guessed. A proposal form with an invented turnover is worse than a
    blank one — a blank is visibly incomplete, a wrong figure is not, and the
    person signing the declaration is the one who carries it.
    """
    if not (form_text or "").strip():
        raise LLMError("That form has no readable text in it.")

    redactor = Redactor(token_map)
    dossier = build_dossier(
        client=client,
        redactor=redactor,
        match=match,
        carrier_name=carrier_name,
        attachments=attachments,
        gap_items=gap_items,
    )

    data = generate_json(
        load_prompt(
            "fill_form",
            form_text=_text(form_text)[:9000],
            dossier=dossier,
        )
    )

    fields = []
    unanswered = []
    for item in data.get("fields") or []:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"))[:160]
        if not label:
            continue
        value = redactor.restore(_text(item.get("value"))[:400])
        source = _text(item.get("source")).lower()
        if source not in {"on file", "derived", "not available"}:
            source = "on file" if value else "not available"
        # A field with a source but no value is unanswered, whatever it says.
        if not value:
            source = "not available"
            unanswered.append(label)
        fields.append({"label": label, "value": value, "source": source})

    if not fields:
        raise LLMError(
            "No fields could be read from that form. Check it is a proposal "
            "form and that the text is selectable rather than a scan."
        )

    for label in data.get("unanswerable") or []:
        text = _text(label)[:160]
        if text and text not in unanswered:
            unanswered.append(text)

    title = redactor.restore(_text(data.get("title")) or "Proposal form")

    # Rendered as text so it can live in the same drafts table, be reviewed
    # and signed off the same way, and export through the same path.
    lines = []
    for field in fields:
        if field["value"]:
            suffix = "  (derived)" if field["source"] == "derived" else ""
            lines.append(f"{field['label']}: {field['value']}{suffix}")
        else:
            lines.append(f"{field['label']}: __________")

    body = "\n".join(lines)
    if unanswered:
        body += (
            "\n\nNOT ANSWERED FROM FILE\n"
            + "\n".join(unanswered)
            + "\n\nThese were left blank rather than guessed. Fill them in "
            "before the form is signed."
        )

    answered = len([f for f in fields if f["value"]])

    return {
        "title": title[:200],
        "body": body,
        "body_redacted": body,
        "warnings": (
            [{
                "level": "warning",
                "text": (
                    f"{len(unanswered)} field(s) could not be answered from the "
                    "file and are left blank."
                ),
            }]
            if unanswered else []
        ),
        "stats": {
            "fields": len(fields),
            "answered": answered,
            "unanswered": len(unanswered),
            "words": len(body.split()),
        },
        "fields": fields,
    }
