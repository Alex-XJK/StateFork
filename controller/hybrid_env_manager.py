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

        logger.info(f"Initializing HybridAttachManager with container '{self.container_name}'")

        # Ensure container is running
        self.__ensure_container_running()
        print("container is running")
        # For terminal-bench integration, we don't create initial snapshot here
        # The agent will create checkpoints as needed
        print("Skipping initial snapshot creation for terminal-bench integration")
        
        # Init the Tree Graph with a placeholder
        self.current_snapshot_id = None
        self.last_snapshot_id = None
        
        # Initialize snapshots dict
        self.snapshots = {}

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

        # Kill tmux sessions before checkpoint to avoid socket issues
        # Terminal-bench will automatically recreate tmux sessions when needed
        try:
            subprocess.run([
                "podman", "exec", self.container_name, "tmux", "kill-server"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[DEBUG] Killed tmux server before checkpoint")
        except Exception as e:
            print(f"[DEBUG] Warning: Failed to kill tmux server before checkpoint: {e}")
        
        # Clean up any remaining tmux socket files to avoid CRIU issues
        try:
            subprocess.run([
                "podman", "exec", self.container_name, "bash", "-c", "rm -rf /tmp/tmux-*"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[DEBUG] Cleaned up tmux socket files before checkpoint")
        except Exception as e:
            print(f"[DEBUG] Warning: Failed to clean up tmux socket files: {e}")
        
        # Additional cleanup: kill any remaining tmux processes and clean sockets again
        try:
            subprocess.run([
                "podman", "exec", self.container_name, "pkill", "-f", "tmux"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([
                "podman", "exec", self.container_name, "bash", "-c", "rm -rf /tmp/tmux-*"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[DEBUG] Additional cleanup: killed tmux processes and cleaned sockets again")
        except Exception as e:
            print(f"[DEBUG] Warning: Failed to do additional cleanup: {e}")
        
        # Verify no tmux sockets remain
        try:
            result = subprocess.run([
                "podman", "exec", self.container_name, "bash", "-c", "ls -l /tmp | grep tmux || echo 'no sockets'"
            ], capture_output=True, text=True)
            if "no sockets" in result.stdout.strip():
                print(f"[DEBUG] Verified: No tmux sockets remain")
            else:
                print(f"[DEBUG] WARNING: Found remaining tmux files: {result.stdout.strip()}")
        except Exception as e:
            print(f"[DEBUG] Warning: Failed to verify socket cleanup: {e}")

        start = time.time()
        subprocess.run([
            "sudo", "podman", "container", "checkpoint", self.container_name,
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
        print("[DEBUG] Creating env with podman container named: ", self.container_name)
        # Stop & remove existing container if running
        subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Find the pod for this container
        pod_name = f"pod_{self.container_name}"
        try:
            pod_id = subprocess.run(
                ["podman", "pod", "ps", "--no-trunc", "--format", "{{.Id}}", "--filter", f"name={pod_name}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            
            if not pod_id:
                logger.warning(f"Pod {pod_name} not found, creating new pod")
                pod_id = subprocess.run(
                    ["podman", "pod", "create", "--name", pod_name],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to find/create pod: {e}, creating new pod")
            pod_id = subprocess.run(
                ["podman", "pod", "create", "--name", pod_name],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        start = time.time()
        subprocess.run([
            "sudo", "podman", "container", "restore",
            "--pod", pod_id,  # Use pod ID for restore
            "-i", export_path,
            "-n", self.container_name
        ], stdout=subprocess.DEVNULL, check=True)
        elapsed = time.time() - start

        return self.container_name, elapsed

    def _core_cleanup(self):
        logger.info(f"Cleaning up Podman container '{self.container_name}'")
        subprocess.run(["podman", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Cleaning up Podman pod '{self.container_name}'")
        subprocess.run(["podman", "pod", "rm", "-f", f"pod_{self.container_name}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


