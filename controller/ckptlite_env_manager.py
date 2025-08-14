import os
import subprocess
import time
import uuid
import logging
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode

logger = logging.getLogger("EnvManager.CkptLite")


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

        if command is None:
            logger.info(f"User skipped the APP launch.")
            super().__init__(target_pid=-1, session_id=sid)
            return

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
        time.sleep(2)  # wait for app to initialize

        super().__init__(target_pid=proc.pid, session_id=sid)

    @property
    def work_dir(self) -> str:
        return self._work_dir