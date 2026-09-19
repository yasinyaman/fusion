"""Model adapters.

Only two methods matter to the harness — ``complete(prompt) -> str`` — so a
new provider is a dozen lines. Ollama is the one implemented here because a
local model is the deployment this whole project targets: no data leaves the
machine, which is the point of the on-premise story.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any


def extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply.

    Models wrap the answer in prose and fences as often as not, and refusing
    those replies would measure formatting compliance rather than whether the
    arm chose the right metrics. The first balanced ``{...}`` is taken, so a
    fenced block or an inline object both work.
    """
    stripped = text.strip()
    start = stripped.find("{")
    if start == -1:
        return stripped
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : index + 1]
    return stripped[start:]


class OllamaModel:
    """A model served by a local ollama.

    ``temperature=0`` by default: a benchmark that gives different answers on
    a re-run cannot be used to decide anything.
    """

    def __init__(
        self,
        model: str = "qwen2.5-coder:7b",
        host: str = "http://localhost:11434",
        temperature: float = 0.0,
        max_tokens: int = 220,
        timeout: float = 180.0,
        json_only: bool = False,
    ) -> None:
        self.model = model
        # Constrained decoding for arms that must answer with a structured
        # request. Arms that answer in SQL need nothing of the sort — SQL is
        # what the model was pretrained on — so this levels the formats rather
        # than favouring one. It is also how a real MCP client works: the tool
        # has a JSON schema and the client enforces it.
        self._json_only = json_only
        self._url = f"{host.rstrip('/')}/api/generate"
        self._options = {
            "temperature": temperature,
            "num_predict": max_tokens,
            # Deterministic sampling all the way down, not just temperature.
            "top_p": 1.0,
            "seed": 7,
        }
        self._timeout = timeout

    def complete(self, prompt: str, schema: Mapping[str, Any] | None = None) -> str:
        """One completion, optionally constrained to a JSON schema.

        A ``schema`` is enforced during decoding, so the reply cannot be
        malformed against it — the sampler is restricted to tokens that keep
        the output valid. That is a stronger guarantee than asking for JSON in
        the prompt, and it is the same mechanism an MCP client applies when a
        tool declares its input schema.

        Raises:
            RuntimeError: When the server answers with an error, so the arm
                records a model failure rather than an empty answer that would
                be graded as a wrong one.
        """
        request_body: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": self._options,
        }
        if schema is not None:
            request_body["format"] = schema
        elif self._json_only:
            request_body["format"] = "json"
        payload = json.dumps(request_body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - fixed localhost URL
            self._url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                reply = json.loads(response.read())
        except urllib.error.URLError as e:
            raise RuntimeError(f"ollama unreachable at {self._url}: {e}") from e
        if "error" in reply:
            raise RuntimeError(f"ollama error: {reply['error']}")
        return str(reply.get("response", ""))


class DuckDBExecutor:
    """Runs statements against a DuckDB connection."""

    def __init__(self, connection) -> None:
        self._connection = connection

    def run(self, sql: str) -> list[tuple]:
        """Execute and return rows."""
        return [tuple(row) for row in self._connection.execute(sql).fetchall()]


class OdbcExecutor:
    """Runs statements over ODBC (Oracle, SQL Server)."""

    def __init__(self, dsn: str) -> None:
        import pyodbc

        self._connection = pyodbc.connect(dsn, autocommit=True)

    def run(self, sql: str) -> list[tuple]:
        """Execute and return rows."""
        cursor = self._connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]


def ollama_remote() -> OllamaModel:
    """Ollama on the Docker host, for arms running inside a container."""
    return OllamaModel(host="http://host.docker.internal:11434")


def oracle_executor() -> OdbcExecutor:
    """The benchmark's Oracle, reachable inside the driver image."""
    return OdbcExecutor(
        "DRIVER={Oracle 23 ODBC driver};DBQ=127.0.0.1:1521/FREEPDB1;UID=warp;PWD=Warp_Integr4tion1;"
    )
