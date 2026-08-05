"""Runtime configuration.

Model identity lives here in source (not in .env) so the grader can read which
model produced the submission. Only the secret goes in .env.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Model ---------------------------------------------------------------
# Gemma 3 4B: 4B parameters, inside the <=10B lab gate.
MODEL_ID = "google/gemma-3-4b-it"
MODEL_PARAMETER_SIZE = "4B"
PROVIDER = "openrouter"
API_BASE = "https://openrouter.ai/api/v1/chat/completions"

TEMPERATURE = 0.0
MAX_TOKENS = 1024
REQUEST_TIMEOUT_S = 90
MAX_RETRIES = 3

# Cases are independent, so they run in parallel. Each case still issues at most
# 4 concurrent tier 1 calls, so peak concurrency is MAX_CASE_WORKERS * 4.
MAX_CASE_WORKERS = 8

# --- Paths ---------------------------------------------------------------
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logging"
TRACE_PATH = LOG_DIR / "trace.jsonl"

POLICY_VERSION = "EC_POLICY_V2"

# --- Delivery conventions the spec leaves open ---------------------------
# How to report delivery_variance_hours when the order arrived early:
#   "signed" keeps the negative number, "null" and "zero" flatten it.
EARLY_DELIVERY_VARIANCE = "signed"
# One seller_handoff_analysis row per "seller" (earliest limit) or per "item".
HANDOFF_GRANULARITY = "seller"
# Whether a handoff exactly on the deadline counts as late: "gt" or "gte".
LATE_HANDOFF_THRESHOLD = "gt"


def api_keys() -> list[str]:
    """All OpenRouter keys, in order.

    Reads OPENROUTER_API_KEY plus any OPENROUTER_API_KEY_2, _3, ... so extra
    keys can be added without touching code. Calls rotate across them, which
    only matters once concurrency is high enough to hit a rate limit.
    """
    found: dict[str, str] = {}

    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            name = name.strip()
            if name.startswith("OPENROUTER_API_KEY") and value.strip():
                found[name] = value.strip()

    for name, value in os.environ.items():
        if name.startswith("OPENROUTER_API_KEY") and value:
            found[name] = value

    keys = [found[name] for name in sorted(found)]
    if not keys:
        raise RuntimeError("no OPENROUTER_API_KEY* found in environment or .env")
    return keys


def api_key() -> str:
    return api_keys()[0]
