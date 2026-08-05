from __future__ import annotations

import os
import subprocess
import time
import uuid
import shutil
import logging
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import FileSizeCalculator
from decider import Decider

logger = logging.getLogger("EnvManager.PodmanHybrid")


class HybridAttachManager(EnvironmentManager):
    def __init__(self,
                 container_name: str,
                 export_dir: str = "/tmp/statefork_podman",
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a hybrid environment using Podman with CRIU by attaching to a running container.

        :param container_name: Name of the existing container to attach to.
            Example: "statefork_hybrid"
        :param export_dir: Directory for storing CRIU export files (.tar.zstd).
            Example: "/tmp/statefork_podman"
        """
        super().__init__(backend_name="Podman+CRIU", decider=decider)
        self.container_name = container_name
        self.export_dir = export_dir
        os.makedirs(self.export_dir, exist_ok=True)

        logger.info(f"Initializing HybridAttachManager with container '{self.container_name}'")

        # Ensure container is running
        self.__ensure_container_running()

        # Take initial snapshot
        sid, _ = self._core_snapshot(self.current_branch_id)
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init the Tree Graph
        self._record_snapshot_node(SnapshotNode(snapshot_id=sid, parent_id=None))
        self.current_snapshot_id = sid
        self.last_snapshot_id = sid

        # Attach the FileSizeCalculator to the export directory
        self._stats.attach_size_calculator(FileSizeCalculator(self.export_dir))


    def __ensure_container_running(self):
        result = subprocess.run(["podman", "ps", "-q", "-f", f"name={self.container_name}"], capture_output=True, text=True)
        if not result.stdout.strip():
            raise RuntimeError(f"Container '{self.container_name}' is not running. Please start it before using this manager.")
        logger.debug(f"Pass validation: Container '{self.container_name}' is running.")

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        sid = str(uuid.uuid4())[:8]
        export_path = os.path.join(self.export_dir, f"{sid}.tar.zstd")

        start = time.time()
        subprocess.run([
            "podman", "container", "checkpoint", self.container_name,
            "-e", export_path, "--leave-running"
        ], stdout=subprocess.DEVNULL, check=True)
        elapsed = time.time() - start

        self._register_snapshot_resource(sid, export_path)

        return sid, elapsed

    def _core_restore(self, snapshot_id: str, branch_id: str) -> tuple[bool, float]:
        export_path = self._snapshot_resource(snapshot_id)
        if not export_path:
            logger.warning(f"Snapshot ID {snapshot_id} not found.")
            return False, 0.0

        # Stop & remove existing container if running
        subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        start = time.time()
        subprocess.run([
            "podman", "container", "restore",
            "-i", export_path,
            "-n", self.container_name
        ], stdout=subprocess.DEVNULL, check=True)
        elapsed = time.time() - start

        return True, elapsed

    def _core_cleanup(self):
        logger.info(f"Cleaning up Podman container '{self.container_name}'")
        subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Cleaning up Podman checkpoint files in {self.export_dir}")
        shutil.rmtree(self.export_dir, ignore_errors=True)

    def _core_exec(self, command, timeout=None, branch_id: str = "main"):
        if isinstance(command, list):
            cmd = ["podman", "exec", self.container_name] + command
        else:
            cmd = ["podman", "exec", self.container_name, "bash", "-c", command]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return result.returncode, result.stdout, result.stderr

class HybridBuildManager(HybridAttachManager):
    def __init__(self,
                 container_name: str = "podman-build",
                 dockerfile_dir: str = ".",
                 export_dir: str = "/tmp/statefork_podman",
                 extra_args: Optional[List[str]] = None,
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a hybrid environment using Podman with CRIU by building from Dockerfile.

        :param container_name: Name to assign to the container.
            Example: "statefork_hybrid"
        :param dockerfile_dir: Path to the directory containing the Dockerfile.
            Example: "/home/user/projects/myapp/"
        :param export_dir: Directory for storing CRIU export files (.tar.zstd).
            Example: "/tmp/statefork_podman"
        :param extra_args: Additional command-line args passed during container startup.
            Example: ["-p", "8000:8000", "-v", "/tmp:/tmp"]
        """
        image_name = "init_image"
        if extra_args is None:
            extra_args = ["-p", "8000:8000", "-v", "/tmp:/tmp"]

        logger.info(f"Building Podman image from directory '{dockerfile_dir}'...")
        subprocess.run(["podman", "build", "-t", image_name, dockerfile_dir], stdout=subprocess.DEVNULL, check=True)

        logger.info(f"Launching container '{container_name}' from image '{image_name}'...")
        cmd = ["podman", "run", "-d", "--rm", "--name", container_name] + extra_args + [image_name]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)

        time.sleep(2)  # wait for app to initialize

        super().__init__(container_name=container_name, export_dir=export_dir, decider=decider)
