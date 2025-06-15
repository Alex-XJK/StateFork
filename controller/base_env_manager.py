import logging
from abc import ABC, abstractmethod
from benchmark import BenchmarkStats
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SnapshotNode:
    snapshot_id: str
    parent_id: Optional[str]
    children: List[str] = field(default_factory=list)


class EnvironmentManager(ABC):

    def __init__(self):
        self.snapshots: Dict[str, str] = {}  # snapshot_id -> image_id
        self.stats = BenchmarkStats()
        self.current_snapshot_id: Optional[str] = None
        self.last_snapshot_id: Optional[str] = None
        self.snapshot_graph: Dict[str, SnapshotNode] = {}  # snapshot_id -> SnapshotNode

    def snapshot(self) -> Optional[str]:
        """
        Create a snapshot of the current environment.
        Returns a unique identifier for the snapshot.
        """
        parent_id = self.last_snapshot_id if self.last_snapshot_id else None

        # Core Operation
        snapshot_id, elapsed = self._core_snapshot()

        # Error handling for snapshot creation
        if snapshot_id is None:
            logger.error("Failed to create snapshot.")
            return None

        # Logging
        self.stats.add_entry("snapshot", snapshot_id, elapsed)
        logger.info(f"Snapshot created: {snapshot_id} in {elapsed:.4f}s")

        # Update the Tree Graph
        node = SnapshotNode(snapshot_id=snapshot_id, parent_id=parent_id)
        self.snapshot_graph[snapshot_id] = node
        if parent_id and parent_id in self.snapshot_graph:
            self.snapshot_graph[parent_id].children.append(snapshot_id)
        self.last_snapshot_id = snapshot_id

        return snapshot_id

    @abstractmethod
    def _core_snapshot(self) -> tuple[Optional[str], float]:
        """
        Internal method to create a core snapshot.
        Concrete implementations should override this method.
        Returns a unique identifier for the snapshot and the time taken.
        """
        pass

    def restore(self, snapshot_id: str) -> bool:
        """
        Restore the environment to a previous snapshot.
        Returns True if successful, False otherwise.
        """
        # Core Operation
        success, elapsed = self._core_restore(snapshot_id)

        # Error handling for restoration
        if not success:
            logger.error(f"Failed to restore environment from snapshot {snapshot_id}.")
            return False

        self.stats.add_entry("restore", snapshot_id, elapsed)
        logger.info(f"Environment restored from snapshot {snapshot_id} in {elapsed:.4f}s")

        # Update the Tree Graph
        self.current_snapshot_id = snapshot_id
        self.last_snapshot_id = snapshot_id
        return True

    @abstractmethod
    def _core_restore(self, snapshot_id: str) -> tuple[bool, float]:
        """
        Internal method to restore the environment from a snapshot.
        Concrete implementations should override this method.
        Returns True if successful, False otherwise and the time taken.
        """
        pass

    def create_env_from_snapshot(self, snapshot_id: str) -> Optional[str]:
        """
        Create a new environment from a given snapshot.
        Returns the name of the new container or None if it fails.
        """
        # Core Operation
        container_name, elapsed = self._core_create_env(snapshot_id)

        # Error handling for environment creation
        if container_name is None:
            logger.warning(f"Failed to create environment from snapshot {snapshot_id}.")
            return None

        self.stats.add_entry("container", snapshot_id, elapsed)

        logger.info(f"Container created from snapshot {snapshot_id} in {elapsed:.4f}s")

        # Update the Tree Graph
        self.current_snapshot_id = snapshot_id
        return container_name

    @abstractmethod
    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        """
        Internal method to create an environment from a snapshot.
        Concrete implementations should override this method.
        Returns the name of the new container and the time taken.
        """
        pass

    def cleanup(self):
        """
        Clean up any resources used by the environment manager.
        This should be called when the manager is no longer needed.
        """
        logger.info("Cleaning up Docker environment...")

        # Core Cleanup
        self._core_cleanup()

        logger.info("Cleanup complete.")

    @abstractmethod
    def _core_cleanup(self):
        """
        Internal method to clean up resources.
        Concrete implementations should override this method.
        """
        pass

    def list_snapshots(self) -> List[str]:
        """
        List all available snapshots.
        Returns a list of snapshot IDs.
        """
        return list(self.snapshots.keys())

    def print_snapshot_tree(self):
        def recurse(sid: str, indent: str = " "):
            if sid == self.current_snapshot_id:
                print(f"{indent}- {sid} (current)")
            elif sid == self.last_snapshot_id:
                print(f"{indent}- {sid} (last)")
            else:
                print(f"{indent}- {sid}")
            for child in self.snapshot_graph[sid].children:
                recurse(child, indent + "  ")

        roots = [sid for sid, node in self.snapshot_graph.items() if node.parent_id is None]
        if not roots:
            print("No snapshot tree available.")
            return
        print("Snapshot Tree:")
        for root in roots:
            recurse(root)
