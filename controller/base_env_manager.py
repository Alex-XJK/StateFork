from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .benchmark import BenchmarkStats
from decider import Decider, DecisionContext, AlwaysTrueDecider


logger = logging.getLogger("EnvManager.Base")

# Sequential managers have exactly this one branch. Forkable managers reuse the
# same bookkeeping and add more branch records as live environments are forked.
DEFAULT_BRANCH_ID = "main"


@dataclass
class SnapshotNode:
    snapshot_id: str
    parent_id: Optional[str]
    children: List[str] = field(default_factory=list)

    # Logic for virtual snapshot
    is_virtual: bool = False
    # Commands needed to reach THIS snapshot from its parent
    replay_commands: List[List[str] | str] = field(default_factory=list)


@dataclass
class BranchState:
    """Controller bookkeeping for one independently advancing environment."""

    branch_id: str
    current_snapshot_id: Optional[str] = None
    last_snapshot_id: Optional[str] = None
    command_log: List[List[str] | str] = field(default_factory=list)
    cumulative_exec_time: float = 0.0
    active: bool = True
    operation_lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )


class EnvironmentManager(ABC):
    """
    Base snapshot/restore capability shared by every backend.

    Public operations are Template Methods. Backend implementations provide the
    protected ``_core_*`` primitives, while this class owns snapshot lineage,
    branch state, benchmarking, replay bookkeeping, and synchronization.

    A sequential manager contains one branch named :data:`DEFAULT_BRANCH_ID`.
    ``ForkableEnvironmentManager`` extends this bookkeeping with additional live
    branches without widening the public contract of these Template Methods.
    """

    def __init__(self, backend_name: str = "Base", decider: Optional[Decider] = None):
        self.backend_name = backend_name

        # Manager-wide immutable-snapshot state. Concrete backends should use
        # the protected registry helpers rather than mutating these dictionaries.
        self.snapshots: Dict[str, str] = {}  # snapshot_id -> backend resource id
        self.snapshot_graph: Dict[str, SnapshotNode] = {}

        self._stats = BenchmarkStats()
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._current_branch_lock = threading.RLock()
        self._branches: Dict[str, BranchState] = {
            DEFAULT_BRANCH_ID: BranchState(DEFAULT_BRANCH_ID)
        }
        self._current_branch_id = DEFAULT_BRANCH_ID

        self.is_cleaned_up: bool = False
        self.decider: Decider = decider if decider is not None else AlwaysTrueDecider()

    def __del__(self):
        """Best-effort fallback cleanup for a fully constructed manager."""
        if not getattr(self, "is_cleaned_up", True):
            logger.info("EnvironmentManager is being deleted, performing cleanup...")
            self.cleanup()

    # ------------------------------------------------------------------
    # Branch-state helpers
    # ------------------------------------------------------------------

    def _get_current_branch_id(self) -> str:
        with self._state_lock:
            return self._current_branch_id

    def _get_branch_state(self, branch_id: Optional[str] = None) -> BranchState:
        with self._state_lock:
            target = branch_id if branch_id is not None else self._current_branch_id
            try:
                return self._branches[target]
            except KeyError as exc:
                raise KeyError(f"Branch {target!r} is not registered.") from exc

    def _register_branch(
        self,
        branch_id: str,
        base_snapshot_id: Optional[str] = None,
    ) -> BranchState:
        with self._state_lock:
            state = self._branches.get(branch_id)
            if state is None:
                state = BranchState(
                    branch_id=branch_id,
                    current_snapshot_id=base_snapshot_id,
                    last_snapshot_id=base_snapshot_id,
                )
                self._branches[branch_id] = state
            return state

    def _remove_branch(self, branch_id: str) -> None:
        with self._state_lock:
            state = self._branches.get(branch_id)
            if state is not None:
                state.active = False
            self._branches.pop(branch_id, None)

    def _set_current_branch(self, branch_id: str) -> None:
        with self._current_branch_lock:
            with self._state_lock:
                if branch_id not in self._branches:
                    raise KeyError(f"Branch {branch_id!r} is not registered.")
                self._current_branch_id = branch_id

    def _set_branch_position(
        self,
        branch_id: str,
        snapshot_id: Optional[str],
    ) -> None:
        state = self._register_branch(branch_id, snapshot_id)
        with self._state_lock:
            state.active = True
            state.current_snapshot_id = snapshot_id
            state.last_snapshot_id = snapshot_id
            state.command_log = []
            state.cumulative_exec_time = 0.0

    # Compatibility properties keep existing sequential backend constructors
    # small while mapping their old single-branch fields onto DEFAULT_BRANCH_ID.
    @property
    def current_snapshot_id(self) -> Optional[str]:
        with self._state_lock:
            return self._get_branch_state().current_snapshot_id

    @current_snapshot_id.setter
    def current_snapshot_id(self, snapshot_id: Optional[str]) -> None:
        with self._state_lock:
            self._get_branch_state().current_snapshot_id = snapshot_id

    @property
    def last_snapshot_id(self) -> Optional[str]:
        with self._state_lock:
            return self._get_branch_state().last_snapshot_id

    @last_snapshot_id.setter
    def last_snapshot_id(self, snapshot_id: Optional[str]) -> None:
        with self._state_lock:
            self._get_branch_state().last_snapshot_id = snapshot_id

    @property
    def _command_log(self) -> List[List[str] | str]:
        with self._state_lock:
            return list(self._get_branch_state().command_log)

    @_command_log.setter
    def _command_log(self, commands: List[List[str] | str]) -> None:
        with self._state_lock:
            self._get_branch_state().command_log = list(commands)

    @property
    def _cumulative_exec_time(self) -> float:
        with self._state_lock:
            return self._get_branch_state().cumulative_exec_time

    @_cumulative_exec_time.setter
    def _cumulative_exec_time(self, elapsed: float) -> None:
        with self._state_lock:
            self._get_branch_state().cumulative_exec_time = elapsed

    # ------------------------------------------------------------------
    # Manager-wide snapshot registry helpers
    # ------------------------------------------------------------------

    def _register_snapshot_resource(self, snapshot_id: str, resource_id: str) -> None:
        with self._state_lock:
            self.snapshots[snapshot_id] = resource_id

    def _snapshot_resource(self, snapshot_id: str) -> Optional[str]:
        with self._state_lock:
            return self.snapshots.get(snapshot_id)

    def _snapshot_items(self) -> List[tuple[str, str]]:
        with self._state_lock:
            return list(self.snapshots.items())

    def _remove_snapshot_resource(self, snapshot_id: str) -> None:
        with self._state_lock:
            self.snapshots.pop(snapshot_id, None)

    def _record_snapshot_node(self, node: SnapshotNode) -> None:
        with self._state_lock:
            self.snapshot_graph[node.snapshot_id] = node
            if node.parent_id and node.parent_id in self.snapshot_graph:
                children = self.snapshot_graph[node.parent_id].children
                if node.snapshot_id not in children:
                    children.append(node.snapshot_id)

    # ------------------------------------------------------------------
    # Snapshot Template Method
    # ------------------------------------------------------------------

    def snapshot(self) -> Optional[str]:
        """Create a snapshot of the current branch."""
        with self._current_branch_lock:
            return self._snapshot_branch(self._get_current_branch_id())

    def _snapshot_branch(
        self,
        branch_id: str,
        *,
        force_physical: bool = False,
    ) -> Optional[str]:
        """Shared snapshot template used by sequential and forkable managers."""
        state = self._get_branch_state(branch_id)
        with state.operation_lock:
            with self._state_lock:
                if not state.active:
                    logger.error("Branch %s is no longer active.", branch_id)
                    return None
                parent_id = state.last_snapshot_id
                cumulative_exec_time = state.cumulative_exec_time
                replay_commands = list(state.command_log)

            context = DecisionContext(cumulative_exec_time=cumulative_exec_time)
            take_physical = force_physical or self.decider.decide(context)

            if take_physical:
                snapshot_id, elapsed = self._core_snapshot(branch_id)
                if snapshot_id is None:
                    logger.error("Failed to create snapshot on branch %s.", branch_id)
                    return None

                self._stats.add_entry(
                    "snapshot",
                    snapshot_id,
                    elapsed,
                    branch_id=branch_id,
                )
                logger.info(
                    "Snapshot created on branch %s: %s in %.4fs",
                    branch_id,
                    snapshot_id,
                    elapsed,
                )
                node = SnapshotNode(snapshot_id=snapshot_id, parent_id=parent_id)
            else:
                snapshot_id = f"v{uuid.uuid4().hex[:7]}"
                logger.info(
                    "Creating virtual snapshot on branch %s: %s",
                    branch_id,
                    snapshot_id,
                )
                node = SnapshotNode(
                    snapshot_id=snapshot_id,
                    parent_id=parent_id,
                    is_virtual=True,
                    replay_commands=replay_commands,
                )

            self._record_snapshot_node(node)
            with self._state_lock:
                state.current_snapshot_id = snapshot_id
                state.last_snapshot_id = snapshot_id
                state.command_log = []
                if take_physical:
                    state.cumulative_exec_time = 0.0

            self._after_snapshot(branch_id, snapshot_id)
            self.is_cleaned_up = False
            return snapshot_id

    def _after_snapshot(self, branch_id: str, snapshot_id: str) -> None:
        """Optional post-bookkeeping hook for capability subclasses."""

    @abstractmethod
    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        """Create an immutable physical snapshot of ``branch_id``.

        The snapshot template calls this hook while serializing operations for
        the branch.  ``branch_id`` identifies the live backend environment to
        capture.  On success, the implementation must register the backend
        resource with :meth:`_register_snapshot_resource` before returning.

        Return ``(snapshot_id, elapsed_seconds)`` on success and
        ``(None, elapsed_seconds)`` on failure.  This hook must not update the
        snapshot graph, branch position, benchmark records, or command log;
        the template owns that bookkeeping.  A failed call must not leave
        partial controller-side registration behind.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Restore Template Method
    # ------------------------------------------------------------------

    def restore(self, snapshot_id: str) -> bool:
        """Make ``snapshot_id`` the active state of the current branch."""
        with self._current_branch_lock:
            return self._restore_current_branch(snapshot_id)

    def _restore_current_branch(self, snapshot_id: str) -> bool:
        departing_state = self._get_branch_state()
        with departing_state.operation_lock:
            with self._state_lock:
                node = self.snapshot_graph.get(snapshot_id)
                if node is None:
                    logger.error("Snapshot %s not found.", snapshot_id)
                    return False

                replay_commands: List[List[str] | str] = []
                physical_ancestor = node
                visited = set()
                while physical_ancestor.is_virtual:
                    if physical_ancestor.snapshot_id in visited:
                        logger.error("Cycle detected in virtual snapshot ancestry.")
                        return False
                    visited.add(physical_ancestor.snapshot_id)
                    replay_commands[0:0] = physical_ancestor.replay_commands
                    if physical_ancestor.parent_id is None:
                        logger.error("Virtual snapshot has no physical ancestor.")
                        return False
                    parent = self.snapshot_graph.get(physical_ancestor.parent_id)
                    if parent is None:
                        logger.error(
                            "Snapshot %s has missing parent %s.",
                            physical_ancestor.snapshot_id,
                            physical_ancestor.parent_id,
                        )
                        return False
                    physical_ancestor = parent

            success, elapsed = self._core_restore(
                physical_ancestor.snapshot_id,
                departing_state.branch_id,
            )
            if not success:
                logger.error("Failed to restore snapshot %s.", snapshot_id)
                return False

            active_branch_id = self._get_current_branch_id()
            self._stats.add_entry(
                "restore",
                physical_ancestor.snapshot_id,
                elapsed,
                branch_id=active_branch_id,
            )

            for command in replay_commands:
                rc, _, stderr = self.exec_command(command)
                if rc != 0:
                    logger.error("Replay failed: %s\n%s", command, stderr)

            self._set_branch_position(active_branch_id, snapshot_id)
            logger.info(
                "Restored snapshot %s on branch %s in %.4fs",
                snapshot_id,
                active_branch_id,
                elapsed,
            )
            return True

    @abstractmethod
    def _core_restore(
        self,
        snapshot_id: str,
        branch_id: str,
    ) -> tuple[bool, float]:
        """Make ``branch_id`` usable at the state stored by ``snapshot_id``.

        ``snapshot_id`` has already been validated and resolves to an
        immutable backend resource.  A sequential backend may replace its
        current process or container; a forkable backend may materialize or
        switch a live branch.  This hook must not update controller branch
        state, snapshot lineage, command logs, or benchmark records.

        Return ``(True, elapsed_seconds)`` only when subsequent commands can
        execute against the restored state.  Return ``(False,
        elapsed_seconds)`` otherwise, preserving a usable pre-restore
        environment where practical.  The template records benchmarks,
        replays commands, and updates the branch position after this hook.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Remaining public Template Methods
    # ------------------------------------------------------------------

    def cleanup(self) -> bool:
        """Clean up backend resources and return whether teardown succeeded."""
        with self._lifecycle_lock:
            logger.info("Cleaning up environment...")
            ok = self._core_cleanup()
            self.is_cleaned_up = True
            logger.info("Cleanup complete.")
            return ok is not False

    @abstractmethod
    def _core_cleanup(self) -> Optional[bool]:
        """Release every backend resource owned by this manager.

        The cleanup template calls this hook under the lifecycle lock.  A
        concrete implementation should tolerate partial initialization and
        repeated calls, and must not rely on controller registries already
        having been cleared.

        Return ``False`` when teardown fails in a way the caller should see.
        Return ``True`` or ``None`` after successful cleanup.
        """
        raise NotImplementedError

    def exec_command(
        self,
        command: List[str] | str,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        """Execute a command on the current branch."""
        with self._current_branch_lock:
            return self._exec_branch(
                self._get_current_branch_id(),
                command,
                timeout,
            )

    def _exec_branch(
        self,
        branch_id: str,
        command: List[str] | str,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        state = self._get_branch_state(branch_id)
        with state.operation_lock:
            with self._state_lock:
                if not state.active:
                    return -1, "", f"Branch {branch_id} is no longer active."
            start = time.time()
            try:
                returncode, stdout, stderr = self._core_exec(
                    command=command,
                    timeout=timeout,
                    branch_id=branch_id,
                )
            except Exception as exc:
                elapsed = time.time() - start
                with self._state_lock:
                    state.command_log.append(command)
                    target_snapshot = state.current_snapshot_id or "<none>"
                self._stats.add_entry(
                    "exec",
                    target_snapshot,
                    elapsed,
                    branch_id=branch_id,
                )
                logger.error("Execution failed on branch %s: %s", branch_id, exc)
                return -1, "", str(exc)

            elapsed = time.time() - start
            with self._state_lock:
                state.cumulative_exec_time += elapsed
                state.command_log.append(command)
                target_snapshot = state.current_snapshot_id or "<none>"
            self._stats.add_entry(
                "exec",
                target_snapshot,
                elapsed,
                branch_id=branch_id,
            )
            logger.info(
                "Exec finished on branch %s (rc=%s) in %.4fs",
                branch_id,
                returncode,
                elapsed,
            )
            return returncode, stdout, stderr

    def _core_exec(
        self,
        command: List[str] | str,
        timeout: Optional[float],
        branch_id: str,
    ) -> tuple[int, str, str]:
        logger.warning("_core_exec not implemented in %s backend.", self.backend_name)
        return -1, "", "Not implemented."

    def list_snapshots(self) -> List[str]:
        with self._state_lock:
            return list(self.snapshots.keys())

    def print_snapshot_tree(self) -> str:
        """Return a thread-safe view of the snapshot DAG and branch positions."""
        with self._state_lock:
            if not self.snapshot_graph:
                return "No snapshot tree available.\n"

            current_at: Dict[str, List[str]] = {}
            last_at: Dict[str, List[str]] = {}
            for branch_id, state in self._branches.items():
                if state.current_snapshot_id:
                    current_at.setdefault(state.current_snapshot_id, []).append(branch_id)
                if (
                    state.last_snapshot_id
                    and state.last_snapshot_id != state.current_snapshot_id
                ):
                    last_at.setdefault(state.last_snapshot_id, []).append(branch_id)

            lines = ["Snapshot Tree:"]

            def recurse(snapshot: str, indent: str = " ") -> None:
                markers = []
                if snapshot in current_at:
                    markers.append(f"current: {', '.join(sorted(current_at[snapshot]))}")
                if snapshot in last_at:
                    markers.append(f"last: {', '.join(sorted(last_at[snapshot]))}")
                suffix = f" ({'; '.join(markers)})" if markers else ""
                lines.append(f"{indent}- {snapshot}{suffix}")
                for child in self.snapshot_graph[snapshot].children:
                    recurse(child, indent + "  ")

            roots = [
                snapshot
                for snapshot, node in self.snapshot_graph.items()
                if node.parent_id is None
            ]
            if not roots:
                return "No snapshot tree available.\n"
            for root in roots:
                recurse(root)
            return "\n".join(lines) + "\n"

    @property
    def current_snapshot(self) -> Optional[str]:
        return self.current_snapshot_id

    @property
    def backend(self) -> str:
        return self.backend_name

    @property
    def stats(self) -> BenchmarkStats:
        return self._stats
