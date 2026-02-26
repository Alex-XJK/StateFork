from __future__ import annotations
import re
import subprocess
import time
import uuid
import logging
from typing import Optional, Literal, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import Calculator
from decider import Decider

BackendType = Literal["Docker", "Podman"]

def get_backend_tool(backend: BackendType) -> tuple[str, str, logging.Logger]:
    """
    Returns the command, name, and logger for the specified backend.
    :param backend: The backend type, either "Docker" or "Podman".
    :return: A tuple containing the command to run, the name of the backend, and a logger instance.
    """
    if backend == "Docker":
        return "docker", "Docker", logging.getLogger("EnvManager.Docker")
    elif backend == "Podman":
        return "podman", "Podman", logging.getLogger("EnvManager.Podman")
    else:
        raise ValueError(f"Unsupported backend: {backend}")


class ImageCalculator(Calculator):
    def __init__(self, cli_command: str, repository: str):
        super().__init__(name="ImageCalculator")
        self.cli = cli_command
        self.repository = repository
        self.logger.debug(f"Tracking image sizes for '{self.repository}' via `{self.cli} images --format`")

    def _collect(self) -> List[tuple[str, int]]:
        try:
            output = subprocess.check_output(
                [self.cli, "images", self.repository, "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"],
                text=True
            )
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to list images: {e}")
            return []

        data = []
        for line in output.strip().splitlines():
            try:
                parts = line.strip().split()
                name = parts[0]
                size_str = ''.join(parts[1:]).replace(" ", "")
                size_bytes = self.parse_size(size_str)
                data.append((name, size_bytes))
            except ValueError as e:
                self.logger.warning(f"Skipping malformed line '{line}': {e}")
        return data

    @staticmethod
    def parse_size(size_str: str) -> int:
        """
        Converts strings like '824.5MB', '56KB', '1.2GB' to bytes.
        """
        match = re.match(r"([0-9.]+)([KMG]?B)", size_str.upper())
        if not match:
            raise ValueError(f"Invalid size format: {size_str}")

        num = float(match.group(1))
        unit = match.group(2)

        multiplier = {
            "B": 1,
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3
        }.get(unit, 1)

        return int(num * multiplier)


