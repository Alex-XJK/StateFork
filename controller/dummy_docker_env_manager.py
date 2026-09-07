"""Architecture sketch for a forkable Docker backend.

This module is intentionally not exported by :mod:`controller`. It illustrates
how a future backend can plug Docker primitives into the shared templates; it
is not intended to be production-ready or attachable across manager restarts.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Optional

from decider import Decider

from .base_env_manager import DEFAULT_BRANCH_ID, SnapshotNode
from .forkable_env_manager import EnvironmentBranch, ForkableEnvironmentManager


@dataclass
class DockerBranch(EnvironmentBranch):
    """Docker-specific handle stored behind the generic branch interface."""

    container_name: str = ""


class DummyForkableDockerManager(ForkableEnvironmentManager[DockerBranch]):
    """Example: one live Docker container per StateFork branch."""

    def __init__(
        self,
        base_image: str,
        branch_args: Optional[Callable[[str], List[str]]] = None,
        decider: Optional[Decider] = None,
    ) -> None:
        super().__init__(backend_name="DummyDocker", decider=decider)
        self._manager_id = uuid.uuid4().hex[:8]
        self._image_repository = f"statefork-dummy-{self._manager_id}"
        self._branch_args = branch_args or (lambda _branch_id: [])

        self._register_snapshot_resource("base", base_image)
        self._record_snapshot_node(SnapshotNode("base", None))

        # Backend initialization is the only place that seeds the protected
        # default branch. Normal branch creation goes through fork().
        main = self._start_container(
            branch_id=DEFAULT_BRANCH_ID,
            snapshot_id="base",
            image_name=base_image,
        )
        self._live_branches[main.id] = main
        self._set_branch_position(main.id, "base")

    # ------------------------------------------------------------------
    # Docker helpers: these know nothing about graphs, stats, or branch locks.
    # ------------------------------------------------------------------

    def _branch(self, branch_id: str) -> DockerBranch:
        with self._state_lock:
            branch = self._live_branches.get(branch_id)
        if branch is None:
            raise KeyError(f"Docker branch {branch_id!r} is not live.")
        return branch

    def _start_container(
        self,
        branch_id: str,
        snapshot_id: str,
        image_name: str,
    ) -> DockerBranch:
        container_name = f"sf-{self._manager_id}-{branch_id}"
        command = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"statefork.manager={self._manager_id}",
            "--label",
            f"statefork.branch={branch_id}",
            "--label",
            f"statefork.base_snapshot={snapshot_id}",
            *self._branch_args(branch_id),
            image_name,
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        return DockerBranch(
            id=branch_id,
            base_snapshot_id=snapshot_id,
            container_name=container_name,
        )

    def _commit_container(
        self,
        branch_id: str,
    ) -> tuple[Optional[str], Optional[str], float]:
        branch = self._branch(branch_id)
        snapshot_id = uuid.uuid4().hex[:12]
        image_name = f"{self._image_repository}:{snapshot_id}"
        start = time.time()
        try:
            subprocess.run(
                ["docker", "commit", branch.container_name, image_name],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return None, None, time.time() - start
        return snapshot_id, image_name, time.time() - start

    # ------------------------------------------------------------------
    # EnvironmentManager hooks
    # ------------------------------------------------------------------

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        snapshot_id, image_name, elapsed = self._commit_container(branch_id)
        if snapshot_id is None or image_name is None:
            return None, elapsed
        self._register_snapshot_resource(snapshot_id, image_name)
        return snapshot_id, elapsed

    def _core_exec(
        self,
        command: List[str] | str,
        timeout: Optional[float],
        branch_id: str,
    ) -> tuple[int, str, str]:
        container_name = self._branch(branch_id).container_name
        docker_command = ["docker", "exec", container_name]
        if isinstance(command, str):
            docker_command.extend(["bash", "-lc", command])
        else:
            docker_command.extend(command)

        result = subprocess.run(
            docker_command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr

    def _core_cleanup(self) -> bool:
        ok = True
        for branch in self._core_list_branches() or []:
            result = subprocess.run(
                ["docker", "rm", "-f", branch.container_name],
                capture_output=True,
                text=True,
            )
            ok = result.returncode == 0 and ok

        for snapshot_id, image_name in self._snapshot_items():
            if snapshot_id == "base":
                continue  # The caller owns the supplied base image.
            result = subprocess.run(
                ["docker", "rmi", image_name],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self._remove_snapshot_resource(snapshot_id)
            else:
                ok = False
        return ok

    # ------------------------------------------------------------------
    # ForkableEnvironmentManager hooks
    # ------------------------------------------------------------------

    def _core_fork(
        self,
        snapshot_id: str,
        requested_ids: List[Optional[str]],
    ) -> List[tuple[DockerBranch, float]]:
        image_name = self._snapshot_resource(snapshot_id)
        if image_name is None:
            return []

        def create(requested_id: Optional[str]):
            branch_id = requested_id or f"branch-{uuid.uuid4().hex[:8]}"
            start = time.time()
            try:
                branch = self._start_container(branch_id, snapshot_id, image_name)
            except subprocess.CalledProcessError:
                return None
            return branch, time.time() - start

        # Parallel creation is a backend choice; the base template still owns
        # validation, registration, statistics, and failure omission.
        with ThreadPoolExecutor(max_workers=len(requested_ids)) as pool:
            outcomes = list(pool.map(create, requested_ids))
        return [outcome for outcome in outcomes if outcome is not None]

    def _core_discard_branch(self, branch_id: str) -> bool:
        container_name = self._branch(branch_id).container_name
        result = subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _core_park_branch(self, branch_id: str) -> tuple[Optional[str], float]:
        snapshot_id, image_name, elapsed = self._commit_container(branch_id)
        if snapshot_id is None or image_name is None:
            return None, elapsed

        # Parking succeeds only when both persistence and retirement succeed.
        if not self._core_discard_branch(branch_id):
            subprocess.run(
                ["docker", "rmi", image_name],
                capture_output=True,
                text=True,
            )
            return None, elapsed

        self._register_snapshot_resource(snapshot_id, image_name)
        return snapshot_id, elapsed

    def _core_list_branches(self) -> Optional[List[DockerBranch]]:
        """Discover containers; a real attachable backend needs durable lineage."""
        try:
            container_ids = subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=statefork.manager={self._manager_id}",
                ],
                text=True,
            ).split()
            if not container_ids:
                return []
            inspections = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", *container_ids],
                    text=True,
                )
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return None

        branches = []
        for inspection in inspections:
            labels = inspection["Config"].get("Labels") or {}
            branch_id = labels.get("statefork.branch")
            if not branch_id:
                continue

            # Docker labels are immutable after creation. This non-attachable
            # sketch therefore prefers the current controller lineage; a real
            # backend would persist updated branch metadata in a sidecar store.
            with self._state_lock:
                known = self._branches.get(branch_id)
                base_snapshot_id = (
                    known.last_snapshot_id
                    if known is not None and known.last_snapshot_id is not None
                    else labels.get("statefork.base_snapshot", "")
                )
            branches.append(
                DockerBranch(
                    id=branch_id,
                    base_snapshot_id=base_snapshot_id,
                    status="running" if inspection["State"]["Running"] else "stopped",
                    container_name=inspection["Name"].lstrip("/"),
                )
            )
        return branches


# Notice what is deliberately absent: restore(). The inherited forkable
# Template Method materializes the target, switches current_branch_id, and
# discards the departing non-main container using the hooks above.
