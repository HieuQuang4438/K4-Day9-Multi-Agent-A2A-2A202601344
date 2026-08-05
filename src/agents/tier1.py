"""Tier 1 domain agents: Customer, Order & Product, Payment, Delivery.

Each one calls exactly one deterministic tool, then asks the LLM for the parts
that are judgement rather than arithmetic: which entries survive an array cap,
what looks contradictory, how to explain the finding to the Policy agent.
"""

from __future__ import annotations

from typing import Any

from .. import facts as fact_tools
from ..llm import complete_json
from ..policy import LIMITS
from .base import Agent, Envelope

_JSON_RULE = (
    "Trả lời DUY NHẤT một JSON object, không thêm chữ nào ngoài JSON. "
    "Không được tự tính lại con số; số liệu đã cho là chính xác."
)


def _guarded_selection(
    proposed: Any, universe: list[str], limit: int
) -> tuple[list[str], list[str]]:
    """Accept the LLM's pick only if it is a valid subset; else take source order."""
    if len(universe) <= limit:
        return list(universe), []
    if isinstance(proposed, list):
        picked = [str(x) for x in proposed if isinstance(x, (str, int))]
        if picked and len(picked) <= limit and set(picked) <= set(universe):
            return picked, []
    return universe[:limit], ["cap_fallback_source_order"]


class CustomerAgent(Agent):
    name = "customer"
    tier = 1
    system_prompt = (
        "Bạn là Customer Agent trong hệ thống điều tra khiếu nại thương mại điện tử. "
        "Bạn sở hữu danh tính khách hàng và lịch sử order. "
        "Nhiệm vụ: xác nhận dữ liệu, chọn tối đa 5 related order tiêu biểu nếu vượt hạn, "
        "và mô tả ngắn gọn bối cảnh khách hàng cho Policy Agent. " + _JSON_RULE
    )

    def run(self, case_id: str, order_id: str) -> Envelope:
        data = self.call_tool("customer_facts", order_id=order_id)
        related = data["related_order_ids"]

        result, usage, latency, flags = complete_json(
            self.system_prompt,
            f"Dữ liệu khách hàng của order {order_id}:\n{self.brief(data)}\n\n"
            f"Số related order: {len(related)} (giới hạn {LIMITS['related_order_ids']}).\n"
            'Trả JSON: {"related_order_ids": [...], "rationale": "...", "flags": [...]}',
            fallback={"related_order_ids": related[: LIMITS["related_order_ids"]]},
        )

        picked, guard_flags = _guarded_selection(
            result.get("related_order_ids"), related, LIMITS["related_order_ids"]
        )
        return Envelope(
            case_id=case_id,
            from_agent=self.name,
            to_agent="policy",
            payload={
                "customer_unique_id": data["customer_unique_id"],
                "related_order_ids": picked,
                "is_repeat_customer": data["is_repeat_customer"],
            },
            tool_calls=["customer_facts"],
            rationale=str(result.get("rationale", "")),
            flags=flags + guard_flags + _llm_flags(result),
            latency_ms=latency,
            tokens=usage,
        )


class OrderProductAgent(Agent):
    name = "order_product"
    tier = 1
    system_prompt = (
        "Bạn là Order & Product Agent. Bạn sở hữu order, item, seller, product và category. "
        "Nhiệm vụ: xác nhận kiểm kê, chọn phần tử giữ lại khi mảng vượt giới hạn, "
        "và nêu cờ cảnh báo nếu dữ liệu bất thường. " + _JSON_RULE
    )

    def run(self, case_id: str, order_id: str) -> Envelope:
        data = self.call_tool("order_facts", order_id=order_id)

        result, usage, latency, flags = complete_json(
            self.system_prompt,
            f"Kiểm kê order {order_id}:\n{self.brief(data)}\n\n"
            f"Giới hạn: item {LIMITS['item_ids']}, seller {LIMITS['seller_ids']}, "
            f"product {LIMITS['product_ids']}, category {LIMITS['category_names']}.\n"
            'Trả JSON: {"item_ids": [...], "seller_ids": [...], "product_ids": [...], '
            '"category_names": [...], "rationale": "...", "flags": [...]}',
            fallback={},
        )

        guard_flags: list[str] = []
        selections: dict[str, list[str]] = {}
        for key, limit_key in (
            ("item_ids", "item_ids"),
            ("seller_ids", "seller_ids"),
            ("product_ids", "product_ids"),
            ("category_names", "category_names"),
        ):
            picked, extra = _guarded_selection(
                result.get(key), data[key], LIMITS[limit_key]
            )
            selections[key] = picked
            guard_flags.extend(f"{key}:{flag}" for flag in extra)

        if data["item_count"] == 0:
            guard_flags.append("no_item_rows")

        return Envelope(
            case_id=case_id,
            from_agent=self.name,
            to_agent="policy",
            payload={
                "order_status": data["order_status"],
                "item_count": data["item_count"],
                "all_seller_ids": data["seller_ids"],
                **selections,
            },
            tool_calls=["order_facts"],
            rationale=str(result.get("rationale", "")),
            flags=flags + guard_flags + _llm_flags(result),
            latency_ms=latency,
            tokens=usage,
        )


