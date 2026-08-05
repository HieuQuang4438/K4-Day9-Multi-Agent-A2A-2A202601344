"""Deterministic fact extraction per domain.

Every number that lands in the output JSON is computed here, never by an LLM.
Each function maps to the domain of one agent, so an agent's "work" is calling
its own tool and reasoning about the result it hands off.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import config
from .data_store import DataStore

TOLERANCE_BRL = 0.10


def _ts(value: Any) -> str | None:
    """CSV timestamp format, or None when the source cell is empty."""
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _hours(later: Any, earlier: Any) -> float | None:
    if later is None or earlier is None or pd.isna(later) or pd.isna(earlier):
        return None
    delta = pd.Timestamp(later) - pd.Timestamp(earlier)
    return round(delta.total_seconds() / 3600.0, 2)


def _brl(value: float) -> float:
    return round(float(value), 2)


# -- Order & Product agent ------------------------------------------------


def order_facts(store: DataStore, order_id: str) -> dict[str, Any]:
    order = store.order(order_id)
    if order is None:
        raise KeyError(f"order_id not found in orders dataset: {order_id}")

    items = store.items_of(order_id)
    product_ids: list[str] = []
    category_names: list[str] = []
    seller_ids: list[str] = []
    item_ids: list[str] = []

    for _, row in items.iterrows():
        item_ids.append(f"{order_id}:{int(row['order_item_id'])}")
        if row["seller_id"] not in seller_ids:
            seller_ids.append(str(row["seller_id"]))
        if row["product_id"] not in product_ids:
            product_ids.append(str(row["product_id"]))
        category = store.category_of(str(row["product_id"]))
        if category is not None and category not in category_names:
            category_names.append(category)

    return {
        "order_id": order_id,
        "customer_id": str(order["customer_id"]),
        "order_status": str(order["order_status"]),
        "item_count": int(len(items)),
        "item_ids": item_ids,
        "seller_ids": seller_ids,
        "product_ids": product_ids,
        "category_names": category_names,
    }


# -- Customer agent -------------------------------------------------------


def customer_facts(store: DataStore, order_id: str, customer_id: str) -> dict[str, Any]:
    customer = store.customer(customer_id)
    if customer is None:
        return {"customer_unique_id": None, "related_order_ids": []}

    unique_id = str(customer["customer_unique_id"])
    history = store.orders_of_customer_unique(unique_id)
    related = [
        str(other)
        for other in history["order_id"].tolist()
        if str(other) != order_id
    ]
    return {
        "customer_unique_id": unique_id,
        "related_order_ids": related,
        "is_repeat_customer": bool(related),
    }


# -- Delivery agent -------------------------------------------------------


def delivery_facts(store: DataStore, order_id: str) -> dict[str, Any]:
    """Delivery and handoff variance.

    Three conventions the lab spec leaves open are configurable in
    src/config.py so the pipeline and the regeneration tool always agree:
    how an early delivery is reported, whether handoff rows are grouped per
    seller or per item, and whether an exactly-on-deadline handoff counts late.
    """
    order = store.order(order_id)
    items = store.items_of(order_id)

    delivered_at = order["order_delivered_customer_date"]
    estimated_at = order["order_estimated_delivery_date"]
    carrier_at = order["order_delivered_carrier_date"]

    raw_variance = _hours(delivered_at, estimated_at)
    # delivered_late must come from the raw value: reporting an early delivery
    # as null or 0 must never change which policy branch the case falls into.
    delivered_late = bool(raw_variance is not None and raw_variance > 0)

    delivery_variance = raw_variance
    if raw_variance is not None and raw_variance < 0:
        if config.EARLY_DELIVERY_VARIANCE == "null":
            delivery_variance = None
        elif config.EARLY_DELIVERY_VARIANCE == "zero":
            delivery_variance = 0.0

    def _is_late(variance: float | None) -> bool:
        if variance is None:
            return False
        return variance >= 0 if config.LATE_HANDOFF_THRESHOLD == "gte" else variance > 0

    handoff_analysis: list[dict[str, Any]] = []
    late_seller_ids: list[str] = []

    if not items.empty:
        if config.HANDOFF_GRANULARITY == "item":
            rows = [
                (str(row["seller_id"]), row["shipping_limit_date"])
                for _, row in items.sort_values("order_item_id").iterrows()
            ]
        else:
            # Earliest shipping_limit_date per seller: the deadline that seller missed.
            rows = [
                (str(seller_id), group["shipping_limit_date"].min())
                for seller_id, group in items.groupby("seller_id", sort=False)
            ]

        for seller_id, limit in rows:
            variance = _hours(carrier_at, limit)
            late = _is_late(variance)
            handoff_analysis.append(
                {
                    "seller_id": seller_id,
                    "shipping_limit_at": _ts(limit),
                    "handoff_variance_hours": variance,
                    "late_handoff": late,
                }
            )
            if late and seller_id not in late_seller_ids:
                late_seller_ids.append(seller_id)

    return {
        "delivered_at": _ts(delivered_at),
        "estimated_delivery_at": _ts(estimated_at),
        "carrier_handoff_at": _ts(carrier_at),
        "delivery_variance_hours": delivery_variance,
        "seller_handoff_analysis": handoff_analysis,
        "late_handoff_seller_ids": late_seller_ids,
        "delivered_late": delivered_late,
    }


# -- Payment agent --------------------------------------------------------


def payment_facts(store: DataStore, order_id: str) -> dict[str, Any]:
    items = store.items_of(order_id)
    payments = store.payments_of(order_id)

    payment_total = _brl(payments["payment_value"].sum()) if not payments.empty else 0.0

    payment_ids: list[str] = []
    payment_types: list[str] = []
    for _, row in payments.iterrows():
        payment_ids.append(f"{order_id}:{int(row['payment_sequential'])}")
        if row["payment_type"] not in payment_types:
            payment_types.append(str(row["payment_type"]))

    if items.empty:
        # No item rows: totals are unknowable, not zero.
        item_total = None
        freight_total = None
        expected_total = None
        difference = None
        reconciled = None
    else:
        item_total = _brl(items["price"].sum())
        freight_total = _brl(items["freight_value"].sum())
        expected_total = _brl(item_total + freight_total)
        difference = _brl(payment_total - expected_total)
        reconciled = bool(abs(difference) <= TOLERANCE_BRL)

    return {
        "currency": "BRL",
        "item_total_brl": item_total,
        "freight_total_brl": freight_total,
        "expected_total_brl": expected_total,
        "payment_total_brl": payment_total,
        "difference_brl": difference,
        "reconciled": reconciled,
        "payment_types": payment_types,
        "payment_ids": payment_ids,
        "payment_row_count": int(len(payments)),
    }


def collect_case_facts(store: DataStore, order_id: str) -> dict[str, Any]:
    """Full deterministic fact bundle for one case."""
    order = order_facts(store, order_id)
    return {
        "order": order,
        "customer": customer_facts(store, order_id, order["customer_id"]),
        "delivery": delivery_facts(store, order_id),
        "payment": payment_facts(store, order_id),
    }
