from __future__ import annotations

import logging
import threading
import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import Generic, List, Optional, TypeVar

from decider import AlwaysTrueDecider, Decider

from .base_env_manager import (
    DEFAULT_BRANCH_ID,
    EnvironmentManager,
    SnapshotNode,
)


logger = logging.getLogger("EnvManager.Forkable")


@dataclass
class EnvironmentBranch:
    """Backend-neutral public description of a live environment branch."""

    id: str
    base_snapshot_id: str
    status: str = "running"


BranchT = TypeVar("BranchT", bound=EnvironmentBranch)


class ForkableEnvironmentManager(EnvironmentManager, Generic[BranchT]):
    """Stronger capability for managers with multiple live environments.

    The first implementation intentionally supports physical snapshots only.
    This keeps branching semantics explicit while virtual-snapshot promotion and
    per-branch decision policies remain undefined.
    """

    def __init__(
        self,
        backend_name: str,
        decider: Optional[Decider] = None,
        *,
        restore_discard_first: bool = False,
    ) -> None:
        self._validate_decider(decider)
        super().__init__(
            backend_name=backend_name,
            decider=decider if decider is not None else AlwaysTrueDecider(),
        )
        self._live_branches: dict[str, BranchT] = {}
        self._branch_registry_lock = threading.RLock()
        #: Which way :meth:`_core_restore` orders its two primitives. The
        #: default materializes the target first, so a failed restore leaves
        #: the current environment untouched. See
        #: :meth:`_core_restore_discard_first` for when to invert that, and
        #: what it costs.
        self.restore_discard_first = bool(restore_discard_first)

    @staticmethod
    def _validate_decider(decider: Optional[Decider]) -> None:
        """Validate before a build manager creates any backend resources."""
        if decider is not None and not isinstance(decider, AlwaysTrueDecider):
            raise ValueError(
                "Forkable environment managers currently require "
                "AlwaysTrueDecider (physical snapshots only)."
            )

    @property
    def current_branch_id(self) -> str:
        """Return the branch targeted by the inherited public operations."""
        return self._get_current_branch_id()

    @property
    def current_fork_id(self) -> str:
        """Compatibility alias for the backend-neutral current branch id."""
        return self.current_branch_id

    @property
    def live_branches(self) -> List[BranchT]:
        with self._state_lock:
            return list(self._live_branches.values())

    @property
    def live_forks(self) -> List[BranchT]:
        """Compatibility alias for the earlier fork-oriented API."""
        return self.live_branches

    def snapshot_branch(
        self,
        branch_id: str,
        *,
        park: bool = False,
    ) -> Optional[str]:
        """Snapshot a named live branch without changing the base API."""
        if park:
            return self.park_branch(branch_id)
        with self._state_lock:
            if branch_id not in self._live_branches:
                logger.error("Branch %s not found.", branch_id)
                return None
        try:
            return self._snapshot_branch(branch_id, force_physical=True)
        except KeyError:
            logger.error("Branch %s is no longer registered.", branch_id)
            return None

    def exec_on_branch(
        self,
        branch_id: str,
        command: List[str] | str,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        """Execute a command on a named live branch."""
        with self._state_lock:
            if branch_id not in self._live_branches:
                return -1, "", f"Branch {branch_id} not found."
        try:
            return self._exec_branch(branch_id, command, timeout)
        except KeyError:
            return -1, "", f"Branch {branch_id} is no longer registered."

    def copy_in_branch(self, branch_id: str, host_src: str, path: str) -> bool:
        """Copy a host file or directory into a named live branch."""
        with self._state_lock:
            if branch_id not in self._live_branches:
                logger.error("Branch %s not found.", branch_id)
                return False
        try:
            return self._copy_branch(branch_id, host_src, path, into=True)
        except KeyError:
            logger.error("Branch %s is no longer registered.", branch_id)
            return False

    def copy_out_branch(self, branch_id: str, path: str, host_dst: str) -> bool:
        """Copy a file or directory out of a named live branch onto the host."""
        with self._state_lock:
            if branch_id not in self._live_branches:
                logger.error("Branch %s not found.", branch_id)
                return False
        try:
            return self._copy_branch(branch_id, host_dst, path, into=False)
        except KeyError:
            logger.error("Branch %s is no longer registered.", branch_id)
            return False

    def fork(
        self,
        snapshot_id: str,
        n: int = 1,
        ids: Optional[List[str]] = None,
    ) -> List[BranchT]:
        """Materialize one or more live branches from a physical snapshot."""
        with self._branch_registry_lock:
            return self._fork(snapshot_id, n=n, ids=ids)

    def _fork(
        self,
        snapshot_id: str,
        n: int = 1,
        ids: Optional[List[str]] = None,
    ) -> List[BranchT]:
        with self._state_lock:
            node = self.snapshot_graph.get(snapshot_id)
            if node is not None and node.is_virtual:
                logger.error(
                    "Snapshot %s is virtual; fork() needs a physical snapshot.",
                    snapshot_id,
                )
                return []
            if snapshot_id not in self.snapshots:
                logger.error("Snapshot %s not found.", snapshot_id)
                return []

        if ids is not None:
            if len(set(ids)) != len(ids):
                logger.error("Duplicate branch ids requested.")
                return []
            with self._state_lock:
                conflicts = set(ids) & (
                    set(self._live_branches) | {DEFAULT_BRANCH_ID}
                )
            if conflicts:
                logger.error(
                    "Branch ids already exist or are reserved: %s",
                    ", ".join(sorted(conflicts)),
                )
                return []
            n = len(ids)
        if n < 1:
            return []

        requested: List[Optional[str]] = list(ids) if ids else [None] * n
        results = self._materialize_branches(snapshot_id, requested)

        logger.info(
            "Materialized %d/%d branch(es) from snapshot %s",
            len(results),
            n,
            snapshot_id,
        )
        return results

    def _materialize_branches(
        self,
        snapshot_id: str,
        requested_ids: List[Optional[str]],
        *,
        activate: bool = True,
    ) -> List[BranchT]:
        """Run the backend fork primitive and atomically register its results."""
        with self._branch_registry_lock:
            outcomes = self._core_fork(snapshot_id, requested_ids)
            results: List[BranchT] = []
            for branch, elapsed in outcomes:
                state = self._register_branch(branch.id, branch.base_snapshot_id)
                with self._state_lock:
                    state.active = activate
                    self._live_branches[branch.id] = branch
                self._stats.add_entry(
                    "fork",
                    branch.id,
                    elapsed,
                    branch_id=branch.id,
                )
                results.append(branch)
            return results

    @abstractmethod
    def _core_fork(
        self,
        snapshot_id: str,
        requested_ids: List[Optional[str]],
    ) -> List[tuple[BranchT, float]]:
        """Materialize live branches from one physical snapshot.

        ``snapshot_id`` has already been validated as a registered physical
        snapshot.  ``requested_ids`` contains one item per requested branch:
        a string is the exact desired identifier and ``None`` asks the backend
        to allocate one.  The backend may create the branches concurrently.

        Return one ``(branch, elapsed_seconds)`` pair for each successful
        materialization, preferably in request order.  Each branch must carry
        its identifier, base snapshot, status, and any backend-specific
        details required by later operations.  Omit failed requests and clean
        up their partial backend resources.  This hook must not modify the
        controller registry, snapshot graph, or benchmark records; the
        template performs those updates.
        """
        raise NotImplementedError

    def _core_restore(
        self,
        snapshot_id: str,
        branch_id: str,
    ) -> tuple[bool, float]:
        """Restore by materializing the target before retiring the old branch.

        With ``restore_discard_first`` the two primitives are ordered the other
        way round; :meth:`_core_restore_discard_first` says why an environment
        would ask for that and what guarantee it gives up.
        """
        if self.restore_discard_first:
            return self._core_restore_discard_first(snapshot_id, branch_id)

        start = time.time()
        branches = self._materialize_branches(
            snapshot_id,
            [None],
            activate=False,
        )
        if not branches:
            logger.error(
                "Restore could not materialize %s; current branch unchanged.",
                snapshot_id,
            )
            return False, 0.0

        restored = branches[0]
        self._set_current_branch(restored.id)
        if branch_id not in (DEFAULT_BRANCH_ID, restored.id):
            if not self._discard_branch(branch_id):
                logger.warning(
                    "Could not discard departing branch %s; it remains live.",
                    branch_id,
                )

        return True, time.time() - start

    def _core_restore_discard_first(
        self,
        snapshot_id: str,
        branch_id: str,
    ) -> tuple[bool, float]:
        """Retire the departing branch first, then materialize into its id.

        The default order cannot restore a state whose own processes are still
        running beside it. The backend materializes the target next to the live
        branch, and every checkpointed listening socket is then bound a second
        time in the same network namespace, which CRIU refuses::

            criu/sk-inet.c:1059  inet: Can't bind inet socket (id 45):
                                 Address already in use

        So a task that left a server running cannot be restored at all, and it
        cannot be forked either: measured on waypoint v0.7.0 with nginx live,
        the checkpoint succeeds while every materialization beside it fails.
        Retiring first frees both the id and the port, and the same checkpoint
        then comes back in full (290 ms, all 57 nginx processes, answering).

        **The guarantee is inverted, and a caller must choose it knowingly.**
        The default order leaves the current environment untouched when
        materialization fails. This one destroys before it builds: if the
        target cannot be materialized back into the freed id, one attempt is
        made under a fresh id so the session still has something live; if that
        fails too, the session has no current branch and the caller must treat
        it as fatal.

        Only for a caller that owns the session sequentially: the departing
        branch's processes are killed before anything is put back, so no
        concurrent branch work may be in flight, and whatever was not
        checkpointed is gone.
        """
        start = time.time()
        # Validate the target while failing is still free — after the retire
        # below, there is nothing to go back to.
        with self._state_lock:
            node = self.snapshot_graph.get(snapshot_id)
            if node is not None and node.is_virtual:
                logger.error(
                    "Snapshot %s is virtual; a discard-first restore needs a "
                    "physical snapshot.",
                    snapshot_id,
                )
                return False, 0.0
            if snapshot_id not in self.snapshots:
                logger.error("Snapshot %s not found.", snapshot_id)
                return False, 0.0

        # Deliberately past the guards in `_discard_branch`: this retires the
        # current branch, and may retire the default one, because it is about
        # to put a branch back under that id.
        if not self._retire_branch(branch_id):
            logger.error(
                "Discard-first restore could not retire %s; nothing was "
                "destroyed and the current branch is unchanged.",
                branch_id,
            )
            return False, 0.0

        branches = self._materialize_branches(snapshot_id, [branch_id])
        if not branches:
            logger.error(
                "Discard-first restore retired %s but could not materialize "
                "%s back into that id; trying a fresh id.",
                branch_id,
                snapshot_id,
            )
            branches = self._materialize_branches(snapshot_id, [None])
            if not branches:
                logger.error(
                    "Discard-first restore of %s failed after %s was already "
                    "retired: this session has no live current branch.",
                    snapshot_id,
                    branch_id,
                )
                return False, 0.0
            logger.warning(
                "Discard-first restore landed on %s instead of %s.",
                branches[0].id,
                branch_id,
            )

        self._set_current_branch(branches[0].id)
        return True, time.time() - start

    def discard_branch(self, branch_id: str) -> bool:
        """Destroy a non-current live branch; any un-snapshotted state is lost."""
        with self._current_branch_lock:
            return self._discard_branch(branch_id)

    def _discard_branch(self, branch_id: str) -> bool:
        if branch_id == DEFAULT_BRANCH_ID:
            logger.error("Refusing to discard the default branch.")
            return False
        if branch_id == self.current_branch_id:
            logger.error("Refusing to discard the current branch.")
            return False
        return self._retire_branch(branch_id)

    def _retire_branch(self, branch_id: str) -> bool:
        """Destroy a live branch, with no policy about which branch it is.

        :meth:`_discard_branch` is the guarded entry point and is what a caller
        wants. This one is also reached from a discard-first restore, which
        retires the current branch — possibly the default branch — on purpose.
        """
        with self._state_lock:
            branch = self._live_branches.get(branch_id)
            state = self._branches.get(branch_id)
        if branch is None or state is None:
            logger.error("Branch %s not found.", branch_id)
            return False

        # Branch operations always precede registry changes. This matches the
        # snapshot path, whose backend hook may refresh the branch registry.
        with state.operation_lock:
            with self._branch_registry_lock:
                with self._state_lock:
                    if branch_id not in self._live_branches or not state.active:
                        logger.error("Branch %s is no longer registered.", branch_id)
                        return False
                if not self._core_discard_branch(branch_id):
                    return False
                with self._state_lock:
                    self._live_branches.pop(branch_id, None)
                self._remove_branch(branch_id)
        logger.info("Branch %s discarded.", branch_id)
        return True

    def destroy_fork(self, fork_id: str) -> bool:
        """Compatibility alias for :meth:`discard_branch`."""
        return self.discard_branch(fork_id)

    @abstractmethod
    def _core_discard_branch(self, branch_id: str) -> bool:
        """Destroy the backend environment for ``branch_id``.

        The template has verified that the branch is live, non-default, and
        not current, and calls this hook while branch operations are
        serialized.  Destroy the live environment and resources private to
        it, but do not edit controller registries or benchmark records.

        Return ``True`` only when the backend branch is gone.  Return
        ``False`` on failure so the template keeps the branch registered and
        the caller can retry.
        """
        raise NotImplementedError

    def park_branch(self, branch_id: str) -> Optional[str]:
        """Snapshot a branch without resuming it, then remove the live branch."""
        with self._current_branch_lock:
            return self._park_branch(branch_id)

    def _park_branch(self, branch_id: str) -> Optional[str]:
        if branch_id == DEFAULT_BRANCH_ID:
            logger.error("Refusing to park the default branch.")
            return None

        with self._state_lock:
            branch = self._live_branches.get(branch_id)
            state = self._branches.get(branch_id)
        if branch is None or state is None:
            logger.error("Branch %s not found.", branch_id)
            return None

        with state.operation_lock:
            with self._branch_registry_lock:
                with self._state_lock:
                    if branch_id not in self._live_branches or not state.active:
                        logger.error("Branch %s is no longer registered.", branch_id)
                        return None
                    parent_id = state.last_snapshot_id
                snapshot_id, elapsed = self._core_park_branch(branch_id)
                if snapshot_id is None:
                    logger.error("Failed to park branch %s.", branch_id)
                    return None

                self._record_snapshot_node(
                    SnapshotNode(snapshot_id=snapshot_id, parent_id=parent_id)
                )
                self._stats.add_entry(
                    "park",
                    snapshot_id,
                    elapsed,
                    branch_id=branch_id,
                )

                if branch_id == self.current_branch_id:
                    self._set_current_branch(DEFAULT_BRANCH_ID)
                with self._state_lock:
                    self._live_branches.pop(branch_id, None)
                self._remove_branch(branch_id)

        logger.info(
            "Branch %s parked as %s in %.4fs",
            branch_id,
            snapshot_id,
            elapsed,
        )
        return snapshot_id

    @abstractmethod
    def _core_park_branch(self, branch_id: str) -> tuple[Optional[str], float]:
        """Persist ``branch_id`` and remove its live backend environment.

        Capture the branch's current state as a new immutable physical
        snapshot without resuming it.  On success, destroy the live branch and
        register the snapshot resource with
        :meth:`_register_snapshot_resource` before returning.  Do not update
        the snapshot graph, live-branch registry, or benchmark records.

        Return ``(snapshot_id, elapsed_seconds)`` on success and ``(None,
        elapsed_seconds)`` on failure.  When failing, leave the branch live and
        usable where practical.
        """
        raise NotImplementedError

    def list_branches(self) -> List[BranchT]:
        """Refresh and return the backend's live branch registry."""
        with self._branch_registry_lock:
            return self._list_branches()

    def _list_branches(self) -> List[BranchT]:
        refreshed = self._core_list_branches()
        if refreshed is None:
            return self.live_branches

        with self._state_lock:
            previous_ids = set(self._live_branches)
            self._live_branches = {branch.id: branch for branch in refreshed}

        for branch in refreshed:
            state = self._register_branch(branch.id, branch.base_snapshot_id or None)
            if (
                branch.base_snapshot_id
                and state.current_snapshot_id != branch.base_snapshot_id
            ):
                # The backend is authoritative when another manager advanced a
                # branch. Any local replay state no longer describes that head.
                self._set_branch_position(branch.id, branch.base_snapshot_id)

        refreshed_ids = {branch.id for branch in refreshed}
        for stale_id in previous_ids - refreshed_ids:
            if stale_id not in (DEFAULT_BRANCH_ID, self.current_branch_id):
                self._remove_branch(stale_id)
        return refreshed

    def list_forks(self) -> List[BranchT]:
        """Compatibility alias for :meth:`list_branches`."""
        return self.list_branches()

    @abstractmethod
    def _core_list_branches(self) -> Optional[List[BranchT]]:
        """Query the backend's authoritative set of live branches.

        Return fully populated branch records containing at least each branch
        identifier, base snapshot, and status.  An empty list is a successful
        query that found no branches; return ``None`` when the backend cannot
        be queried or its response cannot be decoded, allowing the template
        to retain its cached registry.  This hook must not mutate controller
        state itself.
        """
        raise NotImplementedError

    def _after_snapshot(self, branch_id: str, snapshot_id: str) -> None:
        with self._state_lock:
            branch = self._live_branches.get(branch_id)
            if branch is not None:
                branch.base_snapshot_id = snapshot_id
