"""OpenRouter chat client.

Gemma returns JSON wrapped in markdown fences often enough that fence stripping
is part of the normal path, not an error path.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import config

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LLMError(RuntimeError):
    pass


_key_cycle = itertools.cycle(config.api_keys())
_key_lock = threading.Lock()


def _next_key() -> str:
    """Round-robin across every configured key; safe from many threads."""
    with _key_lock:
        return next(_key_cycle)


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        config.API_BASE,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_next_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=config.REQUEST_TIMEOUT_S) as response:
        return json.loads(response.read())


def complete(system: str, user: str) -> tuple[str, dict[str, int], int]:
    """Return (text, token usage, latency_ms). Retries on transport errors."""
    payload = {
        "model": config.MODEL_ID,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
    }

    last_error: Exception | None = None
    for attempt in range(config.MAX_RETRIES):
        started = time.time()
        try:
            data = _post(payload)
            latency_ms = int((time.time() - started) * 1000)
            usage = data.get("usage") or {}
            return (
                data["choices"][0]["message"]["content"],
                {
                    "prompt": int(usage.get("prompt_tokens", 0)),
                    "completion": int(usage.get("completion_tokens", 0)),
                },
                latency_ms,
            )
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 429 means this key is rate limited; the next attempt rotates to
            # another key, so back off briefly rather than giving up.
            time.sleep(4 * (attempt + 1) if exc.code == 429 else 2**attempt)
        except (urllib.error.URLError, KeyError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(2**attempt)

    raise LLMError(f"LLM call failed after {config.MAX_RETRIES} attempts: {last_error}")


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of an LLM reply, tolerating fences and prose."""
    stripped = _FENCE.sub("", text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"no JSON object in reply: {text[:200]!r}")


def complete_json(
    system: str, user: str, fallback: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int], int, list[str]]:
    """complete() plus JSON parsing; falls back instead of aborting the case."""
    flags: list[str] = []
    try:
        text, usage, latency = complete(system, user)
    except LLMError:
        return dict(fallback), {"prompt": 0, "completion": 0}, 0, ["llm_unavailable"]

    try:
        return extract_json(text), usage, latency, flags
    except LLMError:
        return dict(fallback), usage, latency, ["llm_bad_json"]
