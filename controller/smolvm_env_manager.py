from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Literal, Optional

from decider import Decider

from .base_env_manager import DEFAULT_BRANCH_ID, SnapshotNode
from .benchmark import Calculator
from .forkable_env_manager import EnvironmentBranch, ForkableEnvironmentManager

logger = logging.getLogger("EnvManager.Smolvm")

# smolvm (https://github.com/smol-machines/smolvm) runs each environment as a
# hardware-isolated microVM. A machine started ``--branchable`` backs its guest
# RAM with a memfd and exposes a control socket, so a *running* machine can be
# branched: the child gets a copy-on-write clone of RAM and disks and both
# sides keep running. That live fork is the primitive this backend is built on.

SmolvmSnapshotMode = Literal["live", "checkpoint"]

# Every machine this backend creates carries this marker so that a cleanup can
# never touch a machine someone else owns.
MACHINE_PREFIX = "sf"

# A hung CLI call must never block the harness; every invocation has a cap.
DEFAULT_CMD_TIMEOUT = float(os.environ.get("SMOLVM_CMD_TIMEOUT", "180"))


def _resolve_bin() -> str:
    """Locate the smolvm engine binary (``SMOLVM_BIN`` override, then PATH,
    then the installer's default location)."""
    override = os.environ.get("SMOLVM_BIN")
    if override:
        return override
    found = shutil.which("smolvm")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/smolvm")


SMOLVM_BIN = _resolve_bin()


