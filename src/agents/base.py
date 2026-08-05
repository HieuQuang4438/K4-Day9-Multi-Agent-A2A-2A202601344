"""A2A envelope and the agent contract shared by all seven agents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Envelope:
    case_id: str
    from_agent: str
    to_agent: str
    status: str = "ok"
    payload: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    rationale: str = ""
    flags: list[str] = field(default_factory=list)
    latency_ms: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0})

    @property
    def task_id(self) -> str:
        return f"{self.case_id}::{self.from_agent}"

    def to_trace(self, tier: int, inputs_from: list[str]) -> dict[str, Any]:
        from .. import config

        return {
            "case_id": self.case_id,
            "task_id": self.task_id,
            "agent": self.from_agent,
            "to_agent": self.to_agent,
            "tier": tier,
            "inputs_from": inputs_from,
            "tool_calls": self.tool_calls,
            "status": self.status,
            "flags": self.flags,
            "rationale": self.rationale,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "model": config.MODEL_ID,
        }


class ToolAccessError(RuntimeError):
    """Raised when an agent calls a tool outside its registry."""


class Agent:
    """Base agent: owns a name, a tier and a closed tool registry.

    The registry is the access control from architecture.md section 3 — an agent
    physically cannot reach a table outside its domain.
    """

    name: str = "agent"
    tier: int = 0
    system_prompt: str = ""

    def __init__(self, tools: dict[str, Callable[..., Any]] | None = None) -> None:
        self._tools = tools or {}

    def call_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        if tool_name not in self._tools:
            raise ToolAccessError(
                f"agent {self.name!r} không có quyền gọi tool {tool_name!r}"
            )
        return self._tools[tool_name](*args, **kwargs)

    @staticmethod
    def brief(payload: dict[str, Any], limit: int = 2000) -> str:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return text if len(text) <= limit else text[:limit] + "\n...(cắt bớt)"
