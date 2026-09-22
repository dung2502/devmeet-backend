"""CLI entrypoint for Phase 6A Preflight Inspection

Usage:
    python scripts/run_preflight_6a.py
"""

import sys
from pathlib import Path

# Ensure backend root is in sys.path
backend_root = Path(__file__).resolve().parent.parent
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.preflight import run_full_preflight


def main() -> int:
    print("=" * 70)
    print("DEVMEET AI — PHASE 6A PREFLIGHT INSPECTION REPORT")
    print("=" * 70)

    report = run_full_preflight()

    print(f"\n{'CATEGORY':<20} | {'NAME':<32} | {'STATUS':<10} | {'EVIDENCE'}")
    print("-" * 100)

    for item in report.items:
        print(f"{item.category:<20} | {item.name:<32} | {item.status:<10} | {item.evidence}")

    print("-" * 100)
    print(f"\nTotal checks: {len(report.items)}")
    pass_count = sum(1 for i in report.items if i.status in ("PASS", "CONFIGURED"))
    fail_count = sum(1 for i in report.items if i.status in ("FAIL", "MISSING"))
    partial_count = sum(1 for i in report.items if i.status == "PARTIAL")

    print(f"Passed / Configured: {pass_count}")
    print(f"Partial:             {partial_count}")
    print(f"Failed / Missing:    {fail_count}")
    print("=" * 70)

    if fail_count > 0:
        print("\n[RESULT]: PREFLIGHT BLOCKED / FAILED")
        return 1
    else:
        print("\n[RESULT]: PREFLIGHT PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
