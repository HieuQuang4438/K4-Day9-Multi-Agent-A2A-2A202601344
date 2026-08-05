"""Deterministic re-generation of all 50 outputs for leaderboard A/B testing.

Reuses the confidence values from the completed LLM run and recomputes every
other field from the CSVs, so a rule change can be tested in seconds instead of
re-running the 25-minute agent pipeline.

Usage:
    python -m tools.regen                          # rebuild as-is (baseline)
    python -m tools.regen --categories english     # translated category names
    python -m tools.regen --refund-completion full-only
    python -m tools.regen --ranked-causes multi
    python -m tools.regen --sellers responsible
    python -m tools.regen --zip output.zip ...     # rebuild then zip
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src import config  # noqa: E402
from src.data_store import get_store  # noqa: E402
from src.facts import collect_case_facts  # noqa: E402
from src.policy import (  # noqa: E402
    LIMITS,
    PRIMARY_ACTION_BY_ISSUE,
    ROOT_CAUSE_BY_ISSUE,
    classify_primary,
    recommended_refund,
    responsible_parties,
    secondary_issues,
)

CASES = [f"EC_{n:03d}" for n in range(1, 51)]

# Secondary causes worth ranking below the primary one, per primary issue.
SECONDARY_CAUSES = {
    "late_delivery_seller": ["CARRIER_DELIVERED_AFTER_ESTIMATE"],
    "late_delivery_logistics": [],
    "canceled_order_paid": [],
    "unavailable_order_paid": [],
    "valid_split_payment": [],
    "unsupported_late_claim": [],
}


def build_actions(primary: str, secondary: list[str], refund: float, mode: str) -> list[str]:
    actions = [PRIMARY_ACTION_BY_ISSUE[primary]]
    if primary == "late_delivery_seller":
        actions.append("review_seller_handoff")
    elif primary == "late_delivery_logistics":
        actions.append("review_carrier_delay")

    if mode == "any-refund":
        include_completion = refund > 0
    else:  # full-only: only when the primary action is a full refund
        include_completion = PRIMARY_ACTION_BY_ISSUE[primary] == "issue_full_refund"
    if include_completion:
        actions.append("verify_refund_completion")

    if "multi_seller_order" in secondary:
        actions.append("coordinate_multi_seller_case")
    if "split_payment" in secondary and primary != "valid_split_payment":
        actions.append("verify_payment_allocation")
    return actions[: LIMITS["resolution_actions"]]


def build_ranked_causes(primary: str, secondary: list[str], facts: dict, mode: str) -> list[dict]:
    causes = [ROOT_CAUSE_BY_ISSUE[primary]]
    if mode == "multi":
        causes.extend(SECONDARY_CAUSES.get(primary, []))
        # a reconciled split payment is a supporting cause on any other branch
        if (
            "split_payment" in secondary
            and primary != "valid_split_payment"
            and facts["payment"]["reconciled"]
        ):
            causes.append("MULTIPLE_PAYMENTS_RECONCILED")
    deduped: list[str] = []
    for code in causes:
        if code not in deduped:
            deduped.append(code)
    return [
        {"cause_code": code, "rank": index}
        for index, code in enumerate(deduped[: LIMITS["ranked_causes"]], start=1)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--categories", choices=["portuguese", "english"], default="portuguese")
    parser.add_argument(
        "--refund-completion", choices=["any-refund", "full-only"], default="any-refund"
    )
    parser.add_argument("--ranked-causes", choices=["single", "multi"], default="single")
    parser.add_argument("--sellers", choices=["all", "responsible"], default="all")
    parser.add_argument(
        "--variance", choices=["signed", "null-if-early", "zero-if-early"], default="signed"
    )
    parser.add_argument("--handoff", choices=["per-seller", "per-item"], default="per-seller")
    parser.add_argument("--late-threshold", choices=["gt", "gte"], default="gt")
    parser.add_argument(
        "--blank",
        choices=[
            "related_orders",
            "product_context",
            "delivery_variance",
            "variance_only",
            "handoff_only",
            "late_sellers_only",
            "evidence",
            "secondary",
        ],
        default=None,
        help="probe: làm rỗng một thành phần để đo trọng số thực tế của nó",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="tên variant; ghi ra dist/<name>/ và dist/<name>.zip, không đụng output/",
    )
    args = parser.parse_args()

    # output/ is the submitted baseline and is never overwritten by a variant.
    if args.name:
        out_dir = config.ROOT / "dist" / args.name
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = config.OUTPUT_DIR

    # Drive the shared implementation in src/facts.py so the pipeline and this
    # tool can never diverge; nothing here post-processes delivery any more.
    config.EARLY_DELIVERY_VARIANCE = {
        "signed": "signed", "null-if-early": "null", "zero-if-early": "zero"
    }[args.variance]
    config.HANDOFF_GRANULARITY = "item" if args.handoff == "per-item" else "seller"
    config.LATE_HANDOFF_THRESHOLD = args.late_threshold

    store = get_store()
    translation = {}
    if args.categories == "english":
        table = pd.read_csv(config.ROOT / "data" / "product_category_name_translation.csv")
        translation = dict(
            zip(table["product_category_name"], table["product_category_name_english"])
        )

    inputs = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in config.INPUT_DIR.glob("EC_*.json")}
    changed = 0

    for case_id in CASES:
        order_id = inputs[case_id]["customer_request"]["claimed_order_id"]
        facts = collect_case_facts(store, order_id)

        baseline_path = config.OUTPUT_DIR / f"{case_id}.json"
        out_path = out_dir / f"{case_id}.json"
        previous = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
        confidence = previous.get("case_assessment", {}).get("confidence", 0.9)

        primary = classify_primary(facts)
        secondary = secondary_issues(facts)
        parties = responsible_parties(primary, facts)
        refund = recommended_refund(primary, facts)
        actions = build_actions(primary, secondary, refund, args.refund_completion)

        order = facts["order"]
        payment = facts["payment"]
        delivery = facts["delivery"]

        categories = order["category_names"]
        if translation:
            categories = [translation.get(name, name) for name in categories]

        if args.sellers == "responsible":
            party_ids = [p["party_id"] for p in parties if p["party_type"] == "seller"]
            sellers = party_ids or order["seller_ids"][: LIMITS["seller_ids"]]
        else:
            sellers = order["seller_ids"][: LIMITS["seller_ids"]]

        item_ids = order["item_ids"][: LIMITS["item_ids"]]
        payment_ids = payment["payment_ids"][: LIMITS["payment_ids"]]

        evidence = [f"order:{order_id}"]
        evidence += [f"item:{i}" for i in item_ids]
        evidence += [f"payment:{p}" for p in payment_ids]
        evidence += [f"seller:{p['party_id']}" for p in parties if p["party_type"] == "seller"]
        evidence.append(f"policy:{ROOT_CAUSE_BY_ISSUE[primary]}")

        draft = {
            "case_id": case_id,
            "case_assessment": {
                "primary_issue": primary,
                "secondary_issues": secondary,
                "case_status": "action_required" if refund > 0 else "no_action",
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": [order_id],
                "item_ids": item_ids,
                "seller_ids": sellers,
                "payment_ids": payment_ids,
            },
            "customer_context": {
                "customer_unique_id": facts["customer"]["customer_unique_id"],
                "related_order_ids": facts["customer"]["related_order_ids"][
                    : LIMITS["related_order_ids"]
                ],
            },
            "product_context": {
                "product_ids": order["product_ids"][: LIMITS["product_ids"]],
                "category_names": categories[: LIMITS["category_names"]],
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
                "ranked_causes": build_ranked_causes(primary, secondary, facts, args.ranked_causes),
                "responsible_parties": parties,
            },
            "evidence_ids": evidence[: LIMITS["evidence_ids"]],
            "financial_resolution": {"currency": "BRL", "recommended_refund_brl": refund},
            "resolution_actions": actions,
        }

        # Diagnostic probes: blank one component so the leaderboard reveals how
        # much that component is currently earning.
        if args.blank == "related_orders":
            draft["customer_context"]["related_order_ids"] = []
        elif args.blank == "product_context":
            draft["product_context"] = {"product_ids": [], "category_names": []}
        elif args.blank == "delivery_variance":
            draft["delivery_analysis"]["delivery_variance_hours"] = None
            draft["delivery_analysis"]["seller_handoff_analysis"] = []
            draft["delivery_analysis"]["late_handoff_seller_ids"] = []
        elif args.blank == "variance_only":
            draft["delivery_analysis"]["delivery_variance_hours"] = None
        elif args.blank == "handoff_only":
            draft["delivery_analysis"]["seller_handoff_analysis"] = []
        elif args.blank == "late_sellers_only":
            draft["delivery_analysis"]["late_handoff_seller_ids"] = []
        elif args.blank == "evidence":
            draft["evidence_ids"] = [f"order:{order_id}"]
        elif args.blank == "secondary":
            draft["case_assessment"]["secondary_issues"] = []

        if draft != previous:
            changed += 1
        out_path.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(
        f"đã dựng lại 50 case ({changed} case thay đổi) | "
        f"categories={args.categories} refund_completion={args.refund_completion} "
        f"ranked_causes={args.ranked_causes} sellers={args.sellers}"
    )

    if args.name:
        archive_path = config.ROOT / "dist" / f"{args.name}.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for case_id in CASES:
                archive.write(out_dir / f"{case_id}.json", arcname=f"output/{case_id}.json")
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.namelist()
        ok = entries == [f"output/{c}.json" for c in CASES]
        print(f"đã tạo {archive_path} — {len(entries)} file trong output/, đúng cấu trúc={ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
