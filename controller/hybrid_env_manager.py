import os
import subprocess
import time
import uuid
import shutil
import logging
from typing import Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import FileSizeCalculator

logger = logging.getLogger("EnvManager.PodmanHybrid")


class HybridAttachManager(EnvironmentManager):
    def __init__(self,
                 container_name: str,
                 export_dir: str = "/tmp/statefork_podman"
                 ):
        """
        Initialize a hybrid environment using Podman with CRIU by attaching to a running container.

        :param container_name: Name of the existing container to attach to.
            Example: "statefork_hybrid"
        :param export_dir: Directory for storing CRIU export files (.tar.zstd).
            Example: "/tmp/statefork_podman"
        """
        super().__init__(backend_name="Podman+CRIU")
        self.container_name = container_name
        self.export_dir = export_dir
        os.makedirs(self.export_dir, exist_ok=True)
        # Map snapshot_id -> committed image tag to preserve rootfs
        self.snapshot_images: dict[str, str] = {}

        logger.info(f"Initializing HybridAttachManager with container '{self.container_name}'")

        # Ensure container is running
        self.__ensure_container_running()

        # Take initial snapshot
        sid, _ = self._core_snapshot()
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init the Tree Graph
        self.snapshot_graph[sid] = SnapshotNode(snapshot_id=sid, parent_id=None)
        self.current_snapshot_id = sid
        self.last_snapshot_id = sid

        # Attach the FileSizeCalculator to the export directory
        self._stats.attach_size_calculator(FileSizeCalculator(self.export_dir))


    def __ensure_container_running(self):
        result = subprocess.run(["podman", "ps", "-q", "-f", f"name={self.container_name}"], capture_output=True, text=True)
        if not result.stdout.strip():
            raise RuntimeError(f"Container '{self.container_name}' is not running. Please start it before using this manager.")
        logger.debug(f"Pass validation: Container '{self.container_name}' is running.")

    def _core_snapshot(self) -> tuple[Optional[str], float]:
        sid = str(uuid.uuid4())[:8]
        export_path = os.path.join(self.export_dir, f"{sid}.tar.zstd")

        start = time.time()
        subprocess.run([
            "podman", "container", "checkpoint", self.container_name,
            "-e", export_path, "--leave-running"
        ], stdout=subprocess.DEVNULL, check=True)
        elapsed = time.time() - start

        self.snapshots[sid] = export_path

        return sid, elapsed

    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        export_path = self.snapshots.get(snapshot_id)
        if not export_path:
            logger.warning(f"Snapshot ID {snapshot_id} not found.")
            return None, 0.0

        # Stop & remove existing container if running
        subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # If the container belongs to a pod, restore into that existing pod.
        # Harness creates pods with the name pattern: "pod_<container_name>"
        pod_args: list[str] = []
        try:
            guessed_pod_name = f"pod_{self.container_name}"
            # Debug: list current pods before existence check
            try:
                pods_list = subprocess.run(
                    ["podman", "pod", "ps", "--no-trunc", "--format", "{{.ID}} {{.Name}}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                print(f"[DEBUG] Existing pods before restore:\n{pods_list.stdout or ''}")
            except Exception as e:
                logger.debug(f"Failed to list pods before restore: {e}")

            # Resolve pod ID (prefer ID to name for --pod)
            pod_id: Optional[str] = None
            try:
                lines = (pods_list.stdout or "").strip().splitlines()
                for line in lines:
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        pid, pname = parts
                        if pname == guessed_pod_name:
                            pod_id = pid
                            break
            except Exception:
                pass

            # Fallback via pod inspect to get ID
            if not pod_id:
                try:
                    insp = subprocess.run(
                        ["podman", "pod", "inspect", guessed_pod_name, "--format", "{{.Id}}"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    pod_id = (insp.stdout or "").strip() or None
                except Exception:
                    pod_id = None

            if pod_id:
                pod_args = ["--pod", pod_id]
                print(f"[DEBUG] Restoring into pod id: {pod_id} (name: {guessed_pod_name})")
            else:
                print(f"[DEBUG] Pod not found by name '{guessed_pod_name}', restoring without --pod")
        except Exception:
            # Best-effort only; continue without pod args
            pass

        start = time.time()
        # Ensure no stale container remains before restore (avoid name-in-use errors)
        try:
            subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass

        # Restore processes/state into the container name (created above or created by restore)
        restore_cmd = ["podman", "container", "restore"]
        # Always pass pod args to restore if we detected a pod; Podman requires --pod for pod containers
        if pod_args:
            restore_cmd += pod_args
        # Ensure export_path immediately follows -i (do not split -i and its value)
        restore_cmd += ["-i", export_path, "-n", self.container_name]

        subprocess.run(restore_cmd, stdout=subprocess.DEVNULL, check=True)
        elapsed = time.time() - start

        return self.container_name, elapsed

    def _core_cleanup(self):
        logger.info(f"Cleaning up Podman container '{self.container_name}'")
        subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Cleaning up Podman checkpoint files in {self.export_dir}")
        shutil.rmtree(self.export_dir, ignore_errors=True)


class HybridBuildManager(HybridAttachManager):
    def __init__(self,
                 container_name: str = "podman-build",
                 dockerfile_dir: str = ".",
                 export_dir: str = "/tmp/statefork_podman",
                 extra_args: Optional[List[str]] = None
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

        super().__init__(container_name=container_name, export_dir=export_dir)


