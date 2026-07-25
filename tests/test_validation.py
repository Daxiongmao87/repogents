from __future__ import annotations

import unittest

from repogents.validation import compare_findings, extract_findings


class ValidationFindingTests(unittest.TestCase):
    def test_normalizes_positions_and_preserves_duplicate_findings(self) -> None:
        baseline = """
/workspace/client/src/example.ts
  10:2  error  Unexpected any. Specify a different type  @typescript-eslint/no-explicit-any
  20:9  error  Unexpected any. Specify a different type  @typescript-eslint/no-explicit-any
  30:4  warning  Avoid stale state  react-hooks/exhaustive-deps

✖ 3 problems (2 errors, 1 warning)
"""
        candidate = """
/workspace/client/src/example.ts
  110:22  error  Unexpected any. Specify a different type  @typescript-eslint/no-explicit-any
  220:19  error  Unexpected any. Specify a different type  @typescript-eslint/no-explicit-any
  330:14  warning  Avoid stale state  react-hooks/exhaustive-deps

✖ 3 problems (2 errors, 1 warning)
"""

        expected = (
            "client/src/example.ts|error|@typescript-eslint/no-explicit-any|Unexpected any. Specify a different type",
            "client/src/example.ts|error|@typescript-eslint/no-explicit-any|Unexpected any. Specify a different type",
            "client/src/example.ts|warning|react-hooks/exhaustive-deps|Avoid stale state",
        )
        self.assertEqual(extract_findings(baseline, ""), expected)
        self.assertEqual(extract_findings(candidate, ""), expected)

    def test_extracts_test_failure_identity_without_timing_noise(self) -> None:
        first = """
FAIL: test_value (test_value.ValueTest.test_value)
----------------------------------------------------------------------
AssertionError: 1 != 2
Ran 1 test in 0.013s
FAILED (failures=1)
"""
        second = first.replace("0.013s", "1.901s")

        self.assertEqual(extract_findings(first, ""), extract_findings(second, ""))
        self.assertTrue(extract_findings(first, ""))
        self.assertEqual(extract_findings("command failed", ""), ())


class ValidationDeltaTests(unittest.TestCase):
    def test_accepts_equal_or_reduced_debt_and_rejects_any_new_finding(self) -> None:
        baseline = tuple(f"finding-{index:02d}" for index in range(41))

        equal = compare_findings(baseline, baseline)
        reduced = compare_findings(baseline, baseline[:35])
        lower_count_with_new = compare_findings(
            baseline,
            (*baseline[:36], "new-finding-a", "new-finding-b"),
        )

        self.assertTrue(equal.passed)
        self.assertEqual((len(equal.new), len(equal.resolved), len(equal.unchanged)), (0, 0, 41))
        self.assertTrue(reduced.passed)
        self.assertEqual((len(reduced.new), len(reduced.resolved), len(reduced.unchanged)), (0, 6, 35))
        self.assertFalse(lower_count_with_new.passed)
        self.assertEqual(
            (len(lower_count_with_new.new), len(lower_count_with_new.resolved), len(lower_count_with_new.unchanged)),
            (2, 5, 36),
        )


if __name__ == "__main__":
    unittest.main()
