from __future__ import annotations

import os
import psutil
import shutil
import signal
import subprocess
import time
import uuid
import logging
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import FileSizeCalculator
from decider import Decider

logger = logging.getLogger("EnvManager.CRIU")


class CRIUAttachManager(EnvironmentManager):
    """
    CRIUAttachManager is a specialized CRIU EnvironmentManager that attaches to an existing process.
    """
    def __init__(self,
                 target_pid: int,
                 work_dir: str = "/tmp/statefork_criu",
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a CRIU-based environment by attaching to an existing running process.

        :param target_pid: PID of the target process to attach to.
            Example: 12345
        :param work_dir: Directory for storing checkpoint images and related files.
            Example: "/tmp/statefork_criu"
        """
        super().__init__(backend_name="CRIU", decider=decider)
        self.work_dir = work_dir
        os.makedirs(self.work_dir, exist_ok=True)

        self.process = None
        if not psutil.pid_exists(target_pid):
            raise ValueError(f"Target PID {target_pid} does not exist or is not running.")
        self.app_pid = target_pid
        logger.info(f"Attaching to existing APP process with PID {self.app_pid}...")

        sid, _ = self._core_snapshot(self._get_current_branch_id())
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init the Tree Graph
        self._record_snapshot_node(SnapshotNode(snapshot_id=sid, parent_id=None))
        self.current_snapshot_id = sid
        self.last_snapshot_id = sid

        # Attach the FileSizeCalculator to the work directory
        self._stats.attach_size_calculator(FileSizeCalculator(self.work_dir))


    # Benchmarking Notes: This method causes a delay of {soft_timeout + hard_timeout} seconds!!!
    # TODO: Any more efficient way to kill the original process?
    def __kill_original_process(self, force: bool = False, soft_timeout: float = 0.1, hard_timeout: float = 0.1):
        if force:
            proc = psutil.Process(self.app_pid)
            proc.kill()
            proc.wait(timeout=hard_timeout)
            return

        try:
            proc = psutil.Process(self.app_pid)
            proc.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            return

        start_time = time.time()
        while proc.is_running():
            if time.time() - start_time > soft_timeout:
                logger.warning(f"SIGTERM not responding within {soft_timeout}s, sending SIGKILL to PID {self.app_pid}")
                proc.kill()
                proc.wait(timeout=hard_timeout)
                return

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        snapshot_id = str(uuid.uuid4())[:8]
        snapshot_path = os.path.join(self.work_dir, snapshot_id)
        os.makedirs(snapshot_path, exist_ok=True)

        start = time.time()
        try:
            subprocess.run([
                "criu", "dump",
                "-t", str(self.app_pid),
                "--images-dir", snapshot_path,
                "--tcp-established",
                "--shell-job",
                "--leave-running"
            ], check=True)
            elapsed = time.time() - start
            self._register_snapshot_resource(snapshot_id, snapshot_path)
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"CRIU snapshot failed: {e}")
            return None, 0.0

    def _core_restore(self, snapshot_id: str, branch_id: str) -> tuple[bool, float]:
        snapshot_path = self._snapshot_resource(snapshot_id)
        if not snapshot_path:
            logger.warning(f"Snapshot {snapshot_id} not found.")
            return False, 0.0

        # Terminate the existing APP process
        self.__kill_original_process(force=True, hard_timeout=0.01)

        start = time.time()
        try:
            self.process = subprocess.Popen([
                "criu", "restore",
                "--images-dir", snapshot_path,
                "--tcp-established",
                "--shell-job"
            ])
            elapsed = time.time() - start
            return True, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"CRIU restore failed: {e}")
            return False, 0.0

    def _core_cleanup(self):
        logger.info("Shutting down CRIU environment...")
        self.__kill_original_process(soft_timeout=2.0, hard_timeout=2.0)
        logger.info(f"Removing work directory {self.work_dir}...")
        shutil.rmtree(self.work_dir, ignore_errors=True)


class CRIUBuildManager(CRIUAttachManager):
    """
    CRIUBuildManager is a specialized CRIU EnvironmentManager that launches a new APP process.
    """
    def __init__(self,
                 work_dir: str = "/tmp/statefork_criu",
                 command: Optional[List[str]] = None,
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a CRIU-based environment by launching and managing a new local process.

        :param work_dir: Directory for storing checkpoint images and related files.
            Example: "/tmp/statefork_criu"
        :param command: Command to launch the target application process.
            Example: ["python3", "app.py"]
        """
        if command is None:
            command = [
                "uvicorn", "app.api_server:app",
                "--host", "127.0.0.1",
                "--port", "8000",
                "--no-access-log"
            ]

        logger.info("Starting initial APP...")
        process = self.__start_app(command)
        if process.poll() is not None:
            if process.stderr:
                logger.error("Process stderr output:")
                print(process.stderr.read().decode())
            raise RuntimeError("Failed to start the APP.")

        time.sleep(2)  # wait for app to initialize
        super().__init__(target_pid=process.pid, work_dir=work_dir, decider=decider)
        self.process = process

    @staticmethod
    def __start_app(command: List[str]) -> subprocess.Popen:
        return subprocess.Popen(command, stderr=subprocess.PIPE)
