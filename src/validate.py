"""Deterministic output gate used by the Verifier agent.

Returns a list of human-readable errors; empty list means the draft is
submittable. Checks mirror architecture.md section 4.3.
"""

from __future__ import annotations

import re
from typing import Any

from .policy import LIMITS

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

REQUIRED_SECTIONS = {
    "case_id": str,
    "case_assessment": dict,
    "affected_entities": dict,
    "customer_context": dict,
    "product_context": dict,
    "delivery_analysis": dict,
    "payment_reconciliation": dict,
    "root_cause_analysis": dict,
    "evidence_ids": list,
    "financial_resolution": dict,
    "resolution_actions": list,
}

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "unsupported_late_claim",
}

SECONDARY_ORDER = [
    "multi_item_order",
    "multi_seller_order",
    "split_payment",
    "repeat_customer",
    "multiple_categories",
]


def _check_timestamp(errors: list[str], label: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not TIMESTAMP.match(value):
        errors.append(f"{label}: timestamp không đúng định dạng: {value!r}")


def validate_output(draft: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    for key, expected_type in REQUIRED_SECTIONS.items():
        if key not in draft:
            errors.append(f"thiếu khóa bắt buộc: {key}")
        elif not isinstance(draft[key], expected_type):
            errors.append(f"{key}: sai kiểu, cần {expected_type.__name__}")
    if errors:
        return errors

    order_id = facts["order"]["order_id"]
    valid_items = set(facts["order"]["item_ids"])
    valid_payments = set(facts["payment"]["payment_ids"])
    valid_sellers = set(facts["order"]["seller_ids"])
    valid_related = set(facts["customer"]["related_order_ids"])
    valid_products = set(facts["order"]["product_ids"])
    valid_categories = set(facts["order"]["category_names"])

    # -- array limits ------------------------------------------------------
    entities = draft["affected_entities"]
    sized = {
        "order_ids": entities.get("order_ids", []),
        "item_ids": entities.get("item_ids", []),
        "seller_ids": entities.get("seller_ids", []),
        "payment_ids": entities.get("payment_ids", []),
        "related_order_ids": draft["customer_context"].get("related_order_ids", []),
        "product_ids": draft["product_context"].get("product_ids", []),
        "category_names": draft["product_context"].get("category_names", []),
        "ranked_causes": draft["root_cause_analysis"].get("ranked_causes", []),
        "responsible_parties": draft["root_cause_analysis"].get(
            "responsible_parties", []
        ),
        "evidence_ids": draft["evidence_ids"],
        "resolution_actions": draft["resolution_actions"],
    }
    for name, values in sized.items():
        if len(values) > LIMITS[name]:
            errors.append(f"{name}: {len(values)} phần tử, vượt giới hạn {LIMITS[name]}")

    # -- entity existence --------------------------------------------------
    if entities.get("order_ids") != [order_id]:
        errors.append("affected_entities.order_ids phải chỉ chứa claimed order")
    for item_id in entities.get("item_ids", []):
        if item_id not in valid_items:
            errors.append(f"item_id không tồn tại trong CSV: {item_id}")
    for payment_id in entities.get("payment_ids", []):
        if payment_id not in valid_payments:
            errors.append(f"payment_id không tồn tại trong CSV: {payment_id}")
    for seller_id in entities.get("seller_ids", []):
        if seller_id not in valid_sellers:
            errors.append(f"seller_id không thuộc order: {seller_id}")
    for related in draft["customer_context"].get("related_order_ids", []):
        if related not in valid_related:
            errors.append(f"related_order_id không thuộc khách hàng: {related}")
        if related == order_id:
            errors.append("related_order_ids không được chứa chính claimed order")
    for product_id in draft["product_context"].get("product_ids", []):
        if product_id not in valid_products:
            errors.append(f"product_id không thuộc order: {product_id}")
    for category in draft["product_context"].get("category_names", []):
        if category not in valid_categories:
            errors.append(f"category không thuộc order: {category}")

    # -- evidence ID format and existence ----------------------------------
    for evidence in draft["evidence_ids"]:
        kind, _, rest = str(evidence).partition(":")
        if kind == "order":
            ok = rest == order_id
        elif kind == "item":
            ok = rest in valid_items
        elif kind == "payment":
            ok = rest in valid_payments
        elif kind == "seller":
            ok = rest in valid_sellers
        elif kind == "policy":
            ok = bool(rest)
        else:
            ok = False
        if not ok:
            errors.append(f"evidence ID không dựng được từ dữ liệu: {evidence}")

    # -- null handling on item-less orders ---------------------------------
    reconciliation = draft["payment_reconciliation"]
    delivery = draft["delivery_analysis"]
    if facts["order"]["item_count"] == 0:
        for field in ("expected_total_brl", "difference_brl", "reconciled"):
            if reconciliation.get(field) is not None:
                errors.append(f"order không có item row: {field} phải là null")
        for field, container in (
            ("item_ids", entities),
            ("seller_ids", entities),
            ("product_ids", draft["product_context"]),
            ("category_names", draft["product_context"]),
        ):
            if container.get(field):
                errors.append(f"order không có item row: {field} phải là mảng rỗng")
        if delivery.get("seller_handoff_analysis"):
            errors.append("order không có item row: seller_handoff_analysis phải rỗng")

    # -- timestamps --------------------------------------------------------
    for field in ("delivered_at", "estimated_delivery_at", "carrier_handoff_at"):
        _check_timestamp(errors, f"delivery_analysis.{field}", delivery.get(field))
    for entry in delivery.get("seller_handoff_analysis", []):
        _check_timestamp(
            errors, "seller_handoff_analysis.shipping_limit_at", entry.get("shipping_limit_at")
        )

    # -- assessment consistency -------------------------------------------
    assessment = draft["case_assessment"]
    primary = assessment.get("primary_issue")
    if primary not in PRIMARY_ISSUES:
        errors.append(f"primary_issue không hợp lệ: {primary!r}")

    secondary = assessment.get("secondary_issues", [])
    unknown = [s for s in secondary if s not in SECONDARY_ORDER]
    if unknown:
        errors.append(f"secondary_issues lạ: {unknown}")
    ranks = [SECONDARY_ORDER.index(s) for s in secondary if s in SECONDARY_ORDER]
    if ranks != sorted(ranks):
        errors.append("secondary_issues sai thứ tự nghiệp vụ")

    confidence = assessment.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        errors.append(f"confidence ngoài khoảng [0, 1]: {confidence!r}")

    refund = draft["financial_resolution"].get("recommended_refund_brl")
    if not isinstance(refund, (int, float)):
        errors.append("recommended_refund_brl phải là số")
    else:
        expected_status = "action_required" if refund > 0 else "no_action"
        if assessment.get("case_status") != expected_status:
            errors.append(
                f"case_status={assessment.get('case_status')!r} không khớp refund={refund}"
            )

    return errors
