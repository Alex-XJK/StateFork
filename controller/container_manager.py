import subprocess
import time
import uuid
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from benchmark import BenchmarkStats

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EnvironmentManager(ABC):

    def __init__(self):
        self.snapshots: Dict[str, str] = {}  # snapshot_id -> image_id
        self.stats = BenchmarkStats()

    @abstractmethod
    def snapshot(self) -> str:
        """
        Create a snapshot of the current environment.
        Returns a unique identifier for the snapshot.
        """
        pass

    @abstractmethod
    def restore(self, snapshot_id: str) -> bool:
        """
        Restore the environment to a previous snapshot.
        Returns True if successful, False otherwise.
        """
        pass

    @abstractmethod
    def create_env_from_snapshot(self, snapshot_id: str) -> Optional[str]:
        """
        Create a new environment from a given snapshot.
        Returns the name of the new container or None if it fails.
        """
        pass

    def list_snapshots(self) -> List[str]:
        """
        List all available snapshots.
        Returns a list of snapshot IDs.
        """
        return list(self.snapshots.keys())

    def print_benchmark_log(self):
        self.stats.print_summary()


class DockerEnvironmentManager(EnvironmentManager):
    def __init__(self, base_image: str = "statefork-app:latest"):
        super().__init__()
        self.base_image = base_image

        logger.info("Building base Docker image...")
        subprocess.run(["docker", "build", "-t", self.base_image, "."], check=True)

    def snapshot(self) -> str:
        snapshot_id = str(uuid.uuid4())[:8]
        container_name = f"statefork_active"
        image_name = f"snapshot_{snapshot_id}"

        start = time.time()
        subprocess.run(["docker", "commit", container_name, image_name], check=True)
        elapsed = time.time() - start

        self.snapshots[snapshot_id] = image_name
        self.stats.add_entry("snapshot", snapshot_id, elapsed)

        logger.info(f"Snapshot created: {snapshot_id} in {elapsed:.4f}s")
        return snapshot_id

    def create_env_from_snapshot(self, snapshot_id: str) -> Optional[str]:
        image_name = self.snapshots.get(snapshot_id)
        if not image_name:
            logger.error(f"Snapshot ID {snapshot_id} not found.")
            return None

        new_container_name = "statefork_active"

        # Stop & remove existing container if running
        subprocess.run(["docker", "rm", "-f", new_container_name], stderr=subprocess.DEVNULL)

        start = time.time()
        subprocess.run([
            "docker", "run", "-d",
            "--name", new_container_name,
            "-p", "8000:8000",
            image_name
        ], check=True)
        elapsed = time.time() - start

        self.stats.add_entry("create_env", snapshot_id, elapsed)

        logger.info(f"Container created from snapshot {snapshot_id} in {elapsed:.4f}s")
        return new_container_name

    def restore(self, snapshot_id: str) -> bool:
        start = time.time()
        result = self.create_env_from_snapshot(snapshot_id)
        elapsed = time.time() - start
        success = result is not None
        if success:
            self.stats.add_entry("restore", snapshot_id, elapsed)
        return success


def main():
    manager = DockerEnvironmentManager()

    print("StateFork Container Manager")
    print("Commands: snapshot, list, restore <id>, step, stats, exit")

    while True:
        cmd = input("StateFork > ").strip()

        if cmd == "exit":
            break

        elif cmd == "snapshot":
            sid = manager.snapshot()
            print(f"Snapshot created: {sid}")

        elif cmd == "list":
            print("Available snapshots:")
            for s in manager.list_snapshots():
                print(f" - {s}")

        elif cmd.startswith("restore"):
            _, _, sid = cmd.partition(" ")
            if not sid:
                print("Usage: restore <snapshot_id>")
                continue
            ok = manager.restore(sid)
            if ok:
                print(f"Restored to snapshot {sid}")
            else:
                print(f"Snapshot {sid} not found.")

        elif cmd == "step":
            sid = manager.snapshot()
            container = manager.create_env_from_snapshot(sid)
            if container is None:
                print("Failed to create new container from snapshot.")
            else:
                print(f"Stepped to new container with snapshot {sid}")

        elif cmd == "stats":
            manager.print_benchmark_log()

        else:
            print("Unknown command. Available: snapshot, list, restore <id>, step, stats, exit")

if __name__ == "__main__":
    main()
