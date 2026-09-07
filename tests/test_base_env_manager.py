from __future__ import annotations

import inspect
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from controller.base_env_manager import (
    DEFAULT_BRANCH_ID,
    EnvironmentManager,
    SnapshotNode,
)


class MemoryEnvironmentManager(EnvironmentManager):
    def __init__(self) -> None:
        super().__init__(backend_name="Memory")
        self._next_snapshot = 0
        self.restore_calls: list[tuple[str, str]] = []
        self.restore_delay = 0.0
        self.restore_started = threading.Event()
        self.operation_trace: list[str] = []

        self._register_snapshot_resource("root", "root")
        self._record_snapshot_node(SnapshotNode("root", None))
        self._set_branch_position(DEFAULT_BRANCH_ID, "root")

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        self._next_snapshot += 1
        snapshot_id = f"s{self._next_snapshot}"
        self._register_snapshot_resource(snapshot_id, snapshot_id)
        return snapshot_id, 0.01

    def _core_restore(self, snapshot_id: str, branch_id: str) -> tuple[bool, float]:
        self.restore_calls.append((snapshot_id, branch_id))
        self.operation_trace.append("restore-start")
        self.restore_started.set()
        time.sleep(self.restore_delay)
        self.operation_trace.append("restore-end")
        return True, 0.02

    def _core_cleanup(self) -> None:
        return None

    def _core_exec(
        self,
        command: List[str] | str,
        timeout: Optional[float],
        branch_id: str,
    ) -> tuple[int, str, str]:
        self.operation_trace.append("exec")
        return 0, str(command), ""


class EnvironmentManagerTests(unittest.TestCase):
    def test_contract_removes_legacy_and_branch_management_methods(self) -> None:
        manager = MemoryEnvironmentManager()

        self.assertTrue(hasattr(manager, "restore"))
        self.assertFalse(hasattr(manager, "create_env_from_snapshot"))
        self.assertFalse(hasattr(manager, "current_branch_id"))
        self.assertFalse(hasattr(manager, "fork"))
        self.assertEqual(len(inspect.signature(manager.snapshot).parameters), 0)

    def test_sequential_manager_uses_one_default_branch(self) -> None:
        manager = MemoryEnvironmentManager()

        snapshot_id = manager.snapshot()
        self.assertEqual(snapshot_id, "s1")
        self.assertEqual(manager._get_current_branch_id(), DEFAULT_BRANCH_ID)
        self.assertEqual(manager.current_snapshot, "s1")
        self.assertEqual(manager.snapshot_graph["s1"].parent_id, "root")

        self.assertTrue(manager.restore("root"))
        self.assertEqual(manager.restore_calls, [("root", DEFAULT_BRANCH_ID)])
        self.assertEqual(manager.current_snapshot, "root")

    def test_current_branch_operations_wait_for_restore_transition(self) -> None:
        manager = MemoryEnvironmentManager()
        manager.restore_delay = 0.04

        with ThreadPoolExecutor(max_workers=2) as pool:
            restore = pool.submit(manager.restore, "root")
            self.assertTrue(manager.restore_started.wait(timeout=1))
            execute = pool.submit(manager.exec_command, "work")
            self.assertTrue(restore.result())
            self.assertEqual(execute.result()[0], 0)

        self.assertEqual(
            manager.operation_trace,
            ["restore-start", "restore-end", "exec"],
        )

    def test_concurrent_snapshots_form_one_consistent_lineage(self) -> None:
        manager = MemoryEnvironmentManager()

        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshot_ids = list(pool.map(lambda _: manager.snapshot(), range(12)))

        self.assertEqual(set(snapshot_ids), {f"s{index}" for index in range(1, 13)})
        self.assertEqual(manager.snapshot_graph["s1"].parent_id, "root")
        for index in range(2, 13):
            self.assertEqual(
                manager.snapshot_graph[f"s{index}"].parent_id,
                f"s{index - 1}",
            )
        self.assertEqual(manager.current_snapshot, "s12")


if __name__ == "__main__":
    unittest.main()
