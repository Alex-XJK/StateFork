import os
import shlex
import subprocess
import time
import uuid
import logging
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import Calculator

logger = logging.getLogger("EnvManager.CkptLite")

class CkptCalculator(Calculator):
    """
    CkptCalculator is a specialized FileSizeCalculator for Checkpoint-lite that
    collects the sizes of filesystem and memory checkpoint files in a session directory.

    We have to override but not extend the FileSizeCalculator because we need to
    target a specific subdirectory structure created by Checkpoint-lite v0.4.0 and do some filtering.
    """
    def __init__(self, root_dir: str, sub_dir: str, name: str = "CkptFsCalculator"):
        super().__init__(name=name)
        self.root_dir = os.path.abspath(root_dir)
        self.sub_dir = sub_dir  # either "upper" or "criu"
        self.logger.debug(f"Attached CkptCalculator #{self.instance_id} to {self.root_dir}/*/{self.sub_dir}")

    def __get_all_items(self) -> List[str]:
        if not os.path.exists(self.root_dir):
            return []
        items = []
        for name in os.listdir(self.root_dir):
            if name in ["metadata", "work"]:
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

class CheckpointLiteAttachManager(EnvironmentManager):
    """
    CheckpointLiteAttachManager is a specialized Checkpoint-lite EnvironmentManager that attaches to an existing session.
    """
    def __init__(self,
                 target_pid: int,
                 session_id: str
                 ):
        super().__init__(backend_name="Checkpoint-lite")
        self.session_id = session_id
        self.target_pid = target_pid

        logger.info(f"Attaching to existing Checkpoint-lite session {self.session_id} with target PID {self.target_pid}...")

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
            subprocess.run(
                ["./checkpoint-lite", "create", self.session_id, str(self.target_pid), snapshot_id],
                check=True
            )
            elapsed = time.time() - start
            self.snapshots[snapshot_id] = snapshot_id
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"CheckpointLite snapshot failed: {e}")
            return None, 0.0

    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        snapshot_id = self.snapshots.get(snapshot_id)
        if not snapshot_id:
            logger.warning(f"Snapshot {snapshot_id} not found.")
            return None, 0.0

        start = time.time()
        try:
            subprocess.run(
                ["./checkpoint-lite", "restore", self.session_id, snapshot_id],
                check=True
            )
            elapsed = time.time() - start
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"CheckpointLite restore failed: {e}")
            return None, 0.0

    def _core_cleanup(self):
        logger.info("Shutting down CheckpointLite environment...")
        try:
            subprocess.run(
                ["./checkpoint-lite", "cleanup", self.session_id, "--force"],
                check=True
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"CheckpointLite force cleanup failed: {e}")
            return

    def _core_exec(self, command: List[str] | str, timeout: Optional[float]) -> tuple[int, str, str]:
        if not self.session_id:
            return -1, "", "No session_id available"

        # Convert command into a sequence of arguments (checkpoint-lite expects args list)
        if isinstance(command, str):
            cmd_str = command
        else:
            cmd_str = shlex.join(command)

        # Execute `command` via `./checkpoint-lite exec <session_id> <args...>`.
        exec_args = ["./checkpoint-lite", "exec", self.session_id, "sh", "-c", cmd_str]
        try:
            proc = subprocess.run(
                exec_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            err = (e.stderr or "") + f"\n[timeout after {timeout}s]"
            logger.error(f"CheckpointLite exec timeout: {e}")
            return -1, out, err
        except Exception as e:
            logger.error(f"CheckpointLite exec failed: {e}")
            return -1, "", str(e)

class CheckpointLiteBuildManager(CheckpointLiteAttachManager):
    """
    CheckpointLiteBuildManager is a specialized Checkpoint-lite EnvironmentManager that builds a new session.
    """
    def __init__(self,
                 init_dir: Optional[str] = None,
                 command: Optional[List[str] | str] = "default"
                 ):
        if init_dir is None:
            target_dir = os.getcwd()
        else:
            target_dir = os.path.abspath(init_dir)

        logger.info("Creating a new Checkpoint-lite session...")
        init_process = subprocess.run(
            ["./checkpoint-lite", "init", target_dir, "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )

        output = init_process.stdout.strip()
        try:
            sid, self._work_dir = output.split(",", 1)
        except ValueError:
            raise RuntimeError(f"Unexpected output format: {output}")

        logger.info(f"New session {sid} with work directory '{self._work_dir}' created.")

        proc_pid = -1
        if command is None:
            logger.info(f"User skipped the APP launch.")
        else:
            if command == "default":
                command = [
                    "uvicorn", "app.api_server:app",
                    "--host", "127.0.0.1",
                    "--port", "8000",
                    "--no-access-log"
                ]
            elif isinstance(command, str):
                command = command.split()

            logger.info(f"Starting initial APP...")
            proc = subprocess.Popen(
                command,
                cwd=self._work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            time.sleep(5)  # wait for app to initialize
            proc_pid = proc.pid

        super().__init__(target_pid=proc_pid, session_id=sid)

        # Attach the new CkptCalculator to this session
        base_dir = os.path.join(self._work_dir, "../")
        self._stats.attach_size_calculator(CkptCalculator(base_dir, "upper", name="FILESYSTEM"))
        self._stats.attach_size_calculator(CkptCalculator(base_dir, "criu", name="MEMORY"))

    @property
    def work_dir(self) -> str:
        return self._work_dir