"""Olist CSV loader with pre-built indexes.

Loaded once per process; every agent tool reads from this shared store so that
all agents see identical source facts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TIMESTAMP_COLUMNS = {
    "olist_orders_dataset.csv": [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ],
    "olist_order_items_dataset.csv": ["shipping_limit_date"],
}


def _read(name: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / name)
    for column in TIMESTAMP_COLUMNS.get(name, []):
        df[column] = pd.to_datetime(df[column], errors="coerce")
    return df


class DataStore:
    """Indexed view over the nine Olist tables."""

    def __init__(self) -> None:
        self.orders = _read("olist_orders_dataset.csv")
        self.items = _read("olist_order_items_dataset.csv")
        self.payments = _read("olist_order_payments_dataset.csv")
        self.customers = _read("olist_customers_dataset.csv")
        self.products = _read("olist_products_dataset.csv")
        self.sellers = _read("olist_sellers_dataset.csv")

        self._orders_by_id = self.orders.set_index("order_id", drop=False)
        self._items_by_order = self.items.sort_values(
            ["order_id", "order_item_id"]
        ).groupby("order_id")
        self._payments_by_order = self.payments.sort_values(
            ["order_id", "payment_sequential"]
        ).groupby("order_id")
        self._customers_by_id = self.customers.set_index("customer_id", drop=False)
        self._orders_by_customer_unique = self.orders.merge(
            self.customers[["customer_id", "customer_unique_id"]],
            on="customer_id",
            how="left",
        ).groupby("customer_unique_id")
        self._product_category = dict(
            zip(self.products["product_id"], self.products["product_category_name"])
        )

    # -- lookups -----------------------------------------------------------

    def order(self, order_id: str) -> pd.Series | None:
        if order_id not in self._orders_by_id.index:
            return None
        return self._orders_by_id.loc[order_id]

    def items_of(self, order_id: str) -> pd.DataFrame:
        try:
            return self._items_by_order.get_group(order_id)
        except KeyError:
            return self.items.iloc[0:0]

    def payments_of(self, order_id: str) -> pd.DataFrame:
        try:
            return self._payments_by_order.get_group(order_id)
        except KeyError:
            return self.payments.iloc[0:0]

    def customer(self, customer_id: str) -> pd.Series | None:
        if customer_id not in self._customers_by_id.index:
            return None
        return self._customers_by_id.loc[customer_id]

    def orders_of_customer_unique(self, customer_unique_id: str) -> pd.DataFrame:
        try:
            return self._orders_by_customer_unique.get_group(customer_unique_id)
        except KeyError:
            return self.orders.iloc[0:0]

    def category_of(self, product_id: str) -> str | None:
        value = self._product_category.get(product_id)
        if value is None or pd.isna(value):
            return None
        return str(value)


@lru_cache(maxsize=1)
def get_store() -> DataStore:
    return DataStore()
