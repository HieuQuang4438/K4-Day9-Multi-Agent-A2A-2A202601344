"""Tier 3 — Verifier Agent.

A gate, not a logger. Deterministic checks decide accept/reject; the LLM adds a
second opinion recorded as flags. Rejection sends the draft back to the
Coordinator, which rebuilds it without the LLM-chosen array selections.
"""

from __future__ import annotations

from typing import Any

from ..llm import complete_json
from ..validate import validate_output
from .base import Agent, Envelope


class VerifierAgent(Agent):
    name = "verifier"
    tier = 3
    system_prompt = (
        "Bạn là Verifier Agent, cổng kiểm chứng cuối trước khi ghi file. "
        "Bạn nhận một JSON dự thảo và danh sách lỗi do bộ kiểm tra tất định phát hiện. "
        "Việc của bạn: soát những điểm mà rule khó bắt — evidence có khớp entity thật không, "
        "case_status có nhất quán với refund không, action có hợp lý với primary issue không. "
        "Không được sửa số. Trả lời DUY NHẤT một JSON object."
    )

    def run(
        self, case_id: str, draft: dict[str, Any], handoff_facts: dict[str, Any], attempt: int
    ) -> Envelope:
        errors = self.call_tool("validate_output", draft=draft, facts=handoff_facts)

        result, usage, latency, llm_flags = complete_json(
            self.system_prompt,
            f"JSON dự thảo:\n{self.brief(draft, limit=2600)}\n\n"
            f"Lỗi tất định phát hiện được: {errors or 'không có'}\n\n"
            'Trả JSON: {"concerns": [...], "rationale": "..."}',
            fallback={"concerns": []},
        )

        concerns = result.get("concerns")
        concern_flags = (
            [f"llm_concern:{str(c)[:60]}" for c in concerns[:3]]
            if isinstance(concerns, list)
            else []
        )

        return Envelope(
            case_id=case_id,
            from_agent=self.name,
            to_agent="coordinator",
            status="ok" if not errors else "rejected",
            payload={"errors": errors, "attempt": attempt},
            tool_calls=["validate_output"],
            rationale=str(result.get("rationale", "")),
            flags=llm_flags + concern_flags,
            latency_ms=latency,
            tokens=usage,
        )


def build_verifier_agent() -> VerifierAgent:
    return VerifierAgent({"validate_output": validate_output})
