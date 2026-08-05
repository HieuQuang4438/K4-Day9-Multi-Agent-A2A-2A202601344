"""Pre-submission gate.

Runs three independent checks over output/:
  1. file set is exactly EC_001..EC_050 and every file parses
  2. every case passes the deterministic schema/ID/limit validator
  3. every scored field matches the deterministic EC_POLICY_V2 baseline
     recomputed straight from the CSVs, so LLM drift cannot slip through

Usage:
    python -m tools.check_submission
    python -m tools.check_submission --zip output.zip
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows console defaults to cp1252 and cannot encode Vietnamese report text.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src import config  # noqa: E402
from src.data_store import get_store  # noqa: E402
from src.facts import collect_case_facts  # noqa: E402
from src.policy import apply_policy  # noqa: E402
from src.validate import validate_output  # noqa: E402

EXPECTED = [f"EC_{n:03d}" for n in range(1, 51)]

SCORED_FIELDS = (
    "primary_issue",
    "secondary_issues",
    "case_status",
    "recommended_refund_brl",
    "resolution_actions",
    "evidence_ids",
    "root_cause_code",
    "responsible_parties",
)


def _baseline_view(draft: dict) -> dict:
    """Pull the scored fields out of a submitted file, flattened for diffing."""
    return {
        "primary_issue": draft["case_assessment"]["primary_issue"],
        "secondary_issues": draft["case_assessment"]["secondary_issues"],
        "case_status": draft["case_assessment"]["case_status"],
        "recommended_refund_brl": draft["financial_resolution"]["recommended_refund_brl"],
        "resolution_actions": draft["resolution_actions"],
        "evidence_ids": draft["evidence_ids"],
        "root_cause_code": draft["root_cause_analysis"]["ranked_causes"][0]["cause_code"],
        "responsible_parties": draft["root_cause_analysis"]["responsible_parties"],
    }


def check() -> tuple[int, list[str]]:
    problems: list[str] = []
    store = get_store()

    present = sorted(p.stem for p in config.OUTPUT_DIR.glob("EC_*.json"))
    missing = [c for c in EXPECTED if c not in present]
    extra = [c for c in present if c not in EXPECTED]
    if missing:
        problems.append(f"thiếu {len(missing)} case: {missing[:10]}")
    if extra:
        problems.append(f"file lạ trong output/: {extra}")

    # .gitkeep stays for git; build_zip lists the 50 JSON explicitly so it
    # never reaches the archive.
    stray = [
        p.name
        for p in config.OUTPUT_DIR.iterdir()
        if p.suffix != ".json" and p.name != ".gitkeep"
    ]
    if stray:
        problems.append(f"file không phải .json trong output/: {stray}")

    primary_counts: collections.Counter[str] = collections.Counter()
    confidences: list[float] = []
    action_required = 0
    refund_total = 0.0
    checked = 0

    for case_id in EXPECTED:
        path = config.OUTPUT_DIR / f"{case_id}.json"
        if not path.exists():
            continue
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{case_id}: JSON hỏng — {exc}")
            continue

        if draft.get("case_id") != case_id:
            problems.append(f"{case_id}: case_id trong file là {draft.get('case_id')!r}")

        order_ids = draft.get("affected_entities", {}).get("order_ids") or []
        if not order_ids:
            problems.append(f"{case_id}: thiếu affected_entities.order_ids")
            continue

        facts = collect_case_facts(store, order_ids[0])
        facts_view = {
            "order": {**facts["order"], "selected_seller_ids": facts["order"]["seller_ids"]},
            "customer": facts["customer"],
            "payment": facts["payment"],
            "delivery": facts["delivery"],
        }

        for error in validate_output(draft, facts_view):
            problems.append(f"{case_id}: {error}")

        baseline = apply_policy(facts)
        submitted = _baseline_view(draft)
        for field in SCORED_FIELDS:
            expected = baseline[field]
            if field == "evidence_ids":
                # baseline builds from the uncapped source; a capped submission
                # is fine as long as it stays a prefix-consistent subset
                if not set(submitted[field]) <= set(expected):
                    problems.append(
                        f"{case_id}: evidence_ids có phần tử ngoài baseline: "
                        f"{sorted(set(submitted[field]) - set(expected))}"
                    )
                continue
            if submitted[field] != expected:
                problems.append(
                    f"{case_id}: {field} lệch baseline — "
                    f"nộp {submitted[field]!r}, baseline {expected!r}"
                )

        confidence = draft["case_assessment"].get("confidence")
        if isinstance(confidence, (int, float)):
            confidences.append(float(confidence))
        primary_counts[submitted["primary_issue"]] += 1
        if submitted["case_status"] == "action_required":
            action_required += 1
        refund_total += float(submitted["recommended_refund_brl"])
        checked += 1

    print(f"đã kiểm tra {checked}/50 case")
    if confidences:
        print(
            f"confidence: min {min(confidences):.2f} "
            f"trung bình {sum(confidences) / len(confidences):.2f} "
            f"max {max(confidences):.2f}"
        )
    print(f"action_required {action_required} | no_action {checked - action_required}")
    print(f"tổng refund đề xuất: {refund_total:.2f} BRL")
    print("phân bố primary issue:")
    for issue, count in primary_counts.most_common():
        print(f"  {issue:<26} {count}")

    return checked, problems


def build_zip(target: Path) -> None:
    """Zip exactly the 50 JSON, flat, nothing else."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for case_id in EXPECTED:
            path = config.OUTPUT_DIR / f"{case_id}.json"
            archive.write(path, arcname=path.name)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    print(f"\nđã tạo {target} — {len(names)} file")
    if names != [f"{c}.json" for c in EXPECTED]:
        print("CẢNH BÁO: nội dung zip không đúng 50 file mong đợi", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", type=str, default=None, help="đóng gói zip nếu không có lỗi")
    args = parser.parse_args()

    checked, problems = check()

    if problems:
        print(f"\n{len(problems)} vấn đề:", file=sys.stderr)
        for problem in problems[:60]:
            print(f"  - {problem}", file=sys.stderr)
        if len(problems) > 60:
            print(f"  ... còn {len(problems) - 60} vấn đề nữa", file=sys.stderr)
        return 1

    print("\nkhông có vấn đề nào")
    if args.zip:
        if checked != 50:
            print("chưa đủ 50 case, không đóng zip", file=sys.stderr)
            return 1
        build_zip(Path(args.zip))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
