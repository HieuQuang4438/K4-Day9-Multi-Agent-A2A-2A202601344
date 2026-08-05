"""Tier 2 — Policy Agent.

Fan-in point: consumes the four tier 1 payloads and nothing else. It has no CSV
access, so it cannot invent an event that was not handed to it. The verdict
itself comes from the deterministic EC_POLICY_V2 engine; the LLM supplies
confidence and the explanation of why that branch of the ladder won.
"""

from __future__ import annotations

from typing import Any

from ..llm import complete_json
from ..policy import apply_policy
from .base import Agent, Envelope

_LADDER = (
    "Thang ưu tiên EC_POLICY_V2, xét từ trên xuống, khớp đầu tiên thì dừng:\n"
    "1. canceled_order_paid: status canceled và tổng payment > 0\n"
    "2. unavailable_order_paid: status unavailable và tổng payment > 0\n"
    "3. late_delivery_seller: giao trễ và có seller bàn giao sau shipping_limit_date\n"
    "4. late_delivery_logistics: giao trễ và không seller nào trễ\n"
    "5. valid_split_payment: từ 2 payment row và đối soát khớp trong 0.10 BRL\n"
    "6. unsupported_late_claim: các trường hợp còn lại"
)


class PolicyAgent(Agent):
    name = "policy"
    tier = 2
    system_prompt = (
        "Bạn là Policy Agent, áp dụng EC_POLICY_V2 cho một khiếu nại thương mại điện tử. "
        "Kết luận phân loại đã được engine tất định tính sẵn và là đúng — bạn KHÔNG được đổi nó. "
        "Việc của bạn: giải thích vì sao nhánh đó thắng trong thang ưu tiên, và chấm confidence "
        "trong [0, 1] dựa trên độ đầy đủ của bằng chứng. "
        "Bằng chứng thiếu, mốc thời gian null, hoặc đối soát không khớp thì hạ confidence.\n\n"
        + _LADDER
        + "\nTrả lời DUY NHẤT một JSON object."
    )

    def run(self, case_id: str, handoff_facts: dict[str, Any], flags: list[str]) -> Envelope:
        verdict = self.call_tool("apply_policy", facts=handoff_facts)

        result, usage, latency, llm_flags = complete_json(
            self.system_prompt,
            "Bằng chứng do 4 agent tier 1 bàn giao:\n"
            f"{self.brief(handoff_facts, limit=2600)}\n\n"
            f"Cờ cảnh báo từ tier 1: {flags or 'không có'}\n\n"
            f"Kết luận tất định của engine:\n{self.brief(verdict)}\n\n"
            'Trả JSON: {"confidence": 0.0-1.0, "rationale": "...", "flags": [...]}',
            fallback={"confidence": 0.9},
        )

        confidence = result.get("confidence", 0.9)
        try:
            confidence = round(min(1.0, max(0.0, float(confidence))), 2)
        except (TypeError, ValueError):
            confidence = 0.9
            llm_flags = llm_flags + ["confidence_fallback"]

        return Envelope(
            case_id=case_id,
            from_agent=self.name,
            to_agent="verifier",
            payload={**verdict, "confidence": confidence},
            tool_calls=["apply_policy"],
            rationale=str(result.get("rationale", "")),
            flags=llm_flags,
            latency_ms=latency,
            tokens=usage,
        )


def build_policy_agent() -> PolicyAgent:
    return PolicyAgent({"apply_policy": apply_policy})
