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


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENROUTER_API_KEY":
                return value.strip()
    raise RuntimeError("OPENROUTER_API_KEY not found in environment or .env")
