from __future__ import annotations

import unittest
from unittest.mock import patch

from controller.base_env_manager import EnvironmentManager
from controller.container_env_manager import ContainerBuildManager
from controller.forkable_env_manager import ForkableEnvironmentManager
from controller.waypoint_env_manager import WaypointAttachManager, WaypointFork
import os
import types
from unittest import mock


class ManagerArchitectureTests(unittest.TestCase):
    @patch("controller.container_env_manager.subprocess.run")
    def test_sequential_backend_initialization_uses_protected_branch_id(
        self,
        _run,
    ) -> None:
        manager = ContainerBuildManager(backend="Docker", dockerfile_dir=".")
        manager.is_cleaned_up = True

        self.assertEqual(manager.current_snapshot, "base")

    def test_waypoint_reuses_public_template_methods(self) -> None:
        self.assertIs(WaypointAttachManager.snapshot, EnvironmentManager.snapshot)
        self.assertIs(WaypointAttachManager.restore, EnvironmentManager.restore)
        self.assertIs(WaypointAttachManager.exec_command, EnvironmentManager.exec_command)
        self.assertIs(WaypointAttachManager.fork, ForkableEnvironmentManager.fork)
        self.assertIs(
            WaypointAttachManager._core_restore,
            ForkableEnvironmentManager._core_restore,
        )

    def test_waypoint_fork_details_remain_compatible(self) -> None:
        legacy = WaypointFork(
            id="legacy",
            pid=42,
            socket="/tmp/legacy.sock",
            base_checkpoint="root",
        )
        self.assertEqual(legacy.base_snapshot_id, "root")
        self.assertEqual(legacy.base_checkpoint, "root")

        parsed = WaypointAttachManager._parse_fork_line(
            "child pid=7 socket=/tmp/child.sock duration=12ms",
            "root",
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.id, "child")
        self.assertEqual(parsed.pid, 7)
        self.assertEqual(parsed.base_checkpoint, "root")


if __name__ == "__main__":
    unittest.main()


class WaypointCopyPathTests(unittest.TestCase):
    """A relative host path must not be resolved inside the StateFork checkout.

    `_run_waypoint` runs the binary with cwd=STATEFORK_ROOT, so a caller's
    relative path (a job directory, say) would land there instead of where the
    caller meant -- and the copy would still report success, which is the worst
    shape for a bug like this.
    """

    def test_relative_host_path_is_absolutized_against_the_callers_cwd(self):
        from controller.waypoint_env_manager import WaypointAttachManager

        manager = WaypointAttachManager.__new__(WaypointAttachManager)
        manager.session_id = "sess"

        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch(
            "controller.waypoint_env_manager._run_waypoint", side_effect=fake_run
        ):
            ok, detail = manager._core_copy(
                branch_id="main",
                host_path="jobs/run1/verifier",
                env_path="/logs/verifier",
                into=False,
            )

        self.assertTrue(ok, detail)
        host_arg = seen["args"][-1]
        self.assertTrue(
            os.path.isabs(host_arg), f"host path was left relative: {host_arg}"
        )
        self.assertEqual(host_arg, os.path.abspath("jobs/run1/verifier"))
