from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from controller.benchmark import BenchmarkStats


class BenchmarkStatsTests(unittest.TestCase):
    def test_sequence_and_branch_records_are_thread_safe(self) -> None:
        stats = BenchmarkStats()

        def add_entries(branch_id: str) -> None:
            for index in range(50):
                stats.add_entry("exec", str(index), 0.001, branch_id=branch_id)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add_entries, ["a", "b", "c", "d"]))

        self.assertEqual(len(stats.log), 200)
        self.assertEqual([entry.sequence for entry in stats.log], list(range(1, 201)))
        self.assertEqual(
            {entry.branch_id for entry in stats.log},
            {"a", "b", "c", "d"},
        )


if __name__ == "__main__":
    unittest.main()
