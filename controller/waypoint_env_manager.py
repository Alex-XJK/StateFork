from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
import logging
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import Calculator
from decider import Decider

logger = logging.getLogger("EnvManager.Waypoint")

STATEFORK_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _resolve_bin(name: str, env_var: str) -> str:
    """Locate a Waypoint helper binary.

    Resolution order: an explicit ``env_var`` override, then a binary of the
    same name found on ``PATH``, then a repo-local fallback at
    ``STATEFORK_ROOT/<name>`` (typically a developer-created symlink). The path
    is returned even if it does not exist; executability is validated lazily in
    ``_run_waypoint`` so that importing this module never requires Waypoint to
    be installed (other backends must stay usable without it).
    """
    return (
        os.environ.get(env_var)
        or shutil.which(name)
        or os.path.join(STATEFORK_ROOT, name)
    )


WAYPOINT_BIN = _resolve_bin("waypoint", "WAYPOINT_BIN")


def _run_waypoint(args: list[str], **kwargs):
    if not (os.path.isfile(WAYPOINT_BIN) and os.access(WAYPOINT_BIN, os.X_OK)):
        raise FileNotFoundError(
            f"Waypoint binary not found or not executable: {WAYPOINT_BIN}. "
            "Install Waypoint (https://github.com/Alex-XJK/waypoint) and either "
            "put it on PATH, set the WAYPOINT_BIN environment variable, or "
            f"symlink it into {STATEFORK_ROOT}."
        )
    # Waypoint needs root (buildah overlay mount, CRIU, overlay/chroot). Callers
    # (e.g. Harbor) request privilege escalation via WAYPOINT_CMD_PREFIX, e.g.
    # "sudo -n -E". Empty/unset => run the binary directly (unchanged behavior).
    prefix = shlex.split(os.environ.get("WAYPOINT_CMD_PREFIX", ""))
    return subprocess.run(
        [*prefix, WAYPOINT_BIN, *args],
        cwd=STATEFORK_ROOT,
        **kwargs,
    )


class WaypointCalculator(Calculator):
    """
    WaypointCalculator is a specialized FileSizeCalculator for Waypoint that
    collects the sizes of filesystem and memory checkpoint files in a session directory.

    We have to override but not extend the FileSizeCalculator because we need to
    target a specific subdirectory structure created by Waypoint v0.4.0 and do some filtering.
    """
    def __init__(self, root_dir: str, sub_dir: str, name: str = "WaypointFsCalculator"):
        super().__init__(name=name)
        self.root_dir = os.path.abspath(root_dir)
        self.sub_dir = sub_dir  # either "upper" or "criu"
        self.logger.debug(f"Attached WaypointCalculator #{self.instance_id} to {self.root_dir}/*/{self.sub_dir}")

    def __get_all_items(self) -> List[str]:
        if not os.path.exists(self.root_dir):
            return []
        items = []
        for name in os.listdir(self.root_dir):
            if name in ["metadata", "work", "temp"]:
                continue
            sub_path = os.path.join(self.root_dir, name, self.sub_dir)
            if os.path.exists(sub_path):
                items.append(sub_path)
        return items

    def __get_size(self, path: str) -> int:
        try:
            output = subprocess.check_output(["du", "-sb", path], text=True)
            return int(output.split()[0])
        except Exception as e:
            self.logger.error(f"Error getting size for {path}: {e}")
            return 0

    def _collect(self) -> List[tuple[str, int]]:
        items = self.__get_all_items()
        if not items:
            return []

        data = []
        for item in items:
            size = self.__get_size(item)
            if size >= 0:
                parts = os.path.normpath(item).split(os.sep)
                name = os.path.join(parts[-2], parts[-1])
                data.append((name, size))
        return data