class ContainerAttachManager(EnvironmentManager):
    def __init__(self,
                 backend: BackendType,
                 container_name: str,
                 base_image: str,
                 extra_args: Optional[List[str]] = None,
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a container-based environment by attaching to an existing container.

        :param backend: Backend type, either "Docker" or "Podman".
        :param container_name: Name of the running container to attach to.
        :param base_image: Base image to use for the environment.
            Example: "ubuntu:latest" or "statefork:base".
        :param extra_args: Additional command-line args passed during container startup.
            Example: ["-p", "8000:8000", "-v", "/tmp:/tmp"]
        """
        self.BACKEND_CMD, self.BACKEND_NAME, self.logger = get_backend_tool(backend)
        super().__init__(backend_name=self.BACKEND_NAME, decider=decider)

        self.extra_args = extra_args or []
        self.image_prefix, _ = base_image.split(":", 1)
        self.logger.info(f"Recognized base image prefix: {self.image_prefix}")
        self.snapshots["base"] = base_image

        # Init the snapshot graph
        self.snapshot_graph["base"] = SnapshotNode(snapshot_id="base", parent_id=None)

        # Initialize main branch: head points to base, container is the attached one
        self.branches["main"].head_snapshot_id = "base"
        self.branches["main"].container_name = container_name

        # Attach the ImageCalculator to track image sizes
        ic = ImageCalculator(self.BACKEND_CMD, self.image_prefix)
        self._stats.attach_size_calculator(ic)

    def _active_container(self) -> Optional[str]:
        """
        Returns the container name for the currently active branch.
        This is the single source of truth for which container to act on.
        """
        branch = self.branches.get(self.active_branch)
        return branch.container_name if branch else None

    def _core_snapshot(self) -> tuple[Optional[str], float]:
        container = self._active_container()
        if not container:
            self.logger.error(f"No container found for active branch '{self.active_branch}'.")
            return None, 0.0

        snapshot_id = str(uuid.uuid4())[:8]
        image_name = f"{self.image_prefix}:{snapshot_id}"

        start = time.time()
        try:
            subprocess.run(
                [self.BACKEND_CMD, "commit", container, image_name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to commit container '{container}': {e}")
            return None, 0.0
        elapsed = time.time() - start

        self.snapshots[snapshot_id] = image_name
        return snapshot_id, elapsed

    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        image_name = self.snapshots.get(snapshot_id)
        if not image_name:
            self.logger.warning(f"Snapshot ID '{snapshot_id}' not found in snapshots.")
            return None, 0.0

        # The container to (re)create is the one owned by the active branch
        container = self._active_container()
        if not container:
            self.logger.error(f"No container found for active branch '{self.active_branch}'.")
            return None, 0.0

        # Stop & remove existing container if running
        subprocess.run(
            [self.BACKEND_CMD, "rm", "-f", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        cmd = [self.BACKEND_CMD, "run", "-d", "--rm", "--name", container] + self.extra_args + [image_name]
        self.logger.debug(f"Launching container with command: {' '.join(cmd)}")

        start = time.time()
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to start container '{container}': {e}")
            return None, 0.0
        elapsed = time.time() - start

        return container, elapsed

    def _core_fork_env(self, branch_name: str, snapshot_id: str) -> tuple[Optional[str], float]:
        image_name = self.snapshots.get(snapshot_id)
        if not image_name:
            self.logger.error(f"Snapshot '{snapshot_id}' not found.")
            return None, 0.0

        container_name = f"statefork_{branch_name}"

        # Remove existing container with same name (safety)
        subprocess.run(
            [self.BACKEND_CMD, "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Strip port bindings from extra_args: forked containers run in parallel
        # alongside the main container so reusing the same ports would cause
        # "port already allocated" errors.
        filtered_args = []
        skip_next = False
        for arg in self.extra_args:
            if skip_next:
                skip_next = False
                continue
            if arg == "-p":
                skip_next = True
                continue
            if arg.startswith("-p"):
                continue
            filtered_args.append(arg)

        cmd = [
            self.BACKEND_CMD, "run", "-d", "--rm",
            "--name", container_name
        ] + filtered_args + [image_name]
        self.logger.debug(f"Forking container with command: {' '.join(cmd)}")

        start = time.time()
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to fork container: {e.stderr}")
            return None, 0.0
        elapsed = time.time() - start

        return container_name, elapsed

    def _core_exec(self, command: List[str] | str, timeout: Optional[float] = None) -> tuple[int, str, str]:
        container = self._active_container()
        if not container:
            raise RuntimeError(f"No active container for branch '{self.active_branch}'")

        if isinstance(command, list):
            cmd = [self.BACKEND_CMD, "exec", container] + command
        else:
            cmd = [self.BACKEND_CMD, "exec", container, "bash", "-c", command]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return result.returncode, result.stdout, result.stderr

    def _core_cleanup(self) -> None:
        # Tear down all branch containers (deduplicated)
        seen_containers = set()
        for branch in self.branches.values():
            if branch.container_name and branch.container_name not in seen_containers:
                self.logger.info(f"Removing container '{branch.container_name}'...")
                subprocess.run(
                    [self.BACKEND_CMD, "rm", "-f", branch.container_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                seen_containers.add(branch.container_name)

        # Remove all tracked images, skipping the base image since we don't own it
        self.logger.info(f"Cleaning up {self.BACKEND_NAME} images...")
        for snapshot_id, image_name in list(self.snapshots.items()):
            if snapshot_id == "base":
                self.logger.debug(f"Skipping base image '{image_name}' (not owned by us).")
                continue
            subprocess.run(
                [self.BACKEND_CMD, "rmi", image_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        self.snapshots.clear()


class ContainerBuildManager(ContainerAttachManager):
    def __init__(self,
                 backend: BackendType,
                 dockerfile_dir: str = ".",
                 base_image: str = None,
                 extra_args: Optional[List[str]] = None,
                 decider: Optional[Decider] = None,
                 ):
        """
        Initialize a container-based environment by building from a Dockerfile.

        :param backend: Backend type, either "Docker" or "Podman".
        :param dockerfile_dir: Path to the directory containing the Dockerfile.
            Example: "/home/user/projects/myapp/"
        :param base_image: Base image name to tag the built image.
            Example: "python:3.10-slim"
        :param extra_args: Additional command-line args passed during container startup.
            Example: ["-p", "8000:8000", "-v", "/tmp:/tmp"]
        """
        backend_cmd, backend_name, logger = get_backend_tool(backend)

        if base_image is None:
            base_image = f"statefork_{str(uuid.uuid4())[:4]}:base"

        if extra_args is None:
            extra_args = ["-p", "8000:8000", "-v", "/tmp:/tmp"]

        logger.info(f"Building base {backend_name} image '{base_image}' from '{dockerfile_dir}'...")
        subprocess.run(
            [backend_cmd, "build", "-t", base_image, dockerfile_dir],
            stdout=subprocess.DEVNULL,
            check=True
        )

        # ContainerBuildManager owns the base image it built, so cleanup should remove it.
        # We pass container_name="statefork_active" as the initial main container name.
        super().__init__(
            backend=backend,
            container_name="statefork_active",
            base_image=base_image,
            extra_args=extra_args,
            decider=decider
        )

        logger.info("Creating initial environment from base image...")
        result, _ = self._core_create_env("base")
        if result is None:
            raise RuntimeError("Failed to create initial environment from base image.")

    def _core_cleanup(self) -> None:
        # Run the base cleanup first (removes containers and non-base images)
        super()._core_cleanup()

        # ContainerBuildManager owns the base image it built, so remove it too
        base_image = self.snapshots.get("base") or f"{self.image_prefix}:base"
        self.logger.info(f"Removing owned base image '{base_image}'...")
        subprocess.run(
            [self.BACKEND_CMD, "rmi", base_image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
