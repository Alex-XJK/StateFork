from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, List
from .base_env_manager import DEFAULT_BRANCH_ID, SnapshotNode
from .forkable_env_manager import EnvironmentBranch, ForkableEnvironmentManager
from .benchmark import Calculator
from decider import Decider

logger = logging.getLogger("EnvManager.Waypoint")

STATEFORK_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _resolve_bin(name: str, env_var: str) -> str:
    """Locate a Waypoint helper binary.

    Resolution order: an explicit ``env_var`` override, then a binary of the
    same name found on ``PATH``, then a repo-local fallback at
    ``STATEFORK_ROOT/<name>`` (typically a developer-created symlink). The path
    is returned even if it does not exist; executability is validated lazily in
    ``_run_waypoint`` so that importing this module never requires Waypoint to
    be installed (other backends must stay usable without it).
    """
    return (
        os.environ.get(env_var)
        or shutil.which(name)
        or os.path.join(STATEFORK_ROOT, name)
    )


WAYPOINT_BIN = _resolve_bin("waypoint", "WAYPOINT_BIN")


def _run_waypoint(args: list[str], **kwargs):
    if not (os.path.isfile(WAYPOINT_BIN) and os.access(WAYPOINT_BIN, os.X_OK)):
        raise FileNotFoundError(
            f"Waypoint binary not found or not executable: {WAYPOINT_BIN}. "
            "Install Waypoint (https://github.com/Alex-XJK/waypoint) and either "
            "put it on PATH, set the WAYPOINT_BIN environment variable, or "
            f"symlink it into {STATEFORK_ROOT}."
        )
    # Waypoint needs root (buildah overlay mount, CRIU, overlay/chroot). Callers
    # (e.g. Harbor) request privilege escalation via WAYPOINT_CMD_PREFIX, e.g.
    # "sudo -n -E". Empty/unset => run the binary directly (unchanged behavior).
    prefix = shlex.split(os.environ.get("WAYPOINT_CMD_PREFIX", ""))
    return subprocess.run(
        [*prefix, WAYPOINT_BIN, *args],
        cwd=STATEFORK_ROOT,
        **kwargs,
    )


# The `main` fork is the first fork of every session, created by `init --shell`
# or `build`. It is the default environment a manager operates on.
MAIN_FORK_ID = DEFAULT_BRANCH_ID

_forking_supported: Optional[bool] = None


