import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from .benchmark import BenchmarkStats
from decider import Decider, DecisionContext, AlwaysTrueDecider

logger = logging.getLogger("EnvManager.Base")


@dataclass
class SnapshotNode:
    snapshot_id: str
    parent_id: Optional[str]
    children: List[str] = field(default_factory=list)

    # Logic for virtual snapshot
    is_virtual: bool = False
    # Commands needed to reach THIS snapshot from its parent
    replay_commands: List[List[str] | str] = field(default_factory=list)


class EnvironmentManager(ABC):
    """
    The base class and interface for managing environment snapshots.

    Applied the Template Method design pattern for core operations.
    Applied the Strategy design pattern for different environment managers.
    """

    def __init__(self, backend_name: str = "Base", decider: Optional[Decider] = None):
        self.backend_name = backend_name
        self.snapshots: Dict[str, str] = {}  # snapshot_id -> image_id
        self._stats = BenchmarkStats()
        self.current_snapshot_id: Optional[str] = None
        self.last_snapshot_id: Optional[str] = None
        self.snapshot_graph: Dict[str, SnapshotNode] = {}  # snapshot_id -> SnapshotNode
        self.__tmp_tree_print: str = "" # Temporary variable for tree printing, note this makes it non-thread-safe
        self.is_cleaned_up: bool = False

        # Command log since last generated snapshot
        self._command_log: List[List[str] | str] = []
        self.decider: Decider = decider if decider is not None else AlwaysTrueDecider()

        # cumulative execution time since last generated snapshot
        self._cumulative_exec_time: float = 0.0


    def __del__(self):
        """
        Cleanup resources when the EnvironmentManager is deleted.
        This is a fallback to ensure cleanup if not explicitly called.
        """
        # Defensive getattr: __init__ may never have run (e.g. the constructor
        # rejected its arguments), in which case there is nothing to clean up.
        if not getattr(self, "is_cleaned_up", True):
            logger.info("EnvironmentManager is being deleted, performing cleanup...")
            self.cleanup()


    def snapshot(self) -> Optional[str]:
        """
        Create a snapshot of the current environment.
        Returns a unique identifier for the snapshot.
        """
        parent_id = self.last_snapshot_id if self.last_snapshot_id else None

        context = DecisionContext(
            cumulative_exec_time=self._cumulative_exec_time,
        )
        take_physical = self.decider.decide(context)

        if take_physical:
            # ===== Physical Snapshot =====
            # Core Operation
            snapshot_id, elapsed = self._core_snapshot()

            # Error handling for snapshot creation
            if snapshot_id is None:
                logger.error("Failed to create snapshot.")
                return None

            # Logging
            self._stats.add_entry("snapshot", snapshot_id, elapsed)
            logger.info(f"Snapshot created: {snapshot_id} in {elapsed:.4f}s")

            node = SnapshotNode(
                snapshot_id=snapshot_id,
                parent_id=parent_id,
                is_virtual=False,
                replay_commands=[],
            )

            # Clean the cumulative execution time; Do not clean this in
            # virtual node because time is still need to go through that
            # virtual node in restore
            self._cumulative_exec_time = 0.0

        else:
            # ===== Virtual Snapshot =====
            snapshot_id = f"v{int(time.time() * 1000) % 10_000_000:07d}"

            logger.info(f"Creating virtual snapshot: {snapshot_id}")

            node = SnapshotNode(
                snapshot_id=snapshot_id,
                parent_id=parent_id,
                is_virtual=True,
                replay_commands=list(self._command_log),
            )

        # ===== Graph Update =====
        self.snapshot_graph[snapshot_id] = node
        if parent_id and parent_id in self.snapshot_graph:
            parent_node = self.snapshot_graph[parent_id]
            parent_node.children.append(snapshot_id)

        # Reset command log and time since state is fully tracked
        self._command_log = []

        self.last_snapshot_id = snapshot_id
        # TODO: figure out here:
        # self.current_snapshot_id = snapshot_id

        self.is_cleaned_up = False
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
        if snapshot_id not in self.snapshot_graph:
            logger.error(f"Snapshot {snapshot_id} not found.")
            return False

        node = self.snapshot_graph[snapshot_id]

        # ===== Case 1: Physical =====
        if not node.is_virtual:
            success, elapsed = self._core_restore(snapshot_id)

            if not success:
                logger.error(f"Failed to restore snapshot {snapshot_id}.")
                return False

            self._stats.add_entry("restore", snapshot_id, elapsed)
            logger.info(f"Restored physical snapshot {snapshot_id} in {elapsed:.4f}s")

            self.current_snapshot_id = snapshot_id
            self.last_snapshot_id = snapshot_id
            self._command_log = []
            return True

        # ===== Case 2: Virtual =====
        logger.info(f"Restoring virtual snapshot {snapshot_id}")

        replay_chain = []
        current = node

        # Walk upward collecting replay commands
        while current.is_virtual:
            replay_chain.append(current)

            if current.parent_id is None:
                logger.error("Virtual snapshot has no physical ancestor.")
                return False

            current = self.snapshot_graph[current.parent_id]

        physical_ancestor = current

        # Restore physical ancestor
        success, elapsed = self._core_restore(physical_ancestor.snapshot_id)
        if not success:
            logger.error("Failed to restore physical ancestor.")
            return False

        self._stats.add_entry("restore", physical_ancestor.snapshot_id, elapsed)

        # Replay forward
        replay_chain.reverse()

        for virtual_node in replay_chain:
            for cmd in virtual_node.replay_commands:
                rc, _, stderr = self.exec_command(cmd)
                if rc != 0:
                    logger.error(f"Replay failed: {cmd}\n{stderr}")

        self.current_snapshot_id = snapshot_id
        self.last_snapshot_id = snapshot_id
        self._command_log = []
        return True

    def _core_restore(self, snapshot_id: str) -> tuple[bool, float]:
        """
        Internal method to restore the environment from a snapshot.
        Here provide a default implementation that can be overridden by concrete managers.
        Returns True if successful, False otherwise and the time taken.
        """
        start = time.time()
        result, _ = self._core_create_env(snapshot_id)
        elapsed = time.time() - start

        return result is not None, elapsed

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

        self._stats.add_entry("container", snapshot_id, elapsed)

        logger.info(f"Container created from snapshot {snapshot_id} in {elapsed:.4f}s")

        # Update the Tree Graph
        self.current_snapshot_id = snapshot_id

        self.is_cleaned_up = False
        return container_name

    @abstractmethod
    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        """
        Internal method to create an environment from a snapshot.
        Concrete implementations should override this method.
        Returns the name of the new container and the time taken.
        """
        pass

    def cleanup(self) -> None:
        """
        Clean up any resources used by the environment manager.
        This should be called when the manager is no longer needed.
        """
        logger.info("Cleaning up environment...")

        # Core Cleanup
        self._core_cleanup()

        self.is_cleaned_up = True
        logger.info("Cleanup complete.")

    @abstractmethod
    def _core_cleanup(self) -> None:
        """
        Internal method to clean up resources.
        Concrete implementations should override this method.
        """
        pass

    def exec_command(self, command: List[str] | str, timeout: Optional[float] = None) -> tuple[int, str, str]:
        """
        Execute a command inside the managed environment and return (return code, stdout, stderr).
        - `command` may be a list of args or a raw shell string.
        - `timeout` in seconds (optional).
        """
        start = time.time()

        try:
            returncode, stdout, stderr = self._core_exec(command=command, timeout=timeout)
        except Exception as e:
            elapsed = time.time() - start
            self._stats.add_entry("exec", self.current_snapshot or "<none>", elapsed)
            logger.error(f"Execution failed: {e}")
            # Still record the command
            self._command_log.append(command)
            return -1, "", str(e)

        elapsed = time.time() - start
        self._cumulative_exec_time += elapsed
        self._stats.add_entry("exec", self.current_snapshot or "<none>", elapsed)

        self._command_log.append(command)

        logger.info(f"Exec finished (rc={returncode}) in {elapsed:.4f}s")
        return returncode, stdout, stderr

    def _core_exec(self, command: List[str] | str, timeout: Optional[float]) -> tuple[int, str, str]:
        """
        Backend-specific execution primitive.
        Must return a tuple (returncode, stdout, stderr).
        - `command` is either a list of args or a raw string, as passed to `exec_command`.
        - `timeout` is optional.
        """
        logger.warning(f"_core_exec not implemented in {self.backend_name} backend.")
        return -1, "", "Not implemented."

    def list_snapshots(self) -> List[str]:
        """
        List all available snapshots.
        Returns a list of snapshot IDs.
        """
        return list(self.snapshots.keys())

    def print_snapshot_tree(self) -> str:
        """
        This method traverses the snapshot graph and formats it for display.
        Special Notes: This is NOT thread-safe due to the use of a temporary variable.
        :return: str representation of the snapshot tree.
        """
        self.__tmp_tree_print = ""

        def recurse(sid: str, indent: str = " "):
            if sid == self.current_snapshot_id:
                self.__tmp_tree_print += f"{indent}- {sid} (current)\n"
            elif sid == self.last_snapshot_id:
                self.__tmp_tree_print += f"{indent}- {sid} (last)\n"
            else:
                self.__tmp_tree_print += f"{indent}- {sid}\n"
            for child in self.snapshot_graph[sid].children:
                recurse(child, indent + "  ")

        roots = [sid for sid, node in self.snapshot_graph.items() if node.parent_id is None]
        if not roots:
            return "No snapshot tree available.\n"

        self.__tmp_tree_print += "Snapshot Tree:\n"
        for root in roots:
            recurse(root)
        return self.__tmp_tree_print

    @property
    def current_snapshot(self) -> Optional[str]:
        """
        Get the current snapshot ID.
        Returns None if no snapshot has been created.
        """
        return self.current_snapshot_id

    @property
    def backend(self) -> str:
        """
        Get the name of the backend being used.
        """
        return self.backend_name

    @property
    def stats(self) -> BenchmarkStats:
        """
        Get the benchmark component of the environment manager.
        """
        return self._stats