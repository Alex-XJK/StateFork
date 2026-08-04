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
from typing import Dict, Optional, List
from .base_env_manager import EnvironmentManager, SnapshotNode
from .benchmark import Calculator
from decider import Decider, AlwaysTrueDecider

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
MAIN_FORK_ID = "main"

_forking_supported: Optional[bool] = None


def _assert_supports_forking() -> None:
    """Fail fast unless the resolved Waypoint speaks the fork-based CLI.

    StateFork's Waypoint backend targets the concurrent-forking model
    (``fork`` / ``checkpoint`` / ``snapshot`` / ``exec <fork> -- ...``).
    The fork-capable build still reports version ``v0.6.1``, so we cannot
    gate on the version string; instead we probe the usage banner for the
    ``fork`` subcommand, which only exists in the new model. The result is
    cached so repeated manager construction costs at most one probe.
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


@dataclass
class WaypointFork:
    """A live fork of a checkpoint: a running shell with its own private state.

    ``restore_duration`` is Waypoint's own measurement of the materialization
    time, as reported on the ``waypoint fork`` output line (e.g. ``"312ms"``).
    """
    id: str
    pid: int
    socket: str
    base_checkpoint: str
    restore_duration: Optional[str] = None
    status: str = "running"


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

class WaypointAttachManager(EnvironmentManager):
    """
    Attach to an existing Waypoint session and manage its checkpoints/forks.

    Single-verb design with a current branch: every primitive issues exactly
    one Waypoint command, and fork-targeting is an optional argument on the
    generic verbs rather than a parallel API. The manager maintains a *current
    fork* (initially ``main``) -- the branch the generic verbs act on --
    exposed as :attr:`current_fork_id`. ``snapshot()`` / ``exec_command()``
    default to it and accept ``fork_id=`` to target any live fork;
    ``snapshot(..., park=True)`` seals a fork *without* resuming it (cheapest
    persist; revive later with ``fork()``).

    :meth:`restore` moves the current branch: an **explicit two-primitive
    macro** -- the target snapshot is materialized as a fresh fork that
    becomes current, then the departing fork is destroyed. Its un-sealed
    state is discarded *by contract*: seal it first (``snapshot()``, or
    ``park`` for a lossless retire) if you want to keep it; ``main`` is never
    destroyed and simply stays live. The target is materialized before
    anything is destroyed, so a failed restore leaves the current environment
    untouched. ``create_env_from_snapshot()`` remains refused (redundant with
    ``fork()``, the only bare materialization verb).
    """

    def __init__(self,
                 session_id: str,
                 target_pid: Optional[int] = None,
                 decider: Optional[Decider] = None,
                 ):
        super().__init__(backend_name="Waypoint", decider=decider)
        self.session_id = session_id
        # The current branch: the fork the generic verbs act on.
        self._current_fork_id = MAIN_FORK_ID
        # Live forks known to this manager, keyed by fork id.
        self._live_forks: Dict[str, WaypointFork] = {}
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

        if not isinstance(self.decider, AlwaysTrueDecider):
            # Virtual snapshots restore fine (ancestor + replay), but only
            # physical snapshots can be fork()ed into parallel instances.
            logger.info(
                "Note: virtual snapshots restore via replay on this backend, "
                "but fork() accepts physical snapshots only."
            )

        logger.info(f"Attaching to Waypoint session {self.session_id}...")

        # Inherit whatever already exists on disk: the checkpoint DAG and the
        # live fork registry. Attaching to a session created elsewhere thus
        # starts with its full history rather than a blank tree.
        self.sync_snapshot_tree()
        self.list_forks()

        # The initial snapshot parents onto whatever the current fork already
        # sits on (None when the session had no checkpoints yet).
        record = self._live_forks.get(self._current_fork_id)
        parent_id = (record.base_checkpoint or None) if record else None

        sid, _ = self._core_snapshot()
        if sid is None:
            raise RuntimeError("Failed to create initial snapshot.")

        # Init/extend the Tree Graph
        if sid not in self.snapshot_graph:
            self.snapshot_graph[sid] = SnapshotNode(snapshot_id=sid, parent_id=parent_id)
            if parent_id and parent_id in self.snapshot_graph:
                self.snapshot_graph[parent_id].children.append(sid)
        self.current_snapshot_id = sid
        self.last_snapshot_id = sid

        if self._current_fork_id not in self._live_forks:
            # Registry unavailable (e.g. `list` failed); keep a minimal record
            # so lineage bookkeeping still works.
            self._live_forks[self._current_fork_id] = WaypointFork(
                id=self._current_fork_id, pid=0, socket="", base_checkpoint=sid,
            )

    # ===== Generic verbs (single-verb design: optional fork_id, default = current) =====

    @property
    def current_fork_id(self) -> str:
        """The current branch: the fork the generic verbs act on."""
        return self._current_fork_id

    def _core_restore(self, snapshot_id: str) -> tuple[bool, float]:
        """
        Move the current branch to a snapshot -- an explicit two-primitive macro:

        1. Materialize the target as a fresh fork (``fork``) and make it current.
        2. Destroy the departing fork. Its un-sealed state is DISCARDED by
           contract -- seal it first (snapshot(), or park for a lossless
           retire) if you want to keep it. ``main`` is never destroyed
           (session anchor); it simply stays live.

        The target is materialized *before* anything is destroyed, so a failed
        restore leaves the current environment untouched. The base class
        realigns lineage (current/last snapshot ids) after success and handles
        virtual snapshots (restore ancestor here, then replay).
        """
        start = time.time()

        forks = self.fork(snapshot_id, n=1)
        if not forks:
            logger.error(
                f"Restore could not materialize {snapshot_id}; "
                "current branch unchanged."
            )
            return False, 0.0

        departing = self._current_fork_id
        self._current_fork_id = forks[0].id
        if departing not in (MAIN_FORK_ID, forks[0].id):
            if not self.destroy_fork(departing):
                logger.warning(
                    f"Could not destroy departing fork {departing}; it stays "
                    "live (retire it with destroy_fork() or park it)."
                )

        return True, time.time() - start

    def create_env_from_snapshot(self, snapshot_id: str) -> Optional[str]:
        """
        Unsupported by design: on this backend materializing a snapshot IS
        forking, and one operation gets one name -- use :meth:`fork`.
        """
        logger.error(
            "create_env_from_snapshot is redundant on the fork-based Waypoint "
            f"backend: use fork('{snapshot_id}') -- the same operation under "
            "its native name."
        )
        return None

    def snapshot(self, fork_id: Optional[str] = None, park: bool = False) -> Optional[str]:
        """
        Seal a live fork into a new immutable snapshot (one waypoint command).

        - No arguments: the generic verb -- seals the *current* fork through
          the base template (Decider consulted, linear lineage). The fork is
          resumed on top of the new snapshot (note: its PID changes -- the
          backend snapshot is a destructive dump followed by a re-restore).
        - ``fork_id=``: seals that fork instead -- always physical; the new
          snapshot enters the tree as a child of the fork's previous base.
        - ``park=True``: seal *without resuming* (``snapshot --park``), the
          cheapest persist. The fork ceases to exist; its state survives as
          the snapshot and can be revived with fork(). Parking the current
          fork moves the current branch back to ``main``; ``main`` itself
          cannot be parked (session anchor).
        """
        target = fork_id if fork_id is not None else self._current_fork_id
        if park:
            return self._seal_fork(target, park=True)
        if target == self._current_fork_id:
            return super().snapshot()  # template path; _core_snapshot seals current
        return self._seal_fork(target, park=False)

    def _seal_fork(self, fork_id: str, park: bool) -> Optional[str]:
        """Named-fork seal (`waypoint snapshot [--park]`) + tree bookkeeping."""
        if park and fork_id == MAIN_FORK_ID:
            logger.error("Refusing to park `main`: the session needs its live anchor fork.")
            return None

        if fork_id not in self._live_forks:
            # It may exist on disk (another process/manager created it): resync
            # the fork registry AND the DAG, so the base checkpoint this
            # snapshot parents onto is present in the tree.
            self.sync_snapshot_tree()
            self.list_forks()
        record = self._live_forks.get(fork_id)
        if record is None:
            logger.error(f"Fork {fork_id} not found.")
            return None

        snapshot_id = str(uuid.uuid4())[:8]
        args = ["snapshot", self.session_id, fork_id, snapshot_id]
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
            logger.error(f"Waypoint snapshot of fork {fork_id} failed: {_cpe_detail(e)}")
            return None
        elapsed = time.time() - start

        self.snapshots[snapshot_id] = snapshot_id
        self._stats.add_entry("park" if park else "snapshot", snapshot_id, elapsed)

        # Graph: child of the fork's previous base checkpoint.
        parent_id = record.base_checkpoint or None
        self.snapshot_graph[snapshot_id] = SnapshotNode(snapshot_id=snapshot_id, parent_id=parent_id)
        if parent_id and parent_id in self.snapshot_graph:
            self.snapshot_graph[parent_id].children.append(snapshot_id)

        if park:
            # The fork is gone; only the snapshot remains.
            self._live_forks.pop(fork_id, None)
            if fork_id == self._current_fork_id:
                # Like a stash: state saved, branch returns to the anchor.
                self._current_fork_id = MAIN_FORK_ID
                main_rec = self._live_forks.get(MAIN_FORK_ID)
                base = (main_rec.base_checkpoint or None) if main_rec else None
                self.current_snapshot_id = base
                self.last_snapshot_id = base
                self._command_log = []
                self._cumulative_exec_time = 0.0
                logger.info("Current branch parked; the current fork is now `main`.")
            logger.info(f"Fork {fork_id} parked as {snapshot_id} in {elapsed:.4f}s")
        else:
            record.base_checkpoint = snapshot_id
            # The destructive snapshot resumed the fork under a fresh PID and
            # socket; refresh the registry so records stay accurate.
            self.list_forks()
            logger.info(f"Fork {fork_id} snapshotted as {snapshot_id} in {elapsed:.4f}s")
        return snapshot_id

    def exec_command(self, command: List[str] | str, timeout: Optional[float] = None,
                     fork_id: Optional[str] = None) -> tuple[int, str, str]:
        """
        Run one command in a live fork's persistent shell (one waypoint command).

        With no ``fork_id`` this is the generic verb: it runs in the *current*
        fork via the base template (benchmark log + command log + cumulative
        exec time). With ``fork_id=`` it runs in that fork; the call is timed
        into the benchmark log, but feeds neither the command log nor the
        decider (those model the current branch only). Commands on different
        forks may run concurrently; Waypoint serializes commands on the same
        fork.
        """
        target = fork_id if fork_id is not None else self._current_fork_id
        if target == self._current_fork_id:
            return super().exec_command(command, timeout)

        start = time.time()
        rc, out, err = self._exec_raw(target, command, timeout)
        self._stats.add_entry("exec", target, time.time() - start)
        return rc, out, err


    def _core_snapshot(self) -> tuple[Optional[str], float]:
        snapshot_id = str(uuid.uuid4())[:8]

        start = time.time()
        try:
            _run_waypoint(
                ["snapshot", self.session_id, self._current_fork_id, snapshot_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            elapsed = time.time() - start
            self.snapshots[snapshot_id] = snapshot_id
            # Waypoint rebases the snapshotted fork onto the new checkpoint.
            fork = self._live_forks.get(self._current_fork_id)
            if fork:
                fork.base_checkpoint = snapshot_id
            # The destructive snapshot resumed the fork under a fresh PID and
            # socket; refresh the registry so records stay accurate.
            self.list_forks()
            return snapshot_id, elapsed
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint snapshot failed: {_cpe_detail(e)}")
            return None, 0.0

    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        # Unreachable: restore()/create_env_from_snapshot() are refused on this
        # backend (fork() is the only materialization verb). Implemented only
        # to satisfy the EnvironmentManager ABC.
        logger.error("Not supported on the fork-based Waypoint backend; use fork().")
        return None, 0.0

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

    def _core_exec(self, command: List[str] | str, timeout: Optional[float]) -> tuple[int, str, str]:
        return self._exec_raw(self._current_fork_id, command, timeout)

    # ===== Fork lifecycle API (Waypoint-specific extension) =====

    @property
    def live_forks(self) -> List[WaypointFork]:
        """Locally tracked live forks; call list_forks() for a disk refresh."""
        return list(self._live_forks.values())

    def fork(self, snapshot_id: str, n: int = 1, ids: Optional[List[str]] = None) -> List[WaypointFork]:
        """
        Materialize n live forks of a physical snapshot, in parallel.
        Each fork is an isolated running copy (own filesystem layer, own
        process tree, own shell); forks of the same snapshot diverge freely.
        - `ids` optionally names the forks (its length overrides n);
          omitted ids are auto-generated by Waypoint.
        Returns the successfully materialized forks (may be fewer than n).
        """
        node = self.snapshot_graph.get(snapshot_id)
        if node is not None and node.is_virtual:
            logger.error(f"Snapshot {snapshot_id} is virtual; fork() needs a physical snapshot.")
            return []
        if snapshot_id not in self.snapshots:
            logger.error(f"Snapshot {snapshot_id} not found.")
            return []
        if ids is not None:
            if len(set(ids)) != len(ids):
                logger.error("Duplicate fork ids requested.")
                return []
            n = len(ids)
        if n < 1:
            return []
        requested: List[Optional[str]] = list(ids) if ids else [None] * n

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

        results: List[WaypointFork] = []
        # Different forks take different Waypoint locks, so materialization is
        # safe to run concurrently; stats/registry updates stay on this thread.
        with ThreadPoolExecutor(max_workers=min(n, 16)) as pool:
            for outcome in pool.map(materialize, requested):
                if outcome is None:
                    continue
                fork, elapsed = outcome
                self._stats.add_entry("fork", fork.id, elapsed)
                self._live_forks[fork.id] = fork
                results.append(fork)

        logger.info(f"Materialized {len(results)}/{n} fork(s) from snapshot {snapshot_id}")
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

    def destroy_fork(self, fork_id: str) -> bool:
        """Kill a live fork and remove its private layers (state is LOST --
        use snapshot(fork_id=..., park=True) to persist it instead). Refuses
        `main` (session anchor) and the current fork (restore elsewhere first)."""
        if fork_id == MAIN_FORK_ID:
            logger.error("Refusing to destroy `main`: it anchors the session.")
            return False
        if fork_id == self._current_fork_id:
            logger.error(
                "Refusing to destroy the current fork; restore() to another "
                "snapshot (or park it) first."
            )
            return False
        try:
            _run_waypoint(
                ["destroy", self.session_id, fork_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Waypoint destroy of fork {fork_id} failed: {_cpe_detail(e)}")
            return False
        self._live_forks.pop(fork_id, None)
        logger.info(f"Fork {fork_id} destroyed.")
        return True

    def sync_snapshot_tree(self) -> int:
        """
        Hydrate the snapshot tree from the session's checkpoint DAG on disk.

        `waypoint list --json` reports every checkpoint with its parent, so a
        manager attaching to an existing session inherits that history instead
        of starting from a blank tree (without this, sealing a fork created by
        someone else would produce a node whose parent is unknown -- forkable,
        but invisible in print_snapshot_tree()). Locally known nodes are left
        untouched: they may carry virtual-snapshot state the backend cannot
        know about. Returns the number of nodes added.
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
            self.snapshots.setdefault(cid, cid)
            if cid not in self.snapshot_graph:
                self.snapshot_graph[cid] = SnapshotNode(
                    snapshot_id=cid,
                    parent_id=entry.get("parent_id") or None,
                )
                added += 1

        # Pass 2: link children (skipping parents outside this graph, and
        # tolerating a re-sync of nodes that are already linked).
        for entry in entries:
            node = self.snapshot_graph[entry["id"]]
            if node.parent_id and node.parent_id in self.snapshot_graph:
                siblings = self.snapshot_graph[node.parent_id].children
                if node.snapshot_id not in siblings:
                    siblings.append(node.snapshot_id)

        if added:
            logger.info(f"Hydrated {added} checkpoint(s) from session {self.session_id}.")
        return added

    def list_forks(self) -> List[WaypointFork]:
        """Refresh the live-fork registry from `waypoint list --json` and return it."""
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
            return list(self._live_forks.values())
        except json.JSONDecodeError as e:
            logger.error(f"Waypoint list returned invalid JSON: {e}")
            return list(self._live_forks.values())

        refreshed: Dict[str, WaypointFork] = {}
        for entry in listing.get("forks") or []:
            fork_id = entry.get("id")
            if not fork_id:
                continue
            known = self._live_forks.get(fork_id)
            refreshed[fork_id] = WaypointFork(
                id=fork_id,
                pid=entry.get("pid", 0),
                socket=entry.get("socket_path", ""),
                base_checkpoint=entry.get("base_checkpoint_id")
                                or (known.base_checkpoint if known else ""),
                restore_duration=entry.get("restore_duration")
                                 or (known.restore_duration if known else None),
                status=entry.get("status", "running"),
            )
        self._live_forks = refreshed
        return list(self._live_forks.values())

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
            pid=pid,
            socket=kv.get("socket", ""),
            base_checkpoint=base_checkpoint,
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
        return self._work_dir