def _assert_supports_forking() -> None:
    """Fail fast unless the resolved Waypoint speaks the fork-based CLI.

    StateFork's Waypoint backend targets the concurrent-forking model
    (``fork`` / ``checkpoint`` / ``snapshot`` / ``exec <fork> -- ...``).
    The result is cached so repeated manager construction costs at most one probe.
    """
    global _forking_supported
    if _forking_supported:
        return
    if not (os.path.isfile(WAYPOINT_BIN) and os.access(WAYPOINT_BIN, os.X_OK)):
        raise FileNotFoundError(
            f"Waypoint binary not found or not executable: {WAYPOINT_BIN}. "
            "Install Waypoint (https://github.com/Alex-XJK/waypoint) and either "
            "put it on PATH, set the WAYPOINT_BIN environment variable, or "
            f"symlink it into {STATEFORK_ROOT}."
        )
    # The usage banner needs no privileges, so probe the binary directly
    # (bypass the sudo prefix). No args => prints usage to stdout, exits 1.
    proc = subprocess.run(
        [WAYPOINT_BIN],
        cwd=STATEFORK_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    usage = proc.stdout or ""
    if "Materialize live fork" not in usage and "fork <session>" not in usage:
        raise RuntimeError(
            f"The Waypoint binary at {WAYPOINT_BIN} does not support concurrent "
            "forking (no `fork` command). StateFork's Waypoint backend now "
            "requires the fork-based Waypoint. Update the binary or point "
            "WAYPOINT_BIN at a fork-capable build."
        )
    _forking_supported = True


def _cpe_detail(e: subprocess.CalledProcessError) -> str:
    """Waypoint prints its error cause to stdout/stderr; surface it in logs."""
    detail = ((e.stderr or "") + (e.stdout or "")).strip()
    return f"{e}" + (f" :: {detail}" if detail else "")


@dataclass(init=False)
class WaypointFork(EnvironmentBranch):
    """A live fork of a checkpoint: a running shell with its own private state.

    ``restore_duration`` is Waypoint's own measurement of the materialization
    time, as reported on the ``waypoint fork`` output line (e.g. ``"312ms"``).
    """
    pid: int = 0
    socket: str = ""
    restore_duration: Optional[str] = None

    def __init__(
        self,
        id: str,
        pid: int = 0,
        socket: str = "",
        base_checkpoint: str = "",
        restore_duration: Optional[str] = None,
        status: str = "running",
        *,
        base_snapshot_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            id=id,
            base_snapshot_id=(
                base_snapshot_id
                if base_snapshot_id is not None
                else base_checkpoint
            ),
            status=status,
        )
        self.pid = pid
        self.socket = socket
        self.restore_duration = restore_duration

    @property
    def base_checkpoint(self) -> str:
        """Compatibility alias for original public record field."""
        return self.base_snapshot_id

    @base_checkpoint.setter
    def base_checkpoint(self, snapshot_id: str) -> None:
        self.base_snapshot_id = snapshot_id


class WaypointCalculator(Calculator):
    """
    WaypointCalculator is a specialized FileSizeCalculator for Waypoint that
    collects the sizes of filesystem ("upper") and memory ("criu") images under
    every checkpoint in a session's ``checkpoints/`` directory.

    We have to override but not extend the FileSizeCalculator because we need to
    target the per-checkpoint subdirectory structure of the fork-based Waypoint.
    """
    def __init__(self, root_dir: str, sub_dir: str, name: str = "WaypointFsCalculator"):
        super().__init__(name=name)
        self.root_dir = os.path.abspath(root_dir)
        self.sub_dir = sub_dir  # either "upper" or "criu"
        self.logger.debug(f"Attached WaypointCalculator #{self.instance_id} to {self.root_dir}/*/{self.sub_dir}")

    def __get_all_items(self) -> List[str]:
        if not os.path.exists(self.root_dir):
            return []
        items = []
        for name in os.listdir(self.root_dir):
            if name in ["metadata", "work", "temp"]:
                continue
            sub_path = os.path.join(self.root_dir, name, self.sub_dir)
            if os.path.exists(sub_path):
                items.append(sub_path)
        return items

    def __get_size(self, path: str) -> int:
        try:
            output = subprocess.check_output(["du", "-sb", path], text=True)
            return int(output.split()[0])
        except Exception as e:
            self.logger.error(f"Error getting size for {path}: {e}")
            return 0

    def _collect(self) -> List[tuple[str, int]]:
        items = self.__get_all_items()
        if not items:
            return []

        data = []
        for item in items:
            size = self.__get_size(item)
            if size >= 0:
                parts = os.path.normpath(item).split(os.sep)
                name = os.path.join(parts[-2], parts[-1])
                data.append((name, size))
        return data

class WaypointAttachManager(ForkableEnvironmentManager[WaypointFork]):
    """
    Attach to an existing Waypoint session and manage its checkpoints/forks.

    The inherited ``snapshot()`` and ``exec_command()`` Template Methods act on
    the current branch (initially ``main``). Named operations are provided by
    the stronger forkable capability: ``snapshot_branch()``,
    ``exec_on_branch()``, ``park_branch()``, ``fork()``, and
    ``discard_branch()``.

    :meth:`restore` moves the current branch: an **explicit two-primitive
    macro** -- the target snapshot is materialized as a fresh fork that
    becomes current, then the departing fork is destroyed. Its un-sealed
    state is discarded *by contract*: seal it first (``snapshot()``, or
    ``park`` for a lossless retire) if you want to keep it; ``main`` is never
    destroyed and simply stays live. The target is materialized before
    anything is destroyed, so a failed restore leaves the current environment
    untouched. Additional live materialization is exposed only through the
    stronger :class:`ForkableEnvironmentManager` capability.
    """

    def __init__(self,
                 session_id: str,
                 target_pid: Optional[int] = None,
                 decider: Optional[Decider] = None,
                 ):
        super().__init__(backend_name="Waypoint", decider=decider)
        self.session_id = session_id
        # target_pid is obsolete in the fork model (a fork *is* its process
        # tree); accepted but ignored so older callers keep working.
        if target_pid is not None:
            logger.debug("target_pid is deprecated and ignored in the fork-based Waypoint model.")

        try:
            _assert_supports_forking()
        except Exception:
            # Nothing was created on the Waypoint side; skip __del__ cleanup.
            self.is_cleaned_up = True
            raise

        logger.info(f"Attaching to Waypoint session {self.session_id}...")

        # Inherit whatever already exists on disk: the checkpoint DAG and the
        # live fork registry. Attaching to a session created elsewhere thus
        # starts with its full history rather than a blank tree.
        self.sync_snapshot_tree()
        self.list_branches()

        # The initial snapshot parents onto whatever the current fork already
        # sits on (None when the session had no checkpoints yet).
        with self._state_lock:
            record = self._live_branches.get(self.current_branch_id)
        parent_id = (record.base_checkpoint or None) if record else None

        sid, _ = self._core_snapshot(self.current_branch_id)
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init/extend the Tree Graph
        with self._state_lock:
            snapshot_known = sid in self.snapshot_graph
        if not snapshot_known:
            self._record_snapshot_node(
                SnapshotNode(snapshot_id=sid, parent_id=parent_id)
            )
        self._set_branch_position(self.current_branch_id, sid)

        with self._state_lock:
            current_branch_known = self.current_branch_id in self._live_branches
        if not current_branch_known:
            # Registry unavailable (e.g. `list` failed); keep a minimal record
            # so lineage bookkeeping still works.
            with self._state_lock:
                self._live_branches[self.current_branch_id] = WaypointFork(
                    id=self.current_branch_id,
                    base_snapshot_id=sid,
                )

    def _snapshot_waypoint(
        self,
        branch_id: str,
        *,
        park: bool,
    ) -> tuple[Optional[str], float]:
        """Issue the one-command Waypoint snapshot primitive."""
        snapshot_id = str(uuid.uuid4())[:8]
        args = ["snapshot", self.session_id, branch_id, snapshot_id]
        if park:
            args.append("--park")
        start = time.time()
        try:
            _run_waypoint(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Waypoint snapshot of branch {branch_id} failed: {_cpe_detail(e)}"
            )
            return None, 0.0
        elapsed = time.time() - start
        self._register_snapshot_resource(snapshot_id, snapshot_id)
        return snapshot_id, elapsed

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        snapshot_id, elapsed = self._snapshot_waypoint(branch_id, park=False)
        if snapshot_id is not None:
            # A destructive Waypoint snapshot resumes the branch under a fresh
            # PID/socket, so refresh the live branch registry.
            self.list_branches()
        return snapshot_id, elapsed

    def _core_park_branch(self, branch_id: str) -> tuple[Optional[str], float]:
        return self._snapshot_waypoint(branch_id, park=True)

    def _core_cleanup(self) -> bool:
        logger.info("Shutting down Waypoint environment...")
        try:
            _run_waypoint(
                ["cleanup", self.session_id],
                check=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint cleanup failed: {e}")
            logger.info("Attempting force cleanup...")
            try:
                _run_waypoint(
                    ["cleanup", self.session_id, "--force"],
                    check=True,
                )
                return True
            except subprocess.CalledProcessError as e:
                logger.error(f"Waypoint force cleanup failed: {e}")
                return False

    def _core_exec(
        self,
        command: List[str] | str,
        timeout: Optional[float],
        branch_id: str,
    ) -> tuple[int, str, str]:
        return self._exec_raw(branch_id, command, timeout)

    def _core_fork(
        self,
        snapshot_id: str,
        requested_ids: List[Optional[str]],
    ) -> List[tuple[WaypointFork, float]]:
        """
        Materialize Waypoint forks in parallel.

        Validation, controller registration, and benchmarking are handled by
        ``ForkableEnvironmentManager.fork``; concurrent subprocess execution
        and output parsing remain backend primitives.
        """
        def materialize(fork_id: Optional[str]) -> Optional[tuple[WaypointFork, float]]:
            args = ["fork", self.session_id, snapshot_id]
            if fork_id:
                args += ["--id", fork_id]
            start = time.time()
            try:
                proc = _run_waypoint(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                logger.error(f"Waypoint fork failed: {_cpe_detail(e)}")
                return None
            elapsed = time.time() - start
            fork = self._parse_fork_line(proc.stdout, snapshot_id)
            if fork is None:
                logger.error(f"Could not parse fork output: {proc.stdout!r}")
                return None
            return fork, elapsed

        results: List[tuple[WaypointFork, float]] = []
        # Different forks take different Waypoint locks, so materialization is
        # safe to run concurrently.
        with ThreadPoolExecutor(max_workers=min(len(requested_ids), 16)) as pool:
            for outcome in pool.map(materialize, requested_ids):
                if outcome is None:
                    continue
                results.append(outcome)
        return results

    def _exec_raw(self, fork_id: str, command: List[str] | str,
                  timeout: Optional[float] = None) -> tuple[int, str, str]:
        """One `waypoint exec` in a fork's persistent shell -> (rc, stdout, stderr)."""
        if not self.session_id:
            return -1, "", "No session_id available"

        cmd_str = command if isinstance(command, str) else shlex.join(command)

        try:
            proc = _run_waypoint(
                ["exec", self.session_id, fork_id, "--", cmd_str],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            err = (e.stderr or "") + f"\n[timeout after {timeout}s]"
            logger.error(f"Waypoint exec timeout: {e}")
            return -2, out, err  # -2 = TIMEOUT; -1 = backend failure
        except Exception as e:
            logger.error(f"Waypoint exec failed: {e}")
            return -1, "", str(e)

    def _core_copy(
        self,
        branch_id: str,
        host_path: str,
        env_path: str,
        into: bool,
    ) -> tuple[bool, str]:
        """One `waypoint cp` between the host and a live fork.

        Waypoint performs the copy from inside the fork's namespaces, so the
        in-fork path resolves in the fork's own filesystem view: an absolute
        symlink cannot redirect the transfer at the host. It also takes the
        fork's operation lock, so the copy cannot interleave with a snapshot
        that would move the fork to a new pid.
        """
        if not self.session_id:
            return False, "No session_id available"

        fork_ref = f"{branch_id}:{env_path}"
        args = (
            ["cp", self.session_id, host_path, fork_ref]
            if into
            else ["cp", self.session_id, fork_ref, host_path]
        )
        try:
            _run_waypoint(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            return True, ""
        except subprocess.CalledProcessError as e:
            return False, _cpe_detail(e)
        except Exception as e:
            return False, str(e)

    def _core_discard_branch(self, branch_id: str) -> bool:
        """Destroy one Waypoint fork and its private layers."""
        try:
            _run_waypoint(
                ["destroy", self.session_id, branch_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Waypoint destroy of branch {branch_id} failed: {_cpe_detail(e)}"
            )
            return False
        return True

    def sync_snapshot_tree(self) -> int:
        """
        Hydrate the snapshot tree from the session's checkpoint DAG on disk.

        `waypoint list --json` reports every checkpoint with its parent, so a
        manager attaching to an existing session inherits that history instead
        of starting from a blank tree (without this, sealing a fork created by
        someone else would produce a node whose parent is unknown -- forkable,
        but invisible in print_snapshot_tree()). Locally known nodes are left
        untouched. Returns the number of nodes added.
        """
        try:
            proc = _run_waypoint(
                ["list", self.session_id, "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            listing = json.loads(proc.stdout)
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint list failed: {_cpe_detail(e)}")
            return 0
        except json.JSONDecodeError as e:
            logger.error(f"Waypoint list returned invalid JSON: {e}")
            return 0

        entries = [c for c in (listing.get("checkpoints") or []) if c.get("id")]

        # Pass 1: create the nodes. Parents are not guaranteed to appear before
        # their children in the listing, so links are deferred to pass 2.
        added = 0
        for entry in entries:
            cid = entry["id"]
            self._register_snapshot_resource(cid, cid)
            with self._state_lock:
                known = cid in self.snapshot_graph
            if not known:
                self._record_snapshot_node(
                    SnapshotNode(
                        snapshot_id=cid,
                        parent_id=entry.get("parent_id") or None,
                    )
                )
                added += 1

        # Pass 2: link children (skipping parents outside this graph, and
        # tolerating a re-sync of nodes that are already linked).
        with self._state_lock:
            for entry in entries:
                node = self.snapshot_graph[entry["id"]]
                if node.parent_id and node.parent_id in self.snapshot_graph:
                    siblings = self.snapshot_graph[node.parent_id].children
                    if node.snapshot_id not in siblings:
                        siblings.append(node.snapshot_id)

        if added:
            logger.info(f"Hydrated {added} checkpoint(s) from session {self.session_id}.")
        return added

    def _core_list_branches(self) -> Optional[List[WaypointFork]]:
        """Read Waypoint's live-fork registry from ``waypoint list --json``."""
        try:
            proc = _run_waypoint(
                ["list", self.session_id, "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            listing = json.loads(proc.stdout)
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint list failed: {_cpe_detail(e)}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Waypoint list returned invalid JSON: {e}")
            return None

        refreshed: List[WaypointFork] = []
        for entry in listing.get("forks") or []:
            fork_id = entry.get("id")
            if not fork_id:
                continue
            with self._state_lock:
                known = self._live_branches.get(fork_id)
            refreshed.append(
                WaypointFork(
                    id=fork_id,
                    base_snapshot_id=(
                        entry.get("base_checkpoint_id")
                        or (known.base_snapshot_id if known else "")
                    ),
                    status=entry.get("status", "running"),
                    pid=entry.get("pid", 0),
                    socket=entry.get("socket_path", ""),
                    restore_duration=(
                        entry.get("restore_duration")
                        or (known.restore_duration if known else None)
                    ),
                )
            )
        return refreshed

    @staticmethod
    def _parse_fork_line(output: str, base_checkpoint: str) -> Optional[WaypointFork]:
        """Parse a `waypoint fork` line: `<id> pid=<p> socket=<s> duration=<d> [...]`."""
        lines = [ln for ln in output.strip().splitlines() if ln.strip()]
        if not lines:
            return None
        fields = lines[-1].split()
        kv = {}
        for field in fields[1:]:
            key, sep, value = field.partition("=")
            if sep and key not in kv:  # first occurrence wins over breakdown fields
                kv[key] = value
        try:
            pid = int(kv.get("pid", "0"))
        except ValueError:
            pid = 0
        return WaypointFork(
            id=fields[0],
            base_snapshot_id=base_checkpoint,
            pid=pid,
            socket=kv.get("socket", ""),
            restore_duration=kv.get("duration"),
        )


class WaypointBuildManager(WaypointAttachManager):
    """
    WaypointBuildManager is a specialized Waypoint EnvironmentManager that builds a new session.
    """
    def __init__(self,
                 dockerfile_dir: str = ".",
                 build: bool = True,
                 decider: Optional[Decider] = None,
                 ):
        # Validate policy before `build` / `init` creates a session.
        self._validate_decider(decider)
        # Probe before creating anything so an incompatible binary cannot
        # leave a half-built session behind.
        _assert_supports_forking()

        if dockerfile_dir is None:
            target_dir = os.getcwd()
        else:
            target_dir = os.path.abspath(dockerfile_dir)

        logger.info("Creating a new Waypoint session...")
        if not build:
            # --shell starts the live `main` fork; without it the session has
            # no process to checkpoint or exec into.
            init_process = _run_waypoint(
                ["init", target_dir, "--shell", "--quiet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            output = init_process.stdout.strip()
            try:
                sid, self._work_dir = output.split(",", 1)
            except ValueError:
                raise RuntimeError(f"Unexpected output format: {output}")
        else:
            init_process = _run_waypoint(
                ["build", target_dir, "--quiet"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            output = init_process.stdout.strip()
            try:
                sid, self._work_dir, _ = output.split(",", 2)
            except ValueError:
                raise RuntimeError(f"Unexpected output format: {output}")

        logger.info(f"New session {sid} with work directory '{self._work_dir}' created.")

        super().__init__(session_id=sid, decider=decider)

        # Attach the new WaypointCalculator to this session's checkpoint store
        checkpoints_dir = os.path.abspath(os.path.join(self._work_dir, "..", "checkpoints"))
        self._stats.attach_size_calculator(WaypointCalculator(checkpoints_dir, "upper", name="FILESYSTEM"))
        self._stats.attach_size_calculator(WaypointCalculator(checkpoints_dir, "criu", name="MEMORY"))

    @property
    def work_dir(self) -> str:
        """The session's canonical overlay mountpoint.

        **Not a host-side file transfer target.** Every fork — ``main``
        included — re-mounts its own private overlay at this same path inside
        its own mount namespace, with propagation disabled, so on the host this
        directory is empty. Copying into it succeeds (``cp`` returns 0) while no
        sandbox can see the result, and reading from it finds nothing. Use
        :meth:`copy_in` / :meth:`copy_out` (or the ``*_branch`` variants) to move
        files between the host and a fork.

        Kept for diagnostics and for locating the session's checkpoint store.
        """
        return self._work_dir
