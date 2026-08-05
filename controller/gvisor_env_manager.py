from __future__ import annotations

import os
import logging
import subprocess
import time
import uuid
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from decider import Decider
from .benchmark import FileSizeCalculator

logger = logging.getLogger("EnvManager.GVisor")

class GvisorCalculator(FileSizeCalculator):
    def __init__(self, container_name: str):
        try:
            docker_root = subprocess.check_output(
                ["docker", "info", "--format", "{{.DockerRootDir}}"],
                text=True
            ).strip()

            container_id = subprocess.check_output(
                ["docker", "inspect", container_name, "--format", "{{.Id}}"],
                text=True
            ).strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get Docker info for GvisorCalculator: {e}")
            raise RuntimeError("Failed to initialize GvisorCalculator due to Docker command failure.")

        root_dir = os.path.join(docker_root, "containers", container_id, "checkpoints")
        super().__init__(root_dir=root_dir)

class GvisorAttachManager(EnvironmentManager):
    def __init__(self,
                 container_name: str,
                 base_image: str,
                 extra_args: Optional[List[str]] = None,
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a gVisor-based environment.

        :param container_name: Name of the running container to attach to.
        :param base_image: Base image to use for the environment.
            Example: "ubuntu:latest" or "statefork:base".
        :param extra_args: Additional command-line args passed during container startup.
            Example: ["-v", "/tmp:/tmp"]
        """
        super().__init__(backend_name="gVisor", decider=decider)
        self.container_name = container_name
        self.extra_args = extra_args or []
        self.base_image = base_image

        sid, _ = self._core_snapshot(self.current_branch_id)
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init the Tree Graph
        self._record_snapshot_node(SnapshotNode(snapshot_id=sid, parent_id=None))
        self.current_snapshot_id = sid
        self.last_snapshot_id = sid

        gc = GvisorCalculator(self.container_name)
        self._stats.attach_size_calculator(gc)

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        snapshot_id = str(uuid.uuid4())[:8]

        start = time.time()
        try:
            subprocess.run(
                ["docker", "checkpoint", "create", "--leave-running", self.container_name, snapshot_id],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            elapsed = time.time() - start
            self._register_snapshot_resource(snapshot_id, snapshot_id)
            self.current_snapshot_id = snapshot_id
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"gVisor snapshot failed: {e}")
            return None, 0.0

    def _core_restore(self, snapshot_id: str, branch_id: str) -> tuple[bool, float]:
        snapshot_id = self._snapshot_resource(snapshot_id)
        if not snapshot_id:
            logger.warning(f"Snapshot {snapshot_id} not found.")
            return False, 0.0

        # Stop container if running
        subprocess.run(["docker", "stop", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Start from checkpoint
        cmd = ["docker", "start", "--checkpoint", snapshot_id, self.container_name]
        logger.debug(f"Starting container with command: {' '.join(cmd)}")

        start = time.time()
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)
            elapsed = time.time() - start

            return True, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"gVisor restore failed: {e}")
            return False, 0.0

    def _core_cleanup(self):
        logger.info(f"Cleaning up gVisor docker container '{self.container_name}'")
        try:
            subprocess.run(["docker", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # if not using custom --checkpoint-dir on checkpoint, checkpoints are in container dir and removed during rm.
            logger.info(f"Cleaning up gvisor docker base image...")
            subprocess.run(["docker", "rmi", "-f", self.base_image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            logger.error(f"gVisor cleanup failed: {e}")
            return

    def _core_exec(self, command, timeout=None, branch_id: str = "main"):
        if isinstance(command, list):
            cmd = ["docker", "exec", self.container_name] + command
        else:
            cmd = ["docker", "exec", self.container_name, "bash", "-c", command]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return result.returncode, result.stdout, result.stderr


class GvisorBuildManager(GvisorAttachManager):
    def __init__(self,
                 dockerfile_dir: str = ".",
                 base_image: str = None,
                 extra_args: Optional[List[str]] = None,
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a gVisor-based environment by building from a Dockerfile.

        :param dockerfile_dir: Path to the directory containing the Dockerfile.
            Example: "/home/user/projects/myapp/"
        :param base_image: Base image to use for building the container.
            Example: "python:3.10-slim"
        :param extra_args: Additional command-line args passed during container startup.
            Example: ["-v", "/tmp:/tmp"]
        """
        if base_image is None:
            base_image = f"statefork_{str(uuid.uuid4())[:4]}:base"

        if extra_args is None: #TODO
            extra_args = ["-v", "/tmp:/tmp"]

        container_name = "statefork_active"

        logger.info(f"Building base gVisor image '{base_image}' from directory '{dockerfile_dir}'...")
        subprocess.run(["docker", "build", "-t", base_image, dockerfile_dir], stdout=subprocess.DEVNULL, check=True)

        logger.info("Creating initial environment from base image...")
        cmd = ["docker", "run", "-d", "--runtime", "runsc", "--network", "host", "--name", container_name] + extra_args + [base_image]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)
        except subprocess.CalledProcessError:
            raise RuntimeError("Failed to create initial environment from base image.")

        super().__init__(container_name=container_name, base_image=base_image, extra_args=extra_args, decider=decider)
