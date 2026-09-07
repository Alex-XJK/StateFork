from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import controller
from controller.base_env_manager import DEFAULT_BRANCH_ID, SnapshotNode
from controller.forkable_env_manager import (
    EnvironmentBranch,
    ForkableEnvironmentManager,
)
from decider import AlwaysFalseDecider


class MemoryForkableManager(ForkableEnvironmentManager[EnvironmentBranch]):
    def __init__(self, decider=None) -> None:
        super().__init__(backend_name="ForkableMemory", decider=decider)
        self._next_snapshot = 0
        self._next_branch = 0
        self._counter_lock = threading.Lock()
        self.active_execs = 0
        self.max_active_execs = 0
        self.exec_delay = 0.03
        self.fail_fork = False
        self.fail_copy = False
        self.backend_events: list[str] = []

        self._register_snapshot_resource("root", "root")
        self._record_snapshot_node(SnapshotNode("root", None))
        self._set_branch_position(DEFAULT_BRANCH_ID, "root")
        self._live_branches[DEFAULT_BRANCH_ID] = EnvironmentBranch(
            id=DEFAULT_BRANCH_ID,
            base_snapshot_id="root",
        )

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        self._next_snapshot += 1
        snapshot_id = f"s{self._next_snapshot}"
        self._register_snapshot_resource(snapshot_id, snapshot_id)
        return snapshot_id, 0.01

    def _core_cleanup(self) -> None:
        return None

    def _core_exec(
        self,
        command: List[str] | str,
        timeout: Optional[float],
        branch_id: str,
    ) -> tuple[int, str, str]:
        with self._counter_lock:
            self.active_execs += 1
            self.max_active_execs = max(self.max_active_execs, self.active_execs)
        time.sleep(self.exec_delay)
        with self._counter_lock:
            self.active_execs -= 1
        return 0, branch_id, ""

    def _core_fork(
        self,
        snapshot_id: str,
        requested_ids: List[Optional[str]],
    ) -> List[tuple[EnvironmentBranch, float]]:
        self.backend_events.append(f"fork:{snapshot_id}")
        if self.fail_fork:
            return []

        branches = []
        for requested_id in requested_ids:
            self._next_branch += 1
            branch_id = requested_id or f"branch-{self._next_branch}"
            branches.append(
                (
                    EnvironmentBranch(
                        id=branch_id,
                        base_snapshot_id=snapshot_id,
                    ),
                    0.02,
                )
            )
        return branches

    def _core_copy(
        self,
        branch_id: str,
        host_path: str,
        env_path: str,
        into: bool,
    ) -> tuple[bool, str]:
        with self._counter_lock:
            self.active_execs += 1
            self.max_active_execs = max(self.max_active_execs, self.active_execs)
        time.sleep(self.exec_delay)
        with self._counter_lock:
            self.active_execs -= 1
        direction = "in" if into else "out"
        self.backend_events.append(f"copy-{direction}:{branch_id}:{host_path}:{env_path}")
        if self.fail_copy:
            return False, "backend refused"
        return True, ""

    def _core_discard_branch(self, branch_id: str) -> bool:
        self.backend_events.append(f"discard:{branch_id}")
        return True

    def _core_park_branch(self, branch_id: str) -> tuple[Optional[str], float]:
        self._next_snapshot += 1
        snapshot_id = f"park-{self._next_snapshot}"
        self._register_snapshot_resource(snapshot_id, snapshot_id)
        return snapshot_id, 0.01

    def _core_list_branches(self) -> Optional[List[EnvironmentBranch]]:
        return self.live_branches


