from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from controller.waypoint_env_manager import WaypointCalculator


def du_plain(path: str) -> int:
    """The pre-fix measurement: ``du -sb`` of the path as given."""
    return int(subprocess.check_output(["du", "-sb", path], text=True).split()[0])


def write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)


class WaypointCalculatorTests(unittest.TestCase):
    """The size calculator must measure a real ``criu``/``upper`` directory
    exactly as before, and measure *through* the ``criu`` symlink that
    Waypoint's ``tmpfs_images`` mode creates, without following symlinks that
    live inside a layer (those may point anywhere on the host)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.checkpoints = os.path.join(self.root, "checkpoints")
        os.makedirs(self.checkpoints)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def sizes(self, sub_dir: str) -> dict[str, int]:
        return dict(WaypointCalculator(self.checkpoints, sub_dir)._collect())

    def test_real_directory_is_measured_exactly_as_before(self) -> None:
        write(os.path.join(self.checkpoints, "ck1", "criu", "pages-1.img"), 100_000)
        write(os.path.join(self.checkpoints, "ck1", "criu", "dump.log"), 2_000)
        self.assertEqual(
            self.sizes("criu"),
            {"ck1/criu": du_plain(os.path.join(self.checkpoints, "ck1", "criu"))},
        )

    def test_tmpfs_images_symlink_is_measured_through_the_link(self) -> None:
        # tmpfs_images: checkpoints/<id>/criu -> <tmpfs dir>, later -> criu.disk
        target = os.path.join(self.root, "shm", "ck2")
        write(os.path.join(target, "pages-1.img"), 300_000)
        os.makedirs(os.path.join(self.checkpoints, "ck2"))
        os.symlink(target, os.path.join(self.checkpoints, "ck2", "criu"))

        self.assertEqual(self.sizes("criu"), {"ck2/criu": du_plain(target)})
        self.assertGreater(self.sizes("criu")["ck2/criu"], 300_000 - 1)

    def test_symlinks_inside_a_layer_are_not_followed(self) -> None:
        # An `upper` layer may contain symlinks left by the image or the
        # workload; their targets (possibly host files) must not be counted.
        huge = os.path.join(self.root, "host-file")
        write(huge, 900_000)
        upper = os.path.join(self.checkpoints, "ck3", "upper")
        write(os.path.join(upper, "small.txt"), 5_000)
        os.symlink(huge, os.path.join(upper, "link-to-host-file"))
        os.symlink("/definitely/missing", os.path.join(upper, "dangling"))

        measured = self.sizes("upper")["ck3/upper"]
        self.assertEqual(measured, du_plain(upper))
        self.assertLess(measured, 900_000)

    def test_dangling_criu_symlink_and_non_checkpoint_dirs_are_skipped(self) -> None:
        os.makedirs(os.path.join(self.checkpoints, "ck4"))
        os.symlink(os.path.join(self.root, "gone"), os.path.join(self.checkpoints, "ck4", "criu"))
        for name in ("metadata", "work", "temp"):
            write(os.path.join(self.checkpoints, name, "criu", "x.img"), 10)
        write(os.path.join(self.checkpoints, "ck5", "criu", "pages-1.img"), 10)

        self.assertEqual(set(self.sizes("criu")), {"ck5/criu"})


if __name__ == "__main__":
    unittest.main()
