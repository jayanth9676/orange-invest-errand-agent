#!/usr/bin/env python3
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

from tests import conftest, test_eval_scenarios, test_regression_bugs


def main() -> int:
    conftest.EVAL_RESULTS.clear()
    passed = 0
    failed = 0
    seen_funcs = set()

    for mod in (test_eval_scenarios, test_regression_bugs):
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("test_") and fn not in seen_funcs:
                seen_funcs.add(fn)
                try:
                    fn()
                    print(f"[PASS] {mod.__name__}::{name}")
                    passed += 1
                except Exception as exc:
                    print(f"[FAIL] {mod.__name__}::{name} -> {exc}")
                    failed += 1

    conftest.pytest_sessionfinish(None, 0 if failed == 0 else 1)
    print(
        f"\nSummary: {passed} passed, {failed} failed. "
        f"Generated EVAL_REPORT.md ({len(conftest.EVAL_RESULTS)} rows) and traces/sample_timeout_recovery_trace.jsonl."
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
