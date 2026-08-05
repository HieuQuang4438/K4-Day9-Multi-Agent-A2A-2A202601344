"""EC_POLICY_V2 rule engine.

The priority ladder, refund maths and action ordering are deterministic. The
Policy agent calls into this module and explains the outcome; it never decides
the outcome on its own, so a hallucinated rule cannot reach the output file.
"""

from __future__ import annotations

from typing import Any

POLICY_VERSION = "EC_POLICY_V2"

ROOT_CAUSE_BY_ISSUE = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_HANDOFF_AFTER_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "valid_split_payment": "MULTIPLE_PAYMENTS_RECONCILED",
    "unsupported_late_claim": "DELIVERY_WITHIN_ESTIMATE",
}

PRIMARY_ACTION_BY_ISSUE = {
    "canceled_order_paid": "issue_full_refund",
    "unavailable_order_paid": "issue_full_refund",
    "late_delivery_seller": "refund_freight",
    "late_delivery_logistics": "refund_freight",
    "valid_split_payment": "explain_valid_split_payment",
    "unsupported_late_claim": "reject_late_refund",
}

LIMITS = {
    "order_ids": 5,
    "item_ids": 5,
    "seller_ids": 3,
    "payment_ids": 5,
    "related_order_ids": 5,
    "product_ids": 5,
    "category_names": 5,
    "ranked_causes": 3,
    "responsible_parties": 3,
    "evidence_ids": 20,
    "resolution_actions": 5,
}


def classify_primary(facts: dict[str, Any]) -> str:
    """Walk the EC_POLICY_V2 ladder top-down; first match wins."""
    order = facts["order"]
    delivery = facts["delivery"]
    payment = facts["payment"]

    status = order["order_status"]
    paid = payment["payment_total_brl"] > 0

    if status == "canceled" and paid:
        return "canceled_order_paid"
    if status == "unavailable" and paid:
        return "unavailable_order_paid"

    if delivery["delivered_late"]:
        if delivery["late_handoff_seller_ids"]:
            return "late_delivery_seller"
        return "late_delivery_logistics"

    if payment["payment_row_count"] >= 2 and payment["reconciled"]:
        return "valid_split_payment"

    return "unsupported_late_claim"


def secondary_issues(facts: dict[str, Any]) -> list[str]:
    """Fixed business order, appended only when the condition holds."""
    order = facts["order"]
    payment = facts["payment"]
    customer = facts["customer"]

    issues: list[str] = []
    if order["item_count"] >= 2:
        issues.append("multi_item_order")
    if len(order["seller_ids"]) >= 2:
        issues.append("multi_seller_order")
    if payment["payment_row_count"] >= 2:
        issues.append("split_payment")
    if customer["is_repeat_customer"]:
        issues.append("repeat_customer")
    if len(order["category_names"]) >= 2:
        issues.append("multiple_categories")
    return issues


def responsible_parties(primary: str, facts: dict[str, Any]) -> list[dict[str, str]]:
    if primary in ("canceled_order_paid", "unavailable_order_paid"):
        return [{"party_type": "platform", "party_id": "OLIST_PLATFORM"}]
    if primary == "late_delivery_seller":
        return [
            {"party_type": "seller", "party_id": seller_id}
            for seller_id in facts["delivery"]["late_handoff_seller_ids"]
        ][: LIMITS["responsible_parties"]]
    if primary == "late_delivery_logistics":
        return [{"party_type": "logistics_provider", "party_id": "LOGISTICS_PROVIDER"}]
    return []


def recommended_refund(primary: str, facts: dict[str, Any]) -> float:
    payment = facts["payment"]
    if primary in ("canceled_order_paid", "unavailable_order_paid"):
        return round(float(payment["payment_total_brl"]), 2)
    if primary in ("late_delivery_seller", "late_delivery_logistics"):
        return round(float(payment["freight_total_brl"] or 0.0), 2)
    return 0.0


def resolution_actions(
    primary: str, secondary: list[str], refund_brl: float
) -> list[str]:
    """Primary action first, then supplementary actions in policy order."""
    actions = [PRIMARY_ACTION_BY_ISSUE[primary]]

    if primary == "late_delivery_seller":
        actions.append("review_seller_handoff")
    elif primary == "late_delivery_logistics":
        actions.append("review_carrier_delay")

    if refund_brl > 0:
        actions.append("verify_refund_completion")

    if "multi_seller_order" in secondary:
        actions.append("coordinate_multi_seller_case")

    # valid_split_payment already explains the split, so the extra check is noise.
    if "split_payment" in secondary and primary != "valid_split_payment":
        actions.append("verify_payment_allocation")

    return actions[: LIMITS["resolution_actions"]]


def evidence_ids(
    primary: str, facts: dict[str, Any], parties: list[dict[str, str]]
) -> list[str]:
    order_id = facts["order"]["order_id"]
    ids = [f"order:{order_id}"]
    ids.extend(f"item:{item_id}" for item_id in facts["order"]["item_ids"])
    ids.extend(f"payment:{pid}" for pid in facts["payment"]["payment_ids"])
    ids.extend(
        f"seller:{party['party_id']}"
        for party in parties
        if party["party_type"] == "seller"
    )
    ids.append(f"policy:{ROOT_CAUSE_BY_ISSUE[primary]}")
    return ids[: LIMITS["evidence_ids"]]


def apply_policy(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic EC_POLICY_V2 verdict for one case."""
    primary = classify_primary(facts)
    secondary = secondary_issues(facts)
    parties = responsible_parties(primary, facts)
    refund = recommended_refund(primary, facts)
    actions = resolution_actions(primary, secondary, refund)

    return {
        "primary_issue": primary,
        "secondary_issues": secondary,
        "case_status": "action_required" if refund > 0 else "no_action",
        "root_cause_code": ROOT_CAUSE_BY_ISSUE[primary],
        "responsible_parties": parties,
        "recommended_refund_brl": refund,
        "resolution_actions": actions,
        "evidence_ids": evidence_ids(primary, facts, parties),
    }
