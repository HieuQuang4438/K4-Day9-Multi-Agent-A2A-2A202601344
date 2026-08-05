"""Run trace writer.

Opened with mode="w" exactly once per run, never "a", so trace.jsonl always
holds one run and nothing older.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self.line_count = 0

    def write(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
            self.line_count += 1

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
