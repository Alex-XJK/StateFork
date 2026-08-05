from __future__ import annotations

import unittest

from controller.base_env_manager import EnvironmentManager
from controller.forkable_env_manager import ForkableEnvironmentManager
from controller.waypoint_env_manager import WaypointAttachManager, WaypointFork


class WaypointManagerArchitectureTests(unittest.TestCase):
    def test_waypoint_reuses_public_template_methods(self) -> None:
        self.assertIs(WaypointAttachManager.snapshot, EnvironmentManager.snapshot)
        self.assertIs(WaypointAttachManager.restore, EnvironmentManager.restore)
        self.assertIs(WaypointAttachManager.exec_command, EnvironmentManager.exec_command)
        self.assertIs(WaypointAttachManager.fork, ForkableEnvironmentManager.fork)

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
