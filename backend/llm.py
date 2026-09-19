"""
Local LLM access via Ollama.

Three jobs beyond making the HTTP call:

1. Getting JSON back reliably. Models wrap JSON in prose or markdown fences
   even when told not to, so the response is repaired before parsing and, if
   that still fails, the call is retried once with a stricter instruction.

2. Keeping prompts out of the code. They live in prompts/*.txt so they can be
   iterated and diffed without touching logic.

3. Staying swappable. Every model call in this project goes through this
   module, and the model/host are read from environment variables. Moving to
   a hosted API later (Claude, OpenAI) means a sibling module with the same
   three public functions — generate, generate_json, embed — and a config
   flag, not a rewrite of every caller.

Right now nothing leaves the machine: OLLAMA_HOST points at a local instance.
That is deliberate for Phase 1, where the documents are carriers' own policy
wordings. It matters more from Phase 2 on, when real customer data enters the
system.
"""
import json
import logging
import os
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# Separate, much smaller model used only to turn text into vectors. Not used
# in Phase 1 — it is here because /health checks for it, so a missing
# embedding model is visible now rather than discovered in Phase 2.
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Summarising a chunk on modest hardware is slow, and there is no reliable
# ceiling on how slow — it depends on the machine, the model, and the section
# length. httpx.Timeout(None) disables the timeout entirely for chat calls, so
# a run is never killed mid-way through work that has already been paid for
# in time. Embedding and the health check keep a real timeout below; those
# are short calls that should never legitimately hang, and a stuck one should
# fail fast rather than block the request that is checking whether Ollama is
# alive at all.
CHAT_TIMEOUT = httpx.Timeout(None)
EMBED_TIMEOUT = float(os.getenv("OLLAMA_EMBED_TIMEOUT", "120"))

# Extraction wants repeatable output rather than variety.
TEMPERATURE = 0.1

PROMPT_DIR = Path(__file__).parent / "prompts"

# Appended on the retry when the first response wasn't valid JSON.
STRICTER_SUFFIX = (
    "\n\nYour previous response could not be parsed as JSON. "
    "Return ONLY a single valid JSON object. "
    "Start your response with { and end it with }. "
    "Do not write any text before or after the JSON. "
    "Do not use markdown code fences."
)


class LLMError(Exception):
    """Raised when the model is unreachable or won't return usable JSON."""


def load_prompt(name: str, **fields) -> str:
    """
    Read a prompt template and fill in its placeholders.

    Uses str.replace rather than str.format because the templates contain
    literal JSON braces as examples, which format() would try to interpret.
    """
    path = PROMPT_DIR / f"{name}.txt"
    if not path.exists():
        raise LLMError(f"Prompt template not found: {path}")

    template = path.read_text(encoding="utf-8")
    for key, value in fields.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def _extract_json(raw: str) -> dict:
    """
    Pull a JSON object out of a model response.

    Handles the common failure modes: markdown fences, a sentence of preamble
    before the object, and trailing commentary after it.
    """
    text = raw.strip()

    # ```json ... ``` or ``` ... ```
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Trailing commas are the most common remaining problem.
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Model did not return valid JSON: {exc}") from exc

    raise LLMError("Model response contained no JSON object.")


def generate(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
) -> str:
    """Send one prompt to Ollama and return the raw text response."""
    options = {"temperature": TEMPERATURE if temperature is None else temperature}
    if seed is not None:
        options["seed"] = seed

    payload = {
        "model": model or OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }

    try:
        response = httpx.post(
            f"{OLLAMA_HOST}/api/generate", json=payload, timeout=CHAT_TIMEOUT
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Ollama at {OLLAMA_HOST}: {exc}") from exc

    return response.json().get("response", "")


def generate_json(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
) -> dict:
    """
    Ask for JSON, retrying once with a stricter instruction if parsing fails.

    One retry rather than a loop: if a model ignores an explicit instruction
    twice, more attempts tend to produce the same output and each one is slow.
    """
    raw = generate(prompt, model=model, temperature=temperature, seed=seed)
    try:
        return _extract_json(raw)
    except LLMError as first_error:
        logger.warning("First JSON parse failed (%s) — retrying stricter", first_error)

    raw = generate(
        prompt + STRICTER_SUFFIX, model=model, temperature=temperature, seed=seed
    )
    try:
        return _extract_json(raw)
    except LLMError as exc:
        logger.error("Retry also failed. Raw response: %s", raw[:400])
        raise LLMError(
            "The model did not return valid JSON after two attempts. "
            "This usually means the model is too small for the task or the "
            "document section is unusually long."
        ) from exc


def embed(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """
    Turn text into vectors. Unused in Phase 1; Phase 2's retrieval needs it.

    Ollama's embed endpoint accepts a list, so a whole document's chunks go in
    one request rather than one call per chunk.
    """
    if not texts:
        return []

    payload = {"model": model or EMBED_MODEL, "input": texts}

    try:
        response = httpx.post(
            f"{OLLAMA_HOST}/api/embed", json=payload, timeout=EMBED_TIMEOUT
        )
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise LLMError(f"Embedding timed out after {EMBED_TIMEOUT:.0f}s.") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Ollama at {OLLAMA_HOST}: {exc}") from exc

    vectors = response.json().get("embeddings")
    if not vectors or len(vectors) != len(texts):
        raise LLMError(
            f"Expected {len(texts)} embeddings, got {len(vectors or [])}. "
            f"Check that '{model or EMBED_MODEL}' is pulled in Ollama."
        )
    return vectors


def _pulled_models() -> list[str]:
    """Names of models Ollama has locally. Empty list if unreachable."""
    try:
        response = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
        response.raise_for_status()
        return [m["name"] for m in response.json().get("models", [])]
    except Exception:
        return []


def embedding_available() -> bool:
    """Whether the embedding model specifically is pulled."""
    # Ollama reports names with a tag, e.g. "nomic-embed-text:latest".
    return any(n.split(":")[0] == EMBED_MODEL.split(":")[0] for n in _pulled_models())


def is_available() -> bool:
    """Whether Ollama is reachable and has the configured chat model pulled."""
    names = _pulled_models()
    return any(
        n == OLLAMA_MODEL or n.split(":")[0] == OLLAMA_MODEL.split(":")[0]
        for n in names
    )
