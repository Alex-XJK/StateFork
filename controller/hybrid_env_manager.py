import os
import subprocess
import tarfile
import tempfile
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

    def _resolve_pod_args(self) -> list[str]:
        """Return ['--pod', '<id>'] if the container belongs to a known pod, else []."""
        guessed_pod_name = f"pod_{self.container_name}"
        try:
            insp = subprocess.run(
                ["podman", "pod", "inspect", guessed_pod_name, "--format", "{{.Id}}"],
                capture_output=True, text=True, check=False,
            )
            pod_id = (insp.stdout or "").strip()
            if pod_id:
                return ["--pod", pod_id]
        except Exception:
            pass
        return []

    def _get_upper_dir(self) -> Optional[str]:
        """Return the overlay UpperDir path for the current container."""
        try:
            result = subprocess.run(
                ["podman", "inspect", self.container_name,
                 "--format", '{{index .GraphDriver.Data "UpperDir"}}'],
                capture_output=True, text=True, check=True,
            )
            upper = result.stdout.strip()
            return upper if upper else None
        except Exception:
            return None

    _ROOTFS_EXCLUDE = ("run/", "etc/hostname", "etc/hosts",
                       "etc/resolv.conf", "etc/mtab", "dev/")

    @classmethod
    def _is_zstd_archive(cls, export_path: str) -> bool:
        """Best-effort check whether export_path is zstd-compressed tar."""
        lower = export_path.lower()
        if lower.endswith((".zst", ".zstd", ".tzst", ".tar.zst", ".tar.zstd")):
            return True
        try:
            with open(export_path, "rb") as f:
                magic = f.read(4)
            # zstd frame magic: 0x28B52FFD
            return magic == b"\x28\xb5\x2f\xfd"
        except Exception:
            return False

    @classmethod
    def _fix_rootfs_diff_in_export(cls, export_path: str, upper_dir: str) -> None:
        """Replace rootfs-diff.tar inside the checkpoint export with a
        correct tar built from the container's overlay upper directory.

        Podman 4.9.3 bug: metadata-only changes (e.g. chmod) are present
        in the overlay upper dir but omitted from rootfs-diff.tar.
        """
        work = tempfile.mkdtemp(prefix="statefork_fix_rootfs_")
        try:
            is_zstd = cls._is_zstd_archive(export_path)
            extract_cmd = (
                ["tar", "-I", "zstd", "-xf", export_path, "-C", work]
                if is_zstd
                else ["tar", "-xf", export_path, "-C", work]
            )
            subprocess.run(
                extract_cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            new_rootfs = os.path.join(work, "rootfs-diff.tar")
            with tarfile.open(new_rootfs, "w") as tf:
                for entry in os.scandir(upper_dir):
                    tf.add(entry.path, arcname=entry.name, recursive=True)

            cleaned = os.path.join(work, "rootfs-diff-cleaned.tar")
            with tarfile.open(new_rootfs, "r") as src, tarfile.open(cleaned, "w") as dst:
                for member in src:
                    if any(member.name == p.rstrip("/") or member.name.startswith(p)
                           for p in cls._ROOTFS_EXCLUDE):
                        continue
                    if member.isfile():
                        dst.addfile(member, src.extractfile(member))
                    else:
                        dst.addfile(member)
            os.replace(cleaned, new_rootfs)

            repacked_tar = export_path + ".tmp.tar"
            subprocess.run(
                ["tar", "-cf", repacked_tar, "-C", work, "."],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if is_zstd:
                subprocess.run(
                    ["zstd", "-f", "--rm", "-q", repacked_tar, "-o", export_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.replace(repacked_tar, export_path)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------ #
    #  Snapshot: podman container checkpoint --leave-running -e <path>    #
    # ------------------------------------------------------------------ #
    def _core_snapshot(self) -> tuple[Optional[str], float]:
        sid = str(uuid.uuid4())[:8]
        export_path = os.path.join(self.export_dir, f"{sid}.tar")
        # export_path = os.path.join(self.export_dir, f"{sid}.tar.zstd")

        # Debug: show container filesystem state immediately before checkpoint
        try:
            pre_snap = subprocess.run(
                ["podman", "exec", self.container_name, "ls", "-al", "/app/"],
                capture_output=True, text=True, check=False,
            )
            print(f"[DEBUG] [SNAPSHOT_PRE] sid={sid} container={self.container_name} ls -al /app/:\n{pre_snap.stdout}{pre_snap.stderr}")
        except Exception as e:
            print(f"[DEBUG] [SNAPSHOT_PRE] failed to ls: {e}")

        start = time.time()
        subprocess.run(
            [
                "podman", "container", "checkpoint",
                self.container_name,
                "--leave-running",
                "-e", export_path,
                "-c=none"
            ],
            stdout=subprocess.DEVNULL,
            check=True,
        )

        # Podman 4.9.3 bug: rootfs-diff.tar omits metadata-only changes
        # (e.g. chmod).  Rebuild it from the overlay upper dir.
        upper_dir = self._get_upper_dir()
        if upper_dir and os.path.isdir(upper_dir):
            try:
                self._fix_rootfs_diff_in_export(export_path, upper_dir)
                print(f"[DEBUG] [SNAPSHOT_FIX] sid={sid} patched rootfs-diff.tar from upper_dir")
            except Exception as e:
                print(f"[DEBUG] [SNAPSHOT_FIX] sid={sid} failed to patch rootfs-diff: {e}")

        elapsed = time.time() - start

        self.snapshots[sid] = export_path

        # Debug: verify the patched export
        try:
            export_size = os.path.getsize(export_path) if os.path.exists(export_path) else -1
            list_cmd = (
                ["tar", "-I", "zstd", "-tf", export_path]
                if self._is_zstd_archive(export_path)
                else ["tar", "-tf", export_path]
            )
            tar_list = subprocess.run(
                list_cmd,
                capture_output=True, text=True, check=False, timeout=10,
            )
            tar_entries = (tar_list.stdout or "").strip().splitlines()
            rootfs_entries = [e for e in tar_entries if "rootfs-diff" in e]
            print(
                f"[DEBUG] [SNAPSHOT_DONE] sid={sid} export_path={export_path} "
                f"size={export_size} total_tar_entries={len(tar_entries)} "
                f"rootfs_diff_entries={len(rootfs_entries)}"
            )
        except Exception as e:
            print(f"[DEBUG] [SNAPSHOT_DONE] sid={sid} tar_inspect_failed={e}")

        return sid, elapsed

    # ------------------------------------------------------------------ #
    #  Restore: rm -f  then  restore --import <path> --name --pod        #
    # ------------------------------------------------------------------ #
    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        export_path = self.snapshots.get(snapshot_id)
        if not export_path:
            logger.warning(f"Snapshot ID {snapshot_id} not found.")
            return None, 0.0

        # Remove existing container (mirrors: podman rm -f $CTR)
        subprocess.run(["podman", "rm", "-f", self.container_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        pod_args = self._resolve_pod_args()
        print(f"[DEBUG] Restoring snapshot {snapshot_id} with pod_args={pod_args}")

        start = time.time()
        restore_cmd = (
            ["podman", "container", "restore"]
            + pod_args
            + ["--import", export_path, "--name", self.container_name]
        )
        subprocess.run(restore_cmd, stdout=subprocess.DEVNULL, check=True)
        elapsed = time.time() - start

        # Debug: show container filesystem state immediately after restore
        try:
            post_restore = subprocess.run(
                ["podman", "exec", self.container_name, "ls", "-al", "/app/"],
                capture_output=True, text=True, check=False,
            )
            print(f"[DEBUG] [RESTORE_POST] sid={snapshot_id} container={self.container_name} ls -al /app/:\n{post_restore.stdout}{post_restore.stderr}")
        except Exception as e:
            print(f"[DEBUG] [RESTORE_POST] failed to ls: {e}")

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