class PaymentAgent(Agent):
    name = "payment"
    tier = 1
    system_prompt = (
        "Bạn là Payment Agent. Bạn sở hữu payment row và việc đối soát với item + freight. "
        "Nhiệm vụ: đọc kết quả đối soát đã tính sẵn, giải thích chênh lệch, "
        "nêu cờ khi order không có item row hoặc khi lệch quá 0.10 BRL. " + _JSON_RULE
    )

    def run(self, case_id: str, order_id: str) -> Envelope:
        data = self.call_tool("payment_facts", order_id=order_id)

        result, usage, latency, flags = complete_json(
            self.system_prompt,
            f"Đối soát thanh toán order {order_id}:\n{self.brief(data)}\n\n"
            'Trả JSON: {"rationale": "...", "flags": [...]}',
            fallback={},
        )

        auto_flags: list[str] = []
        if data["reconciled"] is None:
            auto_flags.append("no_item_rows")
        elif not data["reconciled"]:
            auto_flags.append("payment_mismatch")

        picked_payments, cap_flags = _guarded_selection(
            None, data["payment_ids"], LIMITS["payment_ids"]
        )

        return Envelope(
            case_id=case_id,
            from_agent=self.name,
            to_agent="policy",
            payload={**data, "payment_ids": picked_payments},
            tool_calls=["payment_facts"],
            rationale=str(result.get("rationale", "")),
            flags=flags + auto_flags + cap_flags + _llm_flags(result),
            latency_ms=latency,
            tokens=usage,
        )


class DeliveryAgent(Agent):
    name = "delivery"
    tier = 1
    system_prompt = (
        "Bạn là Delivery Agent. Bạn sở hữu mốc thời gian giao hàng và bàn giao cho đơn vị vận chuyển. "
        "Nhiệm vụ: đọc variance đã tính sẵn, xác nhận seller nào bàn giao trễ, "
        "và mô tả cho Policy Agent biết trễ do seller hay do vận chuyển. " + _JSON_RULE
    )

    def run(self, case_id: str, order_id: str) -> Envelope:
        data = self.call_tool("delivery_facts", order_id=order_id)

        result, usage, latency, flags = complete_json(
            self.system_prompt,
            f"Phân tích giao hàng order {order_id}:\n{self.brief(data)}\n\n"
            'Trả JSON: {"rationale": "...", "flags": [...]}',
            fallback={},
        )

        auto_flags: list[str] = []
        if data["delivered_at"] is None:
            auto_flags.append("not_delivered")
        if data["delivered_late"] and not data["late_handoff_seller_ids"]:
            auto_flags.append("carrier_at_fault")

        return Envelope(
            case_id=case_id,
            from_agent=self.name,
            to_agent="policy",
            payload=data,
            tool_calls=["delivery_facts"],
            rationale=str(result.get("rationale", "")),
            flags=flags + auto_flags + _llm_flags(result),
            latency_ms=latency,
            tokens=usage,
        )


def _llm_flags(result: dict[str, Any]) -> list[str]:
    raw = result.get("flags")
    if not isinstance(raw, list):
        return []
    return [f"llm:{str(item)[:40]}" for item in raw[:5] if item]


def build_tier1(store: Any) -> list[Agent]:
    """Wire each agent to its own tool registry — nothing wider."""
    return [
        CustomerAgent(
            {
                "customer_facts": lambda order_id: fact_tools.customer_facts(
                    store, order_id, fact_tools.order_facts(store, order_id)["customer_id"]
                )
            }
        ),
        OrderProductAgent(
            {"order_facts": lambda order_id: fact_tools.order_facts(store, order_id)}
        ),
        PaymentAgent(
            {"payment_facts": lambda order_id: fact_tools.payment_facts(store, order_id)}
        ),
        DeliveryAgent(
            {"delivery_facts": lambda order_id: fact_tools.delivery_facts(store, order_id)}
        ),
    ]