class SmolvmCli:
    """Thin runner around the ``smolvm`` CLI.

    Kept as a separate object so tests can substitute a fake that records the
    argument vectors and returns canned ``CompletedProcess`` results without
    booting a VM. ``run`` returns the ``CompletedProcess`` and never raises on a
    non-zero exit; callers decide what a failure means. A timeout surfaces as a
    ``CompletedProcess`` with return code ``-2`` and the partial output.
    """

    def __init__(self, binary: Optional[str] = None, timeout: float = DEFAULT_CMD_TIMEOUT):
        self.binary = binary or SMOLVM_BIN
        self.timeout = timeout

    def available(self) -> bool:
        return os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)

    def run(self, args: List[str], *, timeout: Optional[float] = None,
            input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = [self.binary, "machine", *args]
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                input=input_text,
                timeout=timeout if timeout is not None else self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode(errors="replace")
            return subprocess.CompletedProcess(
                cmd, -2, out, (err or "") + f"\n[timeout after {timeout or self.timeout}s]"
            )
        except FileNotFoundError as e:
            return subprocess.CompletedProcess(cmd, -1, "", str(e))


def _detail(proc: subprocess.CompletedProcess) -> str:
    text = ((proc.stderr or "") + (proc.stdout or "")).strip()
    # The engine prefixes some lines with ANSI colour codes and tracing
    # metadata; keep the last few lines, which carry the actual error.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-3:]) if lines else f"exit {proc.returncode}"


# ---------------------------------------------------------------------------
# Guest-side freeze/thaw
# ---------------------------------------------------------------------------
#
# A *live* snapshot is a branched machine whose workload is stopped in place.
# The guest kernel keeps running (the agent that serves ``exec`` must stay
# responsive) but every process of the workload is SIGSTOPped, so the files,
# memory, and process tree stay fixed until the machine is branched again. In
# each new branch the same processes are SIGCONTed. PID 1 (the container
# init / agent) and the exec session's own process tree are left alone; the
# scan runs twice so a process forked during the first pass is caught.
_FREEZE_SCRIPT = r"""
me=$$
pass() {
  cd /proc && cat [0-9]*/stat 2>/dev/null | awk -v me="$me" '
    {
      pid = $1; s = $0; sub(/^[^)]*\) /, "", s); split(s, f, " ");
      ppid[pid] = f[2]; comm = $0; sub(/^[0-9]+ \(/, "", comm); sub(/\).*$/, "", comm); name[pid] = comm;
    }
    END {
      # ancestors of the exec session (never touched)
      p = me; while (p in ppid && p > 1) { anc[p] = 1; p = ppid[p] }
      n = 0; out = "";
      for (pid in ppid) {
        if (pid == 1 || pid == me || (pid in anc)) continue;
        if (name[pid] ~ /smolvm|agent/) continue;
        # descendants of the exec session are skipped too
        q = pid; skip = 0;
        while (q in ppid && q > 1) { if (q == me) { skip = 1; break }; q = ppid[q] }
        if (skip) continue;
        out = out " " pid; n++;
      }
      print n; print out;
    }' | { read -r n; read -r list; [ -n "$list" ] && kill -%SIG% $list 2>/dev/null; echo "$n"; }
}
n1=$(pass); n2=$(pass)
echo "frozen=$n1 recheck=$n2"
"""

FREEZE_SCRIPT = _FREEZE_SCRIPT.replace("%SIG%", "STOP")
THAW_SCRIPT = _FREEZE_SCRIPT.replace("%SIG%", "CONT")


@dataclass(init=False)
class SmolvmBranch(EnvironmentBranch):
    """A live branch: one running smolvm machine.

    ``machine`` is the engine-side name. It changes when the branch is
    snapshotted in ``live`` mode (the old machine becomes the frozen snapshot
    and the branch continues on a fresh copy-on-write child), so callers
    should address branches by ``id`` and read ``machine`` only for
    diagnostics.
    """

    machine: str = ""
    pid: int = 0

    def __init__(
        self,
        id: str,
        machine: str = "",
        base_snapshot_id: str = "",
        status: str = "running",
        pid: int = 0,
    ) -> None:
        super().__init__(id=id, base_snapshot_id=base_snapshot_id, status=status)
        self.machine = machine
        self.pid = pid


class SmolvmCalculator(Calculator):
    """Storage/memory accounting for the snapshots a manager holds.

    ``items`` returns ``(label, kind, target)`` tuples. ``kind`` is ``"path"``
    (measured with ``du -sb``: a machine's data directory or a checkpoint
    store) or ``"pid"`` (the VMM process's proportional set size from
    ``/proc/<pid>/smaps_rollup``, which splits copy-on-write pages shared
    between a frozen snapshot and its children fairly between them).
    """

    def __init__(self, items: Callable[[], List[tuple[str, str, str]]], name: str):
        super().__init__(name=name)
        self._items = items

    @staticmethod
    def _du(path: str) -> int:
        try:
            out = subprocess.check_output(["du", "-sb", path], text=True, stderr=subprocess.DEVNULL)
            return int(out.split()[0])
        except Exception:
            return 0

    @staticmethod
    def _pss(pid: str) -> int:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        return int(line.split()[1]) * 1024
        except Exception:
            pass
        return 0

    def _collect(self) -> List[tuple[str, int]]:
        data = []
        for label, kind, target in self._items():
            size = self._du(target) if kind == "path" else self._pss(target)
            if size > 0:
                data.append((label, size))
        return data


class SmolvmAttachManager(ForkableEnvironmentManager[SmolvmBranch]):
    """Manage an existing, running, ``--branchable`` smolvm machine as ``main``.

    Semantics follow the forkable contract exactly as Waypoint implements it:
    ``snapshot()`` seals the current branch, ``fork()`` materializes live
    copies of a snapshot, ``restore()`` is the explicit fork-then-discard
    macro, ``park`` seals without resuming, ``discard_branch`` destroys.

    Two snapshot modes:

    * ``live`` (default): a snapshot is a copy-on-write branch of the running
      machine whose workload is frozen in place. Sealing a branch freezes its
      current machine, branches a fresh child for the branch to continue on,
      and thaws the child; the frozen machine is the immutable snapshot and
      can be branched any number of times (each child is thawed on
      materialization). Nothing is serialized to disk.
    * ``checkpoint``: a snapshot is a portable ``.checkpoint`` written with
      ``smolvm machine checkpoint`` (RAM + disks, chunk-deduplicated against
      a store); a fork is ``create --from`` + ``start``. This is the
      serialization path comparable to CRIU or Firecracker snapshots.

    Only machines this manager created are ever deleted. An attached machine
    is left in place (and thawed) by ``cleanup``.
    """

    def __init__(
        self,
        machine_name: str,
        snapshot_mode: SmolvmSnapshotMode = "live",
        decider: Optional[Decider] = None,
        cli: Optional[SmolvmCli] = None,
        checkpoint_dir: Optional[str] = None,
        owns_root: bool = False,
        session_tag: Optional[str] = None,
    ) -> None:
        super().__init__(backend_name="Smolvm", decider=decider)
        if snapshot_mode not in ("live", "checkpoint"):
            raise ValueError(f"snapshot_mode must be 'live' or 'checkpoint', got {snapshot_mode!r}")
        self.cli = cli or SmolvmCli()
        self.snapshot_mode: SmolvmSnapshotMode = snapshot_mode
        self.session_tag = session_tag or uuid.uuid4().hex[:6]
        self.root_machine = machine_name
        self._owns_root = owns_root
        self._root_frozen = False
        self._checkpoint_dir = checkpoint_dir or os.path.join(
            os.environ.get("SMOLVM_STATEFORK_DIR", "/tmp/statefork_smolvm"), self.session_tag
        )
        # machines created by this manager, in creation order; the only ones
        # cleanup may delete.
        self._owned: List[str] = [machine_name] if owns_root else []
        self._owned_lock = threading.Lock()
        self._machine_counter = 0
        # snapshot_id -> machine (live) or checkpoint path (checkpoint)
        # is kept in the inherited ``snapshots`` registry.

        if not self.cli.available():
            self.is_cleaned_up = True
            raise FileNotFoundError(
                f"smolvm binary not found or not executable: {self.cli.binary}. Install it "
                "(curl -sSL https://smolmachines.com/install.sh | bash) or set SMOLVM_BIN."
            )

        info = self._status(machine_name)
        if info is None:
            self.is_cleaned_up = True
            raise RuntimeError(f"smolvm machine {machine_name!r} not found.")
        if info.get("state") != "running":
            self.is_cleaned_up = True
            raise RuntimeError(
                f"smolvm machine {machine_name!r} is {info.get('state')}; it must be running "
                "(start it with `smolvm machine start --name NAME --branchable`)."
            )
        if not (info.get("branchable") or info.get("forkable")):
            # The record flag is only set on machines created by `branch`; a
            # root started `--branchable` reports False here although it can be
            # branched, so this is advisory. A machine that really cannot be
            # branched fails loudly at the initial snapshot below.
            logger.warning(
                "smolvm machine %s does not report itself branchable; it must have been "
                "started with `--branchable` or snapshots will fail.", machine_name,
            )

        with self._state_lock:
            self._live_branches[DEFAULT_BRANCH_ID] = SmolvmBranch(
                id=DEFAULT_BRANCH_ID, machine=machine_name, pid=int(info.get("pid") or 0)
            )

        logger.info("Attached to smolvm machine %s (mode=%s, tag=%s)", machine_name, snapshot_mode, self.session_tag)

        sid, _ = self._core_snapshot(DEFAULT_BRANCH_ID)
        if sid is None:
            self.is_cleaned_up = True
            raise RuntimeError("Failed to create initial snapshot.")
        self._record_snapshot_node(SnapshotNode(snapshot_id=sid, parent_id=None))
        self._set_branch_position(DEFAULT_BRANCH_ID, sid)
        with self._state_lock:
            self._live_branches[DEFAULT_BRANCH_ID].base_snapshot_id = sid

        self._stats.attach_size_calculator(
            SmolvmCalculator(self._snapshot_disk_items, name="SNAPSHOT_DISK")
        )
        if snapshot_mode == "live":
            self._stats.attach_size_calculator(
                SmolvmCalculator(self._snapshot_memory_items, name="SNAPSHOT_MEMORY")
            )

    # ------------------------------------------------------------------
    # Engine helpers
    # ------------------------------------------------------------------

    def _new_machine_name(self, role: str) -> str:
        with self._owned_lock:
            self._machine_counter += 1
            return f"{MACHINE_PREFIX}-{self.session_tag}-{role}{self._machine_counter}"

    def _own(self, machine: str) -> None:
        with self._owned_lock:
            if machine not in self._owned:
                self._owned.append(machine)

    def _status(self, machine: str) -> Optional[dict]:
        proc = self.cli.run(["status", "--name", machine, "--json"], timeout=60)
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def _machine_of(self, branch_id: str) -> Optional[str]:
        with self._state_lock:
            branch = self._live_branches.get(branch_id)
            return branch.machine if branch else None

    def _set_machine(self, branch_id: str, machine: str, pid: int = 0) -> None:
        with self._state_lock:
            branch = self._live_branches.get(branch_id)
            if branch is not None:
                branch.machine = machine
                if pid:
                    branch.pid = pid

    def _exec_script(self, machine: str, script: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return self.cli.run(["exec", "--name", machine, "--", "sh", "-c", script], timeout=timeout)

    def _freeze(self, machine: str) -> bool:
        proc = self._exec_script(machine, FREEZE_SCRIPT)
        if proc.returncode != 0:
            logger.error("Freeze of %s failed: %s", machine, _detail(proc))
            return False
        logger.debug("Freeze %s: %s", machine, (proc.stdout or "").strip())
        return True

    def _thaw(self, machine: str) -> bool:
        proc = self._exec_script(machine, THAW_SCRIPT)
        if proc.returncode != 0:
            logger.error("Thaw of %s failed: %s", machine, _detail(proc))
            return False
        return True

    def _branch_machine(self, source: str, child: str, *, branchable: bool = True) -> Optional[int]:
        """``smolvm machine branch``; returns the child's VMM pid (0 if unknown)."""
        args = ["branch", "--from", source, "--name", child]
        if branchable:
            args.append("--branchable")
        proc = self.cli.run(args)
        if proc.returncode != 0:
            logger.error("smolvm branch %s -> %s failed: %s", source, child, _detail(proc))
            return None
        self._own(child)
        pid = 0
        for token in (proc.stdout or "").replace("(", " ").replace(")", " ").replace(".", " ").split():
            if token.isdigit() and "PID" in (proc.stdout or ""):
                pid = int(token)
        return pid

    def _delete_machine(self, machine: str, *, cascade: bool = False) -> bool:
        args = ["delete", "--name", machine, "--force"]
        if cascade:
            args.append("--cascade")
        proc = self.cli.run(args, timeout=120)
        if proc.returncode != 0:
            logger.error("smolvm delete %s failed: %s", machine, _detail(proc))
            return False
        with self._owned_lock:
            if machine in self._owned:
                self._owned.remove(machine)
        return True

    def _checkpoint_path(self, snapshot_id: str) -> str:
        return os.path.join(self._checkpoint_dir, f"{snapshot_id}.checkpoint")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _core_snapshot(self, branch_id: str) -> tuple[Optional[str], float]:
        machine = self._machine_of(branch_id)
        if machine is None:
            logger.error("Branch %s has no machine.", branch_id)
            return None, 0.0
        snapshot_id = str(uuid.uuid4())[:8]
        start = time.time()

        if self.snapshot_mode == "checkpoint":
            os.makedirs(self._checkpoint_dir, exist_ok=True)
            path = self._checkpoint_path(snapshot_id)
            proc = self.cli.run(
                ["checkpoint", "--name", machine, "-o", path, "--store", os.path.join(self._checkpoint_dir, "store")]
            )
            if proc.returncode != 0:
                logger.error("smolvm checkpoint of %s failed: %s", machine, _detail(proc))
                return None, time.time() - start
            elapsed = time.time() - start
            self._register_snapshot_resource(snapshot_id, path)
            return snapshot_id, elapsed

        # live mode: freeze in place, continue the branch on a fresh child.
        if not self._freeze(machine):
            return None, time.time() - start
        successor = self._new_machine_name("b")
        pid = self._branch_machine(machine, successor)
        if pid is None:
            # Leave the branch usable: undo the freeze.
            self._thaw(machine)
            return None, time.time() - start
        if not self._thaw(successor):
            self._delete_machine(successor)
            self._thaw(machine)
            return None, time.time() - start
        elapsed = time.time() - start

        if machine == self.root_machine:
            self._root_frozen = True
        self._set_machine(branch_id, successor, pid)
        self._register_snapshot_resource(snapshot_id, machine)
        return snapshot_id, elapsed

    def _core_park_branch(self, branch_id: str) -> tuple[Optional[str], float]:
        machine = self._machine_of(branch_id)
        if machine is None:
            return None, 0.0
        snapshot_id = str(uuid.uuid4())[:8]
        start = time.time()
        if self.snapshot_mode == "checkpoint":
            sid, elapsed = self._core_snapshot(branch_id)
            if sid is None:
                return None, elapsed
            if not self._delete_machine(machine):
                logger.warning("Parked %s but its machine %s could not be deleted.", branch_id, machine)
            return sid, time.time() - start
        # live: the branch's machine itself becomes the frozen snapshot.
        if not self._freeze(machine):
            return None, time.time() - start
        if machine == self.root_machine:
            self._root_frozen = True
        self._register_snapshot_resource(snapshot_id, machine)
        return snapshot_id, time.time() - start

    # ------------------------------------------------------------------
    # Fork
    # ------------------------------------------------------------------

    def _core_fork(
        self,
        snapshot_id: str,
        requested_ids: List[Optional[str]],
    ) -> List[tuple[SmolvmBranch, float]]:
        resource = self._snapshot_resource(snapshot_id)
        if resource is None:
            logger.error("Snapshot %s has no backend resource.", snapshot_id)
            return []

        def materialize(requested: Optional[str]) -> Optional[tuple[SmolvmBranch, float]]:
            child = self._new_machine_name("b")
            branch_id = requested or child
            start = time.time()
            if self.snapshot_mode == "checkpoint":
                proc = self.cli.run(["create", "--name", child, "--from", resource])
                if proc.returncode != 0:
                    logger.error("smolvm create --from %s failed: %s", resource, _detail(proc))
                    return None
                self._own(child)
                proc = self.cli.run(["start", "--name", child, "--branchable"])
                if proc.returncode != 0:
                    logger.error("smolvm start %s failed: %s", child, _detail(proc))
                    self._delete_machine(child)
                    return None
                info = self._status(child) or {}
                pid = int(info.get("pid") or 0)
            else:
                pid = self._branch_machine(resource, child)
                if pid is None:
                    return None
                if not self._thaw(child):
                    self._delete_machine(child)
                    return None
            elapsed = time.time() - start
            return SmolvmBranch(id=branch_id, machine=child, base_snapshot_id=snapshot_id, pid=pid or 0), elapsed

        results: List[tuple[SmolvmBranch, float]] = []
        with ThreadPoolExecutor(max_workers=min(len(requested_ids), 8)) as pool:
            for outcome in pool.map(materialize, requested_ids):
                if outcome is not None:
                    results.append(outcome)
        return results

    # ------------------------------------------------------------------
    # Exec / copy / discard / list
    # ------------------------------------------------------------------

    def _core_exec(
        self,
        command: List[str] | str,
        timeout: Optional[float],
        branch_id: str,
    ) -> tuple[int, str, str]:
        machine = self._machine_of(branch_id)
        if machine is None:
            return -1, "", f"Branch {branch_id} has no machine."
        cmd_str = command if isinstance(command, str) else shlex.join(command)
        proc = self.cli.run(
            ["exec", "--name", machine, "--", "sh", "-c", cmd_str],
            timeout=timeout if timeout is not None else self.cli.timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _core_copy(
        self,
        branch_id: str,
        host_path: str,
        env_path: str,
        into: bool,
    ) -> tuple[bool, str]:
        machine = self._machine_of(branch_id)
        if machine is None:
            return False, f"Branch {branch_id} has no machine."
        host_path = os.path.abspath(host_path)
        ref = f"{machine}:{env_path}"
        args = ["cp", host_path, ref] if into else ["cp", ref, host_path]
        proc = self.cli.run(args)
        if proc.returncode != 0:
            return False, _detail(proc)
        return True, ""

    def _core_discard_branch(self, branch_id: str) -> bool:
        machine = self._machine_of(branch_id)
        if machine is None:
            return False
        if machine == self.root_machine and not self._owns_root:
            logger.error("Refusing to delete the attached machine %s.", machine)
            return False
        return self._delete_machine(machine)

    def _core_list_branches(self) -> Optional[List[SmolvmBranch]]:
        proc = self.cli.run(["ls", "--json"], timeout=60)
        if proc.returncode != 0:
            return None
        try:
            listing = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        rows = listing if isinstance(listing, list) else listing.get("machines", [])
        by_name = {row.get("name"): row for row in rows if isinstance(row, dict)}
        refreshed: List[SmolvmBranch] = []
        with self._state_lock:
            current = list(self._live_branches.values())
        for branch in current:
            row = by_name.get(branch.machine)
            if row is None:
                continue
            refreshed.append(
                SmolvmBranch(
                    id=branch.id,
                    machine=branch.machine,
                    base_snapshot_id=branch.base_snapshot_id,
                    status=row.get("state", branch.status),
                    pid=int(row.get("pid") or branch.pid or 0),
                )
            )
        return refreshed

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _core_cleanup(self) -> bool:
        ok = True
        with self._owned_lock:
            owned = [m for m in self._owned if m != self.root_machine]
        # Children depend on their sources' disks, so delete newest first;
        # anything that still refuses goes with --cascade.
        for machine in reversed(owned):
            if not self._delete_machine(machine):
                ok = self._delete_machine(machine, cascade=True) and ok
        if self._owns_root:
            with self._owned_lock:
                root_owned = self.root_machine in self._owned
            if root_owned and not self._delete_machine(self.root_machine, cascade=True):
                ok = False
        with self._owned_lock:
            leftovers = list(self._owned)
        if leftovers:
            logger.error("Machines not deleted: %s", ", ".join(leftovers))
            ok = False
        if not self._owns_root and self._root_frozen:
            # Hand the attached machine back the way we found it.
            self._thaw(self.root_machine)
            self._root_frozen = False
        if self.snapshot_mode == "checkpoint" and os.path.isdir(self._checkpoint_dir):
            shutil.rmtree(self._checkpoint_dir, ignore_errors=True)
        return ok

    # ------------------------------------------------------------------
    # Storage accounting
    # ------------------------------------------------------------------

    def _snapshot_disk_items(self) -> List[tuple[str, str, str]]:
        items = []
        for snapshot_id, resource in self._snapshot_items():
            if self.snapshot_mode == "checkpoint":
                if os.path.exists(resource):
                    items.append((snapshot_id, "path", resource))
            else:
                proc = self.cli.run(["data-dir", "--name", resource], timeout=30)
                path = (proc.stdout or "").strip().splitlines()[-1] if proc.returncode == 0 and proc.stdout.strip() else ""
                if path and os.path.isdir(path):
                    items.append((snapshot_id, "path", path))
        return items

    def _snapshot_memory_items(self) -> List[tuple[str, str, str]]:
        items = []
        for snapshot_id, machine in self._snapshot_items():
            info = self._status(machine)
            if info and info.get("pid"):
                items.append((snapshot_id, "pid", str(info["pid"])))
        return items


class SmolvmBuildManager(SmolvmAttachManager):
    """Create a fresh branchable smolvm machine from a container image and
    manage it.

    Unlike the Docker build manager this does not consume a Dockerfile: the
    engine pulls OCI images from a registry, so pass the image you want
    (``base_image``) and, if the environment needs it, ``net=True`` so the
    guest can pull and reach the network. The workload command keeps the
    machine alive; the default idles.
    """

    def __init__(
        self,
        base_image: str = "debian:bookworm-slim",
        command: Optional[List[str]] = None,
        net: bool = False,
        cpus: Optional[int] = None,
        mem_mib: Optional[int] = None,
        extra_args: Optional[List[str]] = None,
        snapshot_mode: SmolvmSnapshotMode = "live",
        decider: Optional[Decider] = None,
        cli: Optional[SmolvmCli] = None,
        checkpoint_dir: Optional[str] = None,
        dockerfile_dir: Optional[str] = None,
    ) -> None:
        self._validate_decider(decider)
        if dockerfile_dir is not None:
            raise ValueError(
                "smolvm_build takes a registry image (base_image=...), not a Dockerfile: "
                "build and push the image, then pass its reference."
            )
        cli = cli or SmolvmCli()
        if not cli.available():
            self.is_cleaned_up = True
            raise FileNotFoundError(
                f"smolvm binary not found or not executable: {cli.binary}. Install it "
                "(curl -sSL https://smolmachines.com/install.sh | bash) or set SMOLVM_BIN."
            )
        tag = uuid.uuid4().hex[:6]
        machine = f"{MACHINE_PREFIX}-{tag}-main"
        args = ["create", "--name", machine, "--image", base_image]
        if net:
            args.append("--net")
        if cpus:
            args += ["--cpus", str(cpus)]
        if mem_mib:
            args += ["--mem", str(mem_mib)]
        args += list(extra_args or [])
        args += ["--", *(command or ["sh", "-c", "while true; do sleep 3600; done"])]
        logger.info("Creating smolvm machine %s from %s...", machine, base_image)
        proc = cli.run(args, timeout=600)
        if proc.returncode != 0:
            self.is_cleaned_up = True
            raise RuntimeError(f"smolvm machine create failed: {_detail(proc)}")
        proc = cli.run(["start", "--name", machine, "--branchable"], timeout=600)
        if proc.returncode != 0:
            cli.run(["delete", "--name", machine, "--force"], timeout=120)
            self.is_cleaned_up = True
            raise RuntimeError(f"smolvm machine start failed: {_detail(proc)}")

        try:
            super().__init__(
                machine_name=machine,
                snapshot_mode=snapshot_mode,
                decider=decider,
                cli=cli,
                checkpoint_dir=checkpoint_dir,
                owns_root=True,
                session_tag=tag,
            )
        except BaseException:
            # Never leave a half-built machine behind.
            cli.run(["delete", "--name", machine, "--force", "--cascade"], timeout=120)
            raise
