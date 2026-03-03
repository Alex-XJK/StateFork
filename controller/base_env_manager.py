import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from .benchmark import BenchmarkStats
from decider import Decider, DecisionContext, AlwaysTrueDecider

logger = logging.getLogger("EnvManager.Base")

@dataclass
class Branch:
    name: str
    head_snapshot_id: str
    container_name: Optional[str] = None

    # branch-local runtime state (Command log and cumulative
    # execution time since last generated snapshotl)
    command_log: List[List[str] | str] = field(default_factory=list)
    cumulative_exec_time: float = 0.0

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
        self.snapshot_graph: Dict[str, SnapshotNode] = {}  # snapshot_id -> SnapshotNode
        self.branches: Dict[str, Branch] = {}  # branch_name -> branch
        self.active_branch: str = "main"
        self.__tmp_tree_print: str = "" # Temporary variable for tree printing, note this makes it non-thread-safe
        self.is_cleaned_up: bool = False
        self.decider: Decider = decider if decider is not None else AlwaysTrueDecider()

        # initialize the first default branch to main and id to None
        self.branches["main"] = Branch(
            name="main",
            head_snapshot_id=None,  # IMPORTANT: matches old "no snapshot yet"
            container_name=None,            
        )


    def __del__(self):
        """
        Cleanup resources when the EnvironmentManager is deleted.
        This is a fallback to ensure cleanup if not explicitly called.
        """
        if not self.is_cleaned_up:
            logger.info("EnvironmentManager is being deleted, performing cleanup...")
            self.cleanup()

    def snapshot(self) -> Optional[str]:
        """
        Create a snapshot of the current environment.
        Returns a unique identifier for the snapshot.
        """
        branch = self.branches[self.active_branch]
        parent_id = branch.head_snapshot_id

        context = DecisionContext(
            cumulative_exec_time=branch.cumulative_exec_time,
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
            branch.cumulative_exec_time = 0.0 

        else:
            # ===== Virtual Snapshot =====
            snapshot_id = f"v{int(time.time() * 1000) % 10_000_000:07d}"

            logger.info(f"Creating virtual snapshot: {snapshot_id}")

            node = SnapshotNode(
                snapshot_id=snapshot_id,
                parent_id=parent_id,
                is_virtual=True,
                replay_commands=list(branch.command_log),
            )

        # ===== Graph Update =====
        self.snapshot_graph[snapshot_id] = node
        if parent_id and parent_id in self.snapshot_graph:
            parent_node = self.snapshot_graph[parent_id]
            parent_node.children.append(snapshot_id)

        # Reset command log and time since state is fully tracked
        branch.command_log.clear()

        branch.head_snapshot_id = snapshot_id

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

    def fork(self, branch_name: str, snapshot_id: str) -> bool:
        """
        Create a new branch from a given snapshot.
        This will spawn a NEW environment (container) without affecting current branch.
        """
        if branch_name in self.branches:
            logger.error(f"Branch '{branch_name}' already exists.")
            return False

        if snapshot_id not in self.snapshot_graph:
            logger.error(f"Snapshot '{snapshot_id}' not found.")
            return False

        node = self.snapshot_graph[snapshot_id]

        # ===== Case 1: Physical =====
        if not node.is_virtual:
            # Core: create a NEW environment (parallel container)
            container_name, elapsed = self._core_fork_env(branch_name, snapshot_id)

            if container_name is None:
                logger.error(f"Failed to fork environment from snapshot {snapshot_id}")
                return False

            # Register new branch
            self.branches[branch_name] = Branch(
                name=branch_name,
                head_snapshot_id=snapshot_id,
                container_name=container_name,
            )

            self._stats.add_entry("fork", snapshot_id, elapsed)
            logger.info(
                f"Forked branch '{branch_name}' from snapshot {snapshot_id} "
                f"(container={container_name}) in {elapsed:.4f}s"
            )
            return True
        
        # ===== Case 2: Virtual =====
        logger.info(f"Forking from virtual snapshot {snapshot_id}")

        replay_chain = []
        current = node

        # Walk upward collecting virtual nodes
        while current.is_virtual:
            replay_chain.append(current)

            if current.parent_id is None:
                logger.error("Virtual snapshot has no physical ancestor.")
                return False

            current = self.snapshot_graph[current.parent_id]

        physical_ancestor = current  # must be physical

        # Register branch EARLY (so backend knows container context)
        new_branch = Branch(
            name=branch_name,
            head_snapshot_id=None,  # temporary during replay
            container_name=f"statefork_{branch_name}",
        )
        self.branches[branch_name] = new_branch

        # Temporarily switch active branch for correct container routing
        old_active = self.active_branch
        self.active_branch = branch_name
        try:
            # Create environment from physical ancestor
            container_name, elapsed = self._core_create_env(physical_ancestor.snapshot_id)

            if container_name is None:
                self.active_branch = old_active
                del self.branches[branch_name]
                logger.error("Failed to create environment from physical ancestor.")
                return False

            # Replay virtual chain (forward order)
            replay_chain.reverse()
            for virtual_node in replay_chain:
                for cmd in virtual_node.replay_commands:
                    rc, _, stderr = self.exec_command(cmd)
                    if rc != 0:
                        logger.error(f"Replay failed during fork: {cmd}\n{stderr}")
                        # rollback branch registration
                        self.active_branch = old_active
                        del self.branches[branch_name]
                        return False

            #  Finalize branch state
            new_branch.head_snapshot_id = snapshot_id
            new_branch.command_log.clear()
            new_branch.cumulative_exec_time = 0.0
            self.is_cleaned_up = False

            self._stats.add_entry("fork", snapshot_id, elapsed)
            logger.info(
                f"Forked branch '{branch_name}' from virtual snapshot {snapshot_id} "
                f"(container={container_name}) in {elapsed:.4f}s"
            )
            return True
        
        finally:
            # Restore original active branch
            self.active_branch = old_active

    @abstractmethod
    def _core_fork_env(self, branch_name: str, snapshot_id: str) -> tuple[Optional[str], float]:
        """
        Create a NEW environment (parallel container) from a snapshot.
        MUST NOT destroy the current container.
        Returns (container_name, elapsed_time).
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
        branch = self.branches[self.active_branch]

        # ===== Case 1: Physical =====
        if not node.is_virtual:
            success, elapsed = self._core_restore(snapshot_id)

            if not success:
                logger.error(f"Failed to restore snapshot {snapshot_id}.")
                return False

            self._stats.add_entry("restore", snapshot_id, elapsed)
            logger.info(f"Restored physical snapshot {snapshot_id} in {elapsed:.4f}s")

            # Restore affects ONLY active branch head
            branch.head_snapshot_id = snapshot_id
            branch.command_log.clear()
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

        branch.head_snapshot_id = snapshot_id
        branch.command_log.clear()
        return True

    def _core_restore(self, snapshot_id: str) -> tuple[bool, float]:
        """
        Internal method to restore the environment from a snapshot.
        Here provide a default implementation that can be overridden by concrete managers.
        Returns True if successful, False otherwise and the time taken.
        """
        result, elapsed = self._core_create_env(snapshot_id)

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

    def switch_branch(self, branch_name: str) -> bool:
        if branch_name not in self.branches:
            return False

        self.active_branch = branch_name
        return True

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
        branch = self.branches[self.active_branch]
        start = time.time()

        try:
            returncode, stdout, stderr = self._core_exec(command=command, timeout=timeout)
        except Exception as e:
            elapsed = time.time() - start
            active_head = branch.head_snapshot_id
            self._stats.add_entry("exec", active_head, elapsed)
            logger.error(f"Execution failed: {e}")
            # Still record the command
            branch.command_log.append(command)
            branch.cumulative_exec_time += elapsed
            return -1, "", str(e)

        elapsed = time.time() - start
        branch.cumulative_exec_time += elapsed
        active_head = branch.head_snapshot_id
        self._stats.add_entry("exec", active_head or "<none>", elapsed)

        branch.command_log.append(command)

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
        This method traverses the snapshot graph and formats it for display. It will
        also print the annotated branch heads.
        Special Notes: This is NOT thread-safe due to the use of a temporary variable.
        :return: str representation of the snapshot tree.
        """
        self.__tmp_tree_print = ""

        # Build reverse mapping: snapshot_id -> [branch names]
        branch_heads: Dict[str, List[str]] = {}
        for branch in self.branches.values():
            if branch.head_snapshot_id:
                branch_heads.setdefault(branch.head_snapshot_id, []).append(branch.name)

        def recurse(sid: str, indent: str = " "):
            head_annotation = ""
            if sid in branch_heads:
                labels = []
                for b in branch_heads[sid]:
                    if b == self.active_branch:
                        labels.append(f"{b}*")
                    else:
                        labels.append(b)
                head_annotation = f" [head: {', '.join(labels)}]"

            self.__tmp_tree_print += f"{indent}- {sid}{head_annotation}\n"

            for child in self.snapshot_graph[sid].children:
                recurse(child, indent + "  ")

        roots = [sid for sid, node in self.snapshot_graph.items() if node.parent_id is None]

        if not roots:
            return "No snapshot tree available.\n"

        self.__tmp_tree_print += "Snapshot Tree:\n"
        for root in roots:
            recurse(root)

        return self.__tmp_tree_print

    def print_branches(self) -> str:
        """
        Print all branches and their head snapshot + container info.
        """
        lines = ["Branches:"]
        for name, branch in self.branches.items():
            marker = "*" if name == self.active_branch else " "
            lines.append(
                f" {marker} {name} -> head: {branch.head_snapshot_id}, "
                f"container: {branch.container_name}"
            )
        return "\n".join(lines) + "\n"

    @property
    def current_snapshot(self) -> Optional[str]:
        """
        Get the current snapshot ID.
        Returns None if no snapshot has been created.
        """
        branch = self.branches.get(self.active_branch)
        return branch.head_snapshot_id if branch else None

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