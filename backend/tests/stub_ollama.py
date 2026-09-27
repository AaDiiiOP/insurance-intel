"""
A minimal Ollama-compatible stub server, used ONLY because a real Ollama
instance is not reachable in this sandbox (confirmed: curl to
localhost:11434 and host.docker.internal:11434 both fail to connect).

Implements exactly the two endpoints llm.py calls (`/api/tags`,
`/api/generate`) with deterministic, regex-based extraction over the
document text embedded in the prompt -- NOT a language model, but shaped
exactly like what generate_json()/is_available() expect, so the real
extraction code path (backend/property_policy_intelligence.py, llm.py,
knowledge/service.py) can be exercised end-to-end against real HTTP + real
Postgres without inventing a fake extraction module of our own.

This is the same stub approach used in the prior Phase 1A hardening pass
(see Results.md's "Phase 1A hardening" section) -- ported here, as a
regular importable module with start()/stop() helpers, so the test suite
can spin it up as a pytest fixture rather than as a standalone `__main__`
script.
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = "qwen2.5:7b"
EMBED_NAME = "nomic-embed-text"


def _extract_document_text(prompt: str) -> str:
    marker = "DOCUMENT TEXT:"
    idx = prompt.find(marker)
    return prompt[idx + len(marker):] if idx != -1 else prompt


def _find_page(text: str, pos: int) -> int | None:
    page = None
    for m in re.finditer(r"\[Page (\d+)\]", text[:pos]):
        page = int(m.group(1))
    return page


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    end = end if end != -1 else len(text)
    return text[start:end].strip()


def _coverage_limit_response(text: str) -> dict:
    """Stand-in for the extract_coverage_limit.txt prompt."""
    m = re.search(
        r"(?:property\s+damage\s+sum\s+insured|total\s+property\s+sum\s+insured"
        r"|sum\s+insured)[^\d]{0,20}(n[\d,]{4,})",
        text, re.IGNORECASE,
    )
    if not m:
        return {"found": False, "reasoning": "No coverage limit figure found by stub."}
    value = m.group(1)
    pos = m.start(1)
    return {
        "found": True,
        "value": value,
        "page": _find_page(text, pos),
        "section": "Coverage",
        "snippet": _line_at(text, pos)[:300],
        "self_confidence": 0.9,
        "reasoning": "Stub: labelled sum-insured figure matched directly.",
    }


_FACT_PATTERNS = [
    ("property.sum_insured", r"(?:property\s+damage\s+sum\s+insured|total\s+property\s+sum\s+insured)[^\d]{0,20}(n[\d,]{4,})"),
    ("policy.deductible", r"(?:standard\s+property\s+deductible|deductible)[^\d]{0,20}(n[\d,]{4,})"),
    ("property.location", r"(?:^|\n)Location:\s*([^\n]+)"),
    ("property.occupancy", r"(The insured operates [^.]+\.)"),
    ("property.risk_factors", r"Overall risk rating:\s*(\w+)"),
    ("policy.endorsement", r"Change\n([^\n]+(?:\n[^\n]+)?)"),
]


def _property_facts_response(text: str) -> dict:
    """Stand-in for the extract_property_facts.txt prompt."""
    facts = []
    for fact_type, pattern in _FACT_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            value = m.group(1).strip()
            if not value:
                continue
            pos = m.start(1)
            facts.append({
                "fact_type": fact_type,
                "value": value[:300],
                "page": _find_page(text, pos),
                "section": None,
                "snippet": _line_at(text, pos)[:300] or value[:300],
                "self_confidence": 0.88,
                "reasoning": f"Stub: matched pattern for {fact_type}.",
            })
    return {"facts": facts}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep test output quiet

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_json({"models": [{"name": MODEL_NAME}, {"name": EMBED_NAME}]})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        if self.path == "/api/generate":
            prompt = payload.get("prompt", "")
            text = _extract_document_text(prompt)
            if "Valid values for \"fact_type\"" in prompt:
                result = _property_facts_response(text)
            else:
                result = _coverage_limit_response(text)
            self._send_json({"response": json.dumps(result)})
        elif self.path == "/api/embed":
            texts = payload.get("input", [])
            self._send_json({"embeddings": [[0.0] * 8 for _ in texts]})
        else:
            self._send_json({"error": "not found"}, 404)


class StubOllamaServer:
    """start()/stop() wrapper so pytest fixtures can run the stub server
    in a background thread for the duration of one test (or the session),
    rather than as a separate standalone process."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11434):
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