class RegistryRefreshingMemoryManager(MemoryForkableManager):
    """Models a backend that refreshes its registry after snapshotting."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_started = threading.Event()
        self.discard_holds_registry = threading.Event()

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        self.snapshot_started.set()
        self.discard_holds_registry.wait(timeout=0.05)
        self.list_branches()
        return super()._core_snapshot(branch_id)


class SignalingLock:
    """Signal when one named thread acquires an underlying reentrant lock."""

    def __init__(self, lock, event: threading.Event, thread_name: str) -> None:
        self._lock = lock
        self._event = event
        self._thread_name = thread_name

    def __enter__(self):
        self._lock.acquire()
        if threading.current_thread().name == self._thread_name:
            self._event.set()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._lock.release()


class ForkableEnvironmentManagerTests(unittest.TestCase):
    def test_capability_is_exported(self) -> None:
        self.assertIs(controller.EnvironmentBranch, EnvironmentBranch)
        self.assertIs(controller.ForkableEnvironmentManager, ForkableEnvironmentManager)

    def test_virtual_snapshot_deciders_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "physical snapshots only"):
            MemoryForkableManager(decider=AlwaysFalseDecider())

    def test_templates_register_and_advance_named_branches(self) -> None:
        manager = MemoryForkableManager()
        branches = manager.fork("root", ids=["left", "right"])

        self.assertEqual([branch.id for branch in branches], ["left", "right"])
        left_snapshot = manager.snapshot_branch("left")
        self.assertEqual(left_snapshot, "s1")
        self.assertEqual(manager._get_branch_state("left").current_snapshot_id, "s1")
        self.assertEqual(manager.snapshot_graph["s1"].parent_id, "root")

        entries = manager.stats.log
        self.assertEqual(
            [entry.branch_id for entry in entries[:3]],
            ["left", "right", "left"],
        )

    def test_named_fork_cannot_replace_an_existing_or_default_branch(self) -> None:
        manager = MemoryForkableManager()
        manager.fork("root", ids=["left"])
        manager.backend_events.clear()

        self.assertEqual(manager.fork("root", ids=["left"]), [])
        self.assertEqual(manager.fork("root", ids=[DEFAULT_BRANCH_ID]), [])
        self.assertEqual(manager.backend_events, [])

    def test_different_branches_execute_concurrently(self) -> None:
        manager = MemoryForkableManager()
        manager.fork("root", ids=["left", "right"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda branch: manager.exec_on_branch(branch, "work"),
                    ["left", "right"],
                )
            )

        self.assertEqual([result[0] for result in results], [0, 0])
        self.assertEqual(manager.max_active_execs, 2)

    def test_same_branch_operations_are_serialized(self) -> None:
        manager = MemoryForkableManager()
        manager.fork("root", ids=["left"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: manager.exec_on_branch("left", "work"), range(2)))

        self.assertEqual(manager.max_active_execs, 1)

    def test_named_snapshot_and_discard_use_consistent_lock_order(self) -> None:
        manager = RegistryRefreshingMemoryManager()
        manager.fork("root", ids=["left"])
        manager._branch_registry_lock = SignalingLock(
            manager._branch_registry_lock,
            manager.discard_holds_registry,
            "discard-left",
        )
        results = {}

        snapshot_thread = threading.Thread(
            target=lambda: results.setdefault(
                "snapshot", manager.snapshot_branch("left")
            ),
            daemon=True,
        )
        snapshot_thread.start()
        self.assertTrue(manager.snapshot_started.wait(timeout=1))

        discard_thread = threading.Thread(
            target=lambda: results.setdefault(
                "discard", manager.discard_branch("left")
            ),
            name="discard-left",
            daemon=True,
        )
        discard_thread.start()

        snapshot_thread.join(timeout=1)
        discard_thread.join(timeout=1)

        self.assertFalse(snapshot_thread.is_alive(), "snapshot deadlocked")
        self.assertFalse(discard_thread.is_alive(), "discard deadlocked")
        self.assertEqual(results, {"snapshot": "s1", "discard": True})

    def test_restore_switches_only_after_materialization(self) -> None:
        manager = MemoryForkableManager()
        manager.fork("root", ids=["left"])
        manager._set_current_branch("left")
        manager.backend_events.clear()

        self.assertTrue(manager.restore("root"))

        restored_id = manager.current_branch_id
        self.assertNotIn(restored_id, (DEFAULT_BRANCH_ID, "left"))
        self.assertNotIn("left", [branch.id for branch in manager.live_branches])
        self.assertEqual(
            manager.backend_events,
            ["fork:root", "discard:left"],
        )
        self.assertTrue(manager._get_branch_state(restored_id).active)
        self.assertEqual(manager.current_snapshot, "root")

    def test_failed_restore_preserves_the_current_branch(self) -> None:
        manager = MemoryForkableManager()
        manager.fork("root", ids=["left"])
        manager._set_current_branch("left")
        manager.fail_fork = True
        manager.backend_events.clear()

        self.assertFalse(manager.restore("root"))
        self.assertEqual(manager.current_branch_id, "left")
        self.assertIn("left", [branch.id for branch in manager.live_branches])
        self.assertEqual(manager.backend_events, ["fork:root"])

    def test_parking_current_branch_returns_to_default(self) -> None:
        manager = MemoryForkableManager()
        branch = manager.fork("root", ids=["left"])[0]
        manager._set_current_branch(branch.id)

        snapshot_id = manager.park_branch(branch.id)

        self.assertEqual(snapshot_id, "park-1")
        self.assertEqual(manager.current_branch_id, DEFAULT_BRANCH_ID)
        self.assertNotIn(branch.id, [item.id for item in manager.live_branches])


if __name__ == "__main__":
    unittest.main()


class CopyVerbTests(unittest.TestCase):
    """copy_in/copy_out and their branch-addressed variants."""

    def setUp(self) -> None:
        self.manager = MemoryForkableManager()

    def test_copy_in_and_out_target_the_current_branch(self) -> None:
        self.assertTrue(self.manager.copy_in("/host/a.txt", "/env/a.txt"))
        self.assertTrue(self.manager.copy_out("/env/b.txt", "/host/b.txt"))
        self.assertIn(
            f"copy-in:{DEFAULT_BRANCH_ID}:/host/a.txt:/env/a.txt",
            self.manager.backend_events,
        )
        self.assertIn(
            f"copy-out:{DEFAULT_BRANCH_ID}:/host/b.txt:/env/b.txt",
            self.manager.backend_events,
        )

    def test_branch_variants_address_the_named_branch(self) -> None:
        (branch,) = self.manager.fork("root", n=1)
        self.assertTrue(
            self.manager.copy_in_branch(branch.id, "/host/x", "/env/x")
        )
        self.assertTrue(
            self.manager.copy_out_branch(branch.id, "/env/y", "/host/y")
        )
        self.assertIn(
            f"copy-in:{branch.id}:/host/x:/env/x", self.manager.backend_events
        )
        self.assertIn(
            f"copy-out:{branch.id}:/host/y:/env/y", self.manager.backend_events
        )

    def test_unknown_branch_is_reported_not_raised(self) -> None:
        with self.assertLogs("EnvManager", level="ERROR"):
            self.assertFalse(self.manager.copy_in_branch("ghost", "/h", "/e"))
        self.assertFalse(
            any(e.startswith("copy-in:ghost") for e in self.manager.backend_events)
        )

    def test_backend_failure_is_reported_as_false(self) -> None:
        self.manager.fail_copy = True
        with self.assertLogs("EnvManager", level="ERROR"):
            self.assertFalse(self.manager.copy_in("/host/a", "/env/a"))

    def test_copy_is_not_recorded_as_a_replayable_command(self) -> None:
        """A copy mutates the filesystem outside the command log; recording it
        would make a virtual snapshot believe it could be replayed."""
        self.manager.copy_in("/host/a", "/env/a")
        state = self.manager._get_branch_state(DEFAULT_BRANCH_ID)
        self.assertEqual(state.command_log, [])

    def test_copy_serializes_with_exec_on_the_same_branch(self) -> None:
        """One branch's operation lock must cover copy as well as exec, so a
        transfer cannot interleave with a command on that branch."""
        self.manager.exec_delay = 0.05
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.manager.copy_in, "/host/a", "/env/a"),
                pool.submit(self.manager.exec_command, "echo hi"),
            ]
            for future in futures:
                future.result()
        self.assertEqual(self.manager.max_active_execs, 1)

    def test_copies_on_different_branches_run_concurrently(self) -> None:
        branches = self.manager.fork("root", n=2)
        self.manager.exec_delay = 0.05
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.manager.copy_in_branch, b.id, "/host/a", "/env/a")
                for b in branches
            ]
            for future in futures:
                self.assertTrue(future.result())
        self.assertEqual(self.manager.max_active_execs, 2)
