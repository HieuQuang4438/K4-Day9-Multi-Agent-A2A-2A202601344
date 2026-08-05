"""Coordinator Agent — intake, dispatch, fan-in, retry, final assembly.

Holds no data tool of its own: everything it writes came from another agent's
envelope. Tier 1 runs in a thread pool, so the tier 1 barrier costs the slowest
agent rather than the sum of four.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import config
from ..data_store import DataStore
from ..facts import collect_case_facts
from ..policy import LIMITS
from ..trace import TraceWriter
from .base import Agent, Envelope
from .policy_agent import build_policy_agent
from .tier1 import build_tier1
from .verifier_agent import build_verifier_agent

MAX_VERIFY_ROUNDS = 2


class Coordinator(Agent):
    name = "coordinator"
    tier = 0

    def __init__(self, store: DataStore, trace: TraceWriter) -> None:
        super().__init__({})
        self.store = store
        self.trace = trace
        self.tier1 = build_tier1(store)
        self.policy_agent = build_policy_agent()
        self.verifier_agent = build_verifier_agent()

    # -- fan-out / fan-in --------------------------------------------------

    def _dispatch_tier1(
        self, case_id: str, order_id: str, records: list[dict[str, Any]]
    ) -> dict[str, Envelope]:
        def invoke(agent: Agent) -> tuple[str, Envelope]:
            try:
                return agent.name, agent.run(case_id, order_id)
            except Exception as exc:  # keep one bad agent from killing the case
                return agent.name, Envelope(
                    case_id=case_id,
                    from_agent=agent.name,
                    to_agent="policy",
                    status="error",
                    flags=[f"agent_exception:{type(exc).__name__}"],
                )

        with ThreadPoolExecutor(max_workers=len(self.tier1)) as pool:
            results = dict(pool.map(invoke, self.tier1))

        for agent in self.tier1:
            envelope = results[agent.name]
            if envelope.status == "error":  # step 3: retry the failed agent alone
                try:
                    results[agent.name] = agent.run(case_id, order_id)
                    results[agent.name].flags.append("retried_after_error")
                except Exception as exc:
                    results[agent.name].flags.append(f"retry_failed:{type(exc).__name__}")
            records.append(results[agent.name].to_trace(1, ["coordinator"]))

        return results

    # -- assembly ----------------------------------------------------------

    def _merge_handoff(
        self, order_id: str, envelopes: dict[str, Envelope], deterministic: dict[str, Any]
    ) -> dict[str, Any]:
        """Fold four payloads into the fact shape the Policy engine expects.

        Falls back to the deterministic bundle for any agent that errored, so a
        transport failure degrades one rationale rather than the whole verdict.
        """
        customer = envelopes["customer"].payload or deterministic["customer"]
        order = envelopes["order_product"].payload
        payment = envelopes["payment"].payload or deterministic["payment"]
        delivery = envelopes["delivery"].payload or deterministic["delivery"]

        if not order:
            order = {
                "order_status": deterministic["order"]["order_status"],
                "item_count": deterministic["order"]["item_count"],
                "all_seller_ids": deterministic["order"]["seller_ids"],
                "item_ids": deterministic["order"]["item_ids"][: LIMITS["item_ids"]],
                "seller_ids": deterministic["order"]["seller_ids"][: LIMITS["seller_ids"]],
                "product_ids": deterministic["order"]["product_ids"][: LIMITS["product_ids"]],
                "category_names": deterministic["order"]["category_names"][
                    : LIMITS["category_names"]
                ],
            }

        return {
            "order": {
                "order_id": order_id,
                "order_status": order["order_status"],
                "item_count": order["item_count"],
                # all sellers drive the multi_seller_order test; the capped list
                # is what reaches affected_entities
                "seller_ids": order["all_seller_ids"],
                "selected_seller_ids": order["seller_ids"],
                "item_ids": order["item_ids"],
                "product_ids": order["product_ids"],
                "category_names": order["category_names"],
            },
            "customer": customer,
            "payment": payment,
            "delivery": delivery,
        }

    def _assemble(
        self, case_id: str, handoff: dict[str, Any], verdict: dict[str, Any]
    ) -> dict[str, Any]:
        order = handoff["order"]
        payment = handoff["payment"]
        delivery = handoff["delivery"]

        return {
            "case_id": case_id,
            "case_assessment": {
                "primary_issue": verdict["primary_issue"],
                "secondary_issues": verdict["secondary_issues"],
                "case_status": verdict["case_status"],
                "confidence": verdict["confidence"],
            },
            "affected_entities": {
                "order_ids": [order["order_id"]],
                "item_ids": order["item_ids"],
                "seller_ids": order["selected_seller_ids"],
                "payment_ids": payment["payment_ids"],
            },
            "customer_context": {
                "customer_unique_id": handoff["customer"]["customer_unique_id"],
                "related_order_ids": handoff["customer"]["related_order_ids"],
            },
            "product_context": {
                "product_ids": order["product_ids"],
                "category_names": order["category_names"],
            },
            "delivery_analysis": {
                "delivered_at": delivery["delivered_at"],
                "estimated_delivery_at": delivery["estimated_delivery_at"],
                "carrier_handoff_at": delivery["carrier_handoff_at"],
                "delivery_variance_hours": delivery["delivery_variance_hours"],
                "seller_handoff_analysis": delivery["seller_handoff_analysis"],
                "late_handoff_seller_ids": delivery["late_handoff_seller_ids"],
            },
            "payment_reconciliation": {
                "currency": "BRL",
                "item_total_brl": payment["item_total_brl"],
                "freight_total_brl": payment["freight_total_brl"],
                "expected_total_brl": payment["expected_total_brl"],
                "payment_total_brl": payment["payment_total_brl"],
                "difference_brl": payment["difference_brl"],
                "reconciled": payment["reconciled"],
                "payment_types": payment["payment_types"],
            },
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": verdict["root_cause_code"], "rank": 1}],
                "responsible_parties": verdict["responsible_parties"],
            },
            "evidence_ids": verdict["evidence_ids"],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": verdict["recommended_refund_brl"],
            },
            "resolution_actions": verdict["resolution_actions"],
        }

    @staticmethod
    def _deterministic_handoff(order_id: str, deterministic: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the handoff using source order only — no LLM selections."""
        order = deterministic["order"]
        payment = dict(deterministic["payment"])
        payment["payment_ids"] = payment["payment_ids"][: LIMITS["payment_ids"]]
        customer = dict(deterministic["customer"])
        customer["related_order_ids"] = customer["related_order_ids"][
            : LIMITS["related_order_ids"]
        ]
        return {
            "order": {
                "order_id": order_id,
                "order_status": order["order_status"],
                "item_count": order["item_count"],
                "seller_ids": order["seller_ids"],
                "selected_seller_ids": order["seller_ids"][: LIMITS["seller_ids"]],
                "item_ids": order["item_ids"][: LIMITS["item_ids"]],
                "product_ids": order["product_ids"][: LIMITS["product_ids"]],
                "category_names": order["category_names"][: LIMITS["category_names"]],
            },
            "customer": customer,
            "payment": payment,
            "delivery": deterministic["delivery"],
        }

    # -- per-case entry point ---------------------------------------------

    def run_case(self, case_path: Path) -> dict[str, Any]:
        case = json.loads(case_path.read_text(encoding="utf-8"))
        case_id = case["case_id"]
        order_id = case["customer_request"]["claimed_order_id"]

        # Buffer this case's records; with cases running in parallel a
        # per-record write would interleave lines between cases.
        records: list[dict[str, Any]] = []
        records.append(
            {
                "case_id": case_id,
                "agent": "coordinator",
                "tier": 0,
                "status": "dispatch",
                "inputs_from": ["input_file"],
                "tool_calls": [],
                "payload": {"claimed_order_id": order_id},
                "model": config.MODEL_ID,
            }
        )

        deterministic = collect_case_facts(self.store, order_id)
        envelopes = self._dispatch_tier1(case_id, order_id, records)
        tier1_flags = [flag for env in envelopes.values() for flag in env.flags]

        handoff = self._merge_handoff(order_id, envelopes, deterministic)
        policy_envelope = self.policy_agent.run(case_id, handoff, tier1_flags)
        records.append(policy_envelope.to_trace(2, sorted(envelopes)))

        draft = self._assemble(case_id, handoff, policy_envelope.payload)

        for attempt in range(1, MAX_VERIFY_ROUNDS + 1):
            verdict_envelope = self.verifier_agent.run(case_id, draft, handoff, attempt)
            records.append(verdict_envelope.to_trace(3, ["policy"]))
            if verdict_envelope.status == "ok":
                break
            # Rejected: drop the LLM's array selections and rebuild from source order.
            handoff = self._deterministic_handoff(order_id, deterministic)
            rebuilt = self.policy_agent.call_tool("apply_policy", facts=handoff)
            rebuilt["confidence"] = policy_envelope.payload["confidence"]
            draft = self._assemble(case_id, handoff, rebuilt)
        else:
            draft["case_assessment"]["confidence"] = min(
                draft["case_assessment"]["confidence"], 0.5
            )
            records.append(
                {
                    "case_id": case_id,
                    "agent": "coordinator",
                    "tier": 0,
                    "status": "verifier_override",
                    "inputs_from": ["verifier"],
                    "tool_calls": [],
                    "model": config.MODEL_ID,
                }
            )

        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = config.OUTPUT_DIR / case_path.name
        out_path.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        records.append(
            {
                "case_id": case_id,
                "agent": "coordinator",
                "tier": 0,
                "status": "written",
                "inputs_from": ["verifier"],
                "tool_calls": [],
                "payload": {"output": out_path.name},
                "model": config.MODEL_ID,
            }
        )

        self.trace.write_block(records)
        return draft