class WaypointAttachManager(EnvironmentManager):
    """
    WaypointAttachManager is a specialized Waypoint EnvironmentManager that attaches to an existing session.
    """
    PID_NOT_PROVIDED = -2

    def __init__(self,
                 session_id: str,
                 target_pid: int = PID_NOT_PROVIDED,
                 decider: Optional[Decider] = None,
                 ):
        super().__init__(backend_name="Waypoint", decider=decider)
        self.session_id = session_id
        self.target_pid = target_pid

        logger.info(f"Attaching to existing Waypoint session {self.session_id} with target PID {self.target_pid}...")

        sid, _ = self._core_snapshot()
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init the Tree Graph
        self.snapshot_graph[sid] = SnapshotNode(snapshot_id=sid, parent_id=None)
        self.current_snapshot_id = sid
        self.last_snapshot_id = sid


    def _core_snapshot(self) -> tuple[Optional[str], float]:
        snapshot_id = str(uuid.uuid4())[:8]

        start = time.time()
        try:
            _run_waypoint(
                ["create", self.session_id, snapshot_id, str(self.target_pid)],
                check=True,
            )
            elapsed = time.time() - start
            self.snapshots[snapshot_id] = snapshot_id
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint snapshot failed: {e}")
            return None, 0.0

    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        snapshot_id = self.snapshots.get(snapshot_id)
        if not snapshot_id:
            logger.warning(f"Snapshot {snapshot_id} not found.")
            return None, 0.0

        start = time.time()
        try:
            _run_waypoint(
                ["restore", self.session_id, snapshot_id],
                check=True,
            )
            elapsed = time.time() - start
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint restore failed: {e}")
            return None, 0.0

    def _core_cleanup(self):
        logger.info("Shutting down Waypoint environment...")
        try:
            _run_waypoint(
                ["cleanup", self.session_id],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint cleanup failed: {e}")
            logger.info("Attempting force cleanup...")
            try:
                _run_waypoint(
                    ["cleanup", self.session_id, "--force"],
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Waypoint force cleanup failed: {e}")
                return

    def _core_exec(self, command: List[str] | str, timeout: Optional[float]) -> tuple[int, str, str]:
        if not self.session_id:
            return -1, "", "No session_id available"

        # Convert command into a sequence of arguments (waypoint expects args list)
        if isinstance(command, str):
            cmd_str = command
        else:
            cmd_str = shlex.join(command)

        # Execute `command` via `waypoint exec <session_id> <args...>`.
        try:
            proc = _run_waypoint(
                ["exec", self.session_id, cmd_str],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            err = (e.stderr or "") + f"\n[timeout after {timeout}s]"
            logger.error(f"Waypoint exec timeout: {e}")
            return -1, out, err
        except Exception as e:
            logger.error(f"Waypoint exec failed: {e}")
            return -1, "", str(e)

class WaypointBuildManager(WaypointAttachManager):
    """
    WaypointBuildManager is a specialized Waypoint EnvironmentManager that builds a new session.
    """
    def __init__(self,
                 dockerfile_dir: str = ".",
                 build: bool = True,
                 decider: Optional[Decider] = None,
                 ):
        if dockerfile_dir is None:
            target_dir = os.getcwd()
        else:
            target_dir = os.path.abspath(dockerfile_dir)

        logger.info("Creating a new Waypoint session...")
        if not build:
            init_process = _run_waypoint(
                ["init", target_dir, "--quiet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            output = init_process.stdout.strip()
            try:
                sid, self._work_dir = output.split(",", 1)
            except ValueError:
                raise RuntimeError(f"Unexpected output format: {output}")
        else:
            init_process = _run_waypoint(
                ["build", target_dir, "--quiet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            output = init_process.stdout.strip()
            try:
                sid, self._work_dir, _ = output.split(",", 2)
            except ValueError:
                raise RuntimeError(f"Unexpected output format: {output}")

        logger.info(f"New session {sid} with work directory '{self._work_dir}' created.")

        super().__init__(session_id=sid, decider=decider)

        # Attach the new WaypointCalculator to this session
        base_dir = os.path.join(self._work_dir, "../")
        self._stats.attach_size_calculator(WaypointCalculator(base_dir, "upper", name="FILESYSTEM"))
        self._stats.attach_size_calculator(WaypointCalculator(base_dir, "criu", name="MEMORY"))

    @property
    def work_dir(self) -> str:
        return self._work_dir
