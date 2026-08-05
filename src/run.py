"""Entry point: python -m src.run [--limit N] [--case EC_001]

Truncates logging/trace.jsonl once at start, then walks the cases in order.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from .agents.coordinator import Coordinator
from .data_store import get_store
from .trace import TraceWriter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="chỉ chạy N case đầu")
    parser.add_argument("--case", type=str, default=None, help="chạy đúng một case, ví dụ EC_001")
    parser.add_argument(
        "--workers",
        type=int,
        default=config.MAX_CASE_WORKERS,
        help="số case chạy song song (cases độc lập nhau)",
    )
    args = parser.parse_args()

    cases = sorted(config.INPUT_DIR.glob("EC_*.json"))
    if args.case:
        cases = [p for p in cases if p.stem == args.case]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("không tìm thấy case nào trong input/", file=sys.stderr)
        return 1

    print(f"model={config.MODEL_ID} cases={len(cases)} workers={args.workers} keys={len(config.api_keys())}")
    store = get_store()
    started = time.time()
    failures: list[str] = []

    with TraceWriter(config.TRACE_PATH) as trace:
        coordinator = Coordinator(store, trace)
        print_lock = threading.Lock()
        done = 0

        def process(case_path):
            case_started = time.time()
            draft = coordinator.run_case(case_path)
            return case_path, draft, time.time() - case_started

        # Cases share no state, so they run concurrently; each case still
        # flushes its own trace block, keeping the file readable.
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(process, path): path for path in cases}
            for future in as_completed(futures):
                path = futures[future]
                try:
                    case_path, draft, elapsed = future.result()
                except Exception as exc:
                    failures.append(path.stem)
                    with print_lock:
                        done += 1
                        print(
                            f"[{done:>2}/{len(cases)}] {path.stem} FAILED: {exc}",
                            file=sys.stderr,
                        )
                    continue
                with print_lock:
                    done += 1
                    print(
                        f"[{done:>2}/{len(cases)}] {case_path.stem} "
                        f"{draft['case_assessment']['primary_issue']:<24} "
                        f"refund={draft['financial_resolution']['recommended_refund_brl']:>8.2f} "
                        f"{elapsed:.1f}s"
                    )

        trace_lines = trace.line_count

    print(
        f"\nxong {len(cases) - len(failures)}/{len(cases)} case "
        f"trong {time.time() - started:.1f}s, trace {trace_lines} dòng"
    )
    if failures:
        print(f"case lỗi: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
