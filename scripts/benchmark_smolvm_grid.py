#!/usr/bin/env python3
"""Snapshot / fork / restore latency grid across execution substrates.

Modelled on the StateFork paper's microbenchmark: an environment holds M MiB of
dirtied anonymous memory (a live python3 process that touched every page and
sleeps) and D MiB of freshly written file data; we then time

    snapshot   seal the environment            (every manager)
    fork       materialize one live copy       (forkable managers only)
    restore    bring the current environment back to the snapshot

for every (M, D) in the grid, ``--repeats`` times each, on a fresh
environment per grid point. Storage held by snapshots is read from the
manager's size calculators after the first snapshot.

Managers:

    smolvm_build     microVM live fork (``--snapshot-mode live``) or portable
                     checkpoint (``--snapshot-mode checkpoint``); runs as the
                     ordinary user, never under sudo
    docker_build     container commit / run            (root)
    podman_build     container commit / run            (root)
    criu_build       CRIU dump / restore of a process  (root)
    hybrid_build     Podman container checkpoint+restore via CRIU (root)
    waypoint_build   Waypoint fork-based sessions, from a rootfs (root)

The root-needing managers expect to be run the way their own demo is::

    sudo -E .venv/bin/python scripts/benchmark_smolvm_grid.py --manager criu_build ...

Examples::

    .venv/bin/python scripts/benchmark_smolvm_grid.py --manager smolvm_build --sizes 0 --repeats 1
    .venv/bin/python scripts/benchmark_smolvm_grid.py --manager smolvm_build --snapshot-mode checkpoint
    sudo -E .venv/bin/python scripts/benchmark_smolvm_grid.py --manager waypoint_build \\
        --rootfs /path/to/rootfs --sizes 0 512 1024 2048 --repeats 3

Output: a CSV of every timed operation (``--csv``) and a markdown table of
medians (``--md``, also printed).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from controller import EnvironmentManager, ForkableEnvironmentManager, create_env_manager  # noqa: E402

log = logging.getLogger("grid")

IMAGE = "python:3.12-slim"
READY = "/var/tmp/sf_ready"
DISK_FILE = "/var/tmp/sf_disk"

# Touch every page of M MiB and stay alive. Written so it runs on any python3.
MEM_PY = (
    "import time,sys\n"
    "n=int(sys.argv[1])*1024*1024\n"
    "b=bytearray(n)\n"
    "for i in range(0,n,4096): b[i]=1\n"
    "open(sys.argv[2],'w').write('ready\\n')\n"
    "time.sleep(10**8)\n"
)


def dirty_script(mem_mib: int, disk_mib: int) -> str:
    """Shell that writes D MiB to disk, then starts the M MiB holder in the
    background; the holder writes READY when every page has been touched."""
    parts = [f"rm -f {READY} {DISK_FILE}"]
    if disk_mib > 0:
        parts.append(f"dd if=/dev/urandom of={DISK_FILE} bs=1M count={disk_mib} status=none")
    code = MEM_PY.replace("'", "'\"'\"'")
    parts.append(f"nohup python3 -c '{code}' {mem_mib} {READY} >/dev/null 2>&1 &")
    return " && ".join(parts)


def wait_ready(exec_fn, timeout: float = 600) -> float:
    start = time.time()
    while time.time() - start < timeout:
        rc, _, _ = exec_fn(f"test -f {READY}")
        if rc == 0:
            return time.time() - start
        time.sleep(0.25)
    raise TimeoutError("memory holder never reported ready")


class Env:
    """One environment under test, with uniform exec/snapshot/fork/restore."""

    def __init__(self, args: argparse.Namespace, mem_mib: int, disk_mib: int):
        self.args = args
        self.mem_mib = mem_mib
        self.disk_mib = disk_mib
        self.tmp = tempfile.mkdtemp(prefix="sfgrid-")
        self.tag = uuid.uuid4().hex[:6]
        self.manager: Optional[EnvironmentManager] = None
        self.label = args.manager if args.manager != "smolvm_build" else f"smolvm_{args.snapshot_mode}"

    # -- construction ------------------------------------------------------
    def _dockerfile_dir(self) -> str:
        d = os.path.join(self.tmp, "ctx")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "Dockerfile"), "w") as f:
            f.write(f"FROM {self.args.image}\nCMD [\"sleep\", \"infinity\"]\n")
        return d

    def build(self) -> None:
        a = self.args
        m = a.manager
        if m == "smolvm_build":
            self.manager = create_env_manager(
                "smolvm_build", base_image=a.image, net=a.net, snapshot_mode=a.snapshot_mode,
                mem_mib=max(2048, self.mem_mib + 1024), cpus=2,
                checkpoint_dir=os.path.join(self.tmp, "ckpt"),
            )
        elif m in ("docker_build", "podman_build"):
            self.manager = create_env_manager(
                m, dockerfile_dir=self._dockerfile_dir(), base_image=f"sfgrid-{self.tag}:base", extra_args=[],
            )
            if not self.manager.restore("base"):
                raise RuntimeError("could not start the container from the built image")
        elif m == "hybrid_build":
            self.manager = create_env_manager(
                "hybrid_build", container_name=f"sfgrid-{self.tag}", dockerfile_dir=self._dockerfile_dir(),
                export_dir=os.path.join(self.tmp, "export"), extra_args=[],
            )
        elif m == "criu_build":
            # The process tree *is* the environment: dirty inside the command.
            ready = os.path.join(self.tmp, "ready")
            disk = os.path.join(self.tmp, "disk")
            script = (
                "import os,subprocess,sys,time\n"
                f"d={self.disk_mib}\n"
                f"if d: subprocess.run(['dd','if=/dev/urandom','of={disk}','bs=1M','count='+str(d),'status=none'],check=True)\n"
                f"n={self.mem_mib}*1024*1024\n"
                "b=bytearray(n)\n"
                "for i in range(0,n,4096): b[i]=1\n"
                f"open('{ready}','w').write('ready\\n')\n"
                "time.sleep(10**8)\n"
            )
            self.manager = create_env_manager(
                "criu_build", work_dir=os.path.join(self.tmp, "criu"), command=["python3", "-c", script],
            )
            self._criu_ready = ready
        elif m == "waypoint_build":
            if not a.rootfs:
                raise ValueError("--rootfs is required for waypoint_build (buildah is not installed)")
            self.manager = create_env_manager("waypoint_build", dockerfile_dir=a.rootfs, build=False)
        else:
            raise ValueError(f"unknown manager {m}")

    # -- uniform operations --------------------------------------------------
    def exec(self, cmd: str, timeout: float = 900):
        return self.manager.exec_command(cmd, timeout=timeout)

    def dirty(self) -> float:
        if self.args.manager == "criu_build":
            start = time.time()
            while time.time() - start < 600:
                if os.path.exists(self._criu_ready):
                    return time.time() - start
                time.sleep(0.25)
            raise TimeoutError("CRIU process never reported ready")
        rc, out, err = self.exec(dirty_script(self.mem_mib, self.disk_mib))
        if rc != 0:
            raise RuntimeError(f"dirtying failed rc={rc}: {err.strip()[:300]} {out.strip()[:200]}")
        return wait_ready(self.exec)

    @property
    def forkable(self) -> bool:
        return isinstance(self.manager, ForkableEnvironmentManager)

    def last(self, op: str) -> float:
        entries = [e for e in self.manager.stats.log if e.operation == op]
        return entries[-1].elapsed_time if entries else float("nan")

    def storage_bytes(self) -> int:
        try:
            result = self.manager.stats.get_all_statistics()
            return int(sum(s.total for s in result.size.values()))
        except Exception:
            return 0

    def cleanup(self) -> None:
        try:
            if self.manager is not None:
                self.manager.cleanup()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


def run_point(args: argparse.Namespace, mem_mib: int, disk_mib: int, rows: list) -> None:
    env = Env(args, mem_mib, disk_mib)
    print(f"\n== {env.label}  mem={mem_mib} MiB  disk={disk_mib} MiB")
    try:
        t0 = time.time()
        env.build()
        print(f"   built in {time.time() - t0:.1f}s")
        t = env.dirty()
        print(f"   dirtied in {t:.1f}s")
        for rep in range(args.repeats):
            sid = env.manager.snapshot()
            if sid is None:
                raise RuntimeError("snapshot failed")
            snap = env.last("snapshot")
            rows.append([env.label, mem_mib, disk_mib, rep, "snapshot", f"{snap:.4f}"])
            storage = env.storage_bytes() if rep == 0 else None
            if storage is not None:
                rows.append([env.label, mem_mib, disk_mib, rep, "storage_bytes", str(storage)])
            fork = float("nan")
            if env.forkable:
                kids = env.manager.fork(sid, n=1)
                if kids:
                    fork = env.last("fork")
                    rows.append([env.label, mem_mib, disk_mib, rep, "fork", f"{fork:.4f}"])
                    env.manager.discard_branch(kids[0].id)
                else:
                    rows.append([env.label, mem_mib, disk_mib, rep, "fork", "nan"])
            if not env.manager.restore(sid):
                raise RuntimeError("restore failed")
            rest = env.last("restore")
            rows.append([env.label, mem_mib, disk_mib, rep, "restore", f"{rest:.4f}"])
            if args.manager == "criu_build":
                time.sleep(0.5)  # criu restore returns before the tree is fully resumed
            print(f"   rep {rep}: snapshot {snap * 1000:.0f} ms  fork {fork * 1000:.0f} ms  restore {rest * 1000:.0f} ms"
                  + (f"  storage {storage / 2**20:.1f} MiB" if storage else ""))
    except Exception as exc:  # report and continue with the next point
        log.error("%s mem=%d disk=%d failed: %s", env.label, mem_mib, disk_mib, exc)
        rows.append([env.label, mem_mib, disk_mib, -1, "error", str(exc)[:200]])
    finally:
        env.cleanup()


def markdown(rows: list) -> str:
    med = defaultdict(list)
    for label, m, d, rep, op, val in rows:
        if op in ("snapshot", "fork", "restore", "storage_bytes"):
            try:
                med[(label, int(m), int(d), op)].append(float(val))
            except ValueError:
                pass
    keys = sorted({(l, m, d) for (l, m, d, _) in med})
    lines = ["| manager | mem MiB | disk MiB | snapshot ms | fork ms | restore ms | storage MiB |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for l, m, d in keys:
        def cell(op, scale=1000.0):
            v = med.get((l, m, d, op))
            if not v or all(x != x for x in v):
                return "—"
            return f"{statistics.median([x for x in v if x == x]) * scale:.0f}"
        lines.append(f"| {l} | {m} | {d} | {cell('snapshot')} | {cell('fork')} | {cell('restore')} | "
                     f"{cell('storage_bytes', 1 / 2**20)} |")
    errors = [r for r in rows if r[4] == "error"]
    if errors:
        lines.append("")
        lines.extend(f"- error: {r[0]} mem={r[1]} disk={r[2]}: {r[5]}" for r in errors)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manager", required=True,
                    choices=["smolvm_build", "docker_build", "podman_build", "criu_build", "hybrid_build", "waypoint_build"])
    ap.add_argument("--snapshot-mode", choices=["live", "checkpoint"], default="live", help="smolvm only")
    ap.add_argument("--sizes", type=int, nargs="+", default=[0, 512, 1024, 2048],
                    help="MiB values; the grid is sizes x sizes unless --mem/--disk pin one axis")
    ap.add_argument("--mem", type=int, nargs="+", help="memory MiB axis (overrides --sizes)")
    ap.add_argument("--disk", type=int, nargs="+", help="disk MiB axis (overrides --sizes)")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--image", default=IMAGE, help="image for smolvm/docker/podman/hybrid (needs python3)")
    ap.add_argument("--net", action="store_true", help="smolvm: give the machine outbound network")
    ap.add_argument("--rootfs", help="waypoint_build: root filesystem with /bin/bash and python3")
    ap.add_argument("--csv", default=None, help="CSV output path (default: run_stats/grid-<manager>-<ts>.csv)")
    ap.add_argument("--md", default=None, help="markdown output path (default: beside the CSV)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.manager == "smolvm_build" and os.geteuid() == 0:
        print("smolvm_build must run as the ordinary user, not under sudo", file=sys.stderr)
        return 2
    if args.manager != "smolvm_build" and os.geteuid() != 0:
        print(f"{args.manager} needs root: run with `sudo -E .venv/bin/python {sys.argv[0]} ...`", file=sys.stderr)
        return 2

    mems = args.mem or args.sizes
    disks = args.disk or args.sizes
    label = args.manager if args.manager != "smolvm_build" else f"smolvm_{args.snapshot_mode}"
    ts = time.strftime("%Y%m%d-%H%M%S")
    csv_path = args.csv or str(REPO_ROOT / "run_stats" / f"grid-{label}-{ts}.csv")
    md_path = args.md or csv_path.rsplit(".", 1)[0] + ".md"
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    rows: list = []
    for m in mems:
        for d in disks:
            run_point(args, m, d, rows)
            with open(csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["manager", "mem_mib", "disk_mib", "repeat", "operation", "value"])
                w.writerows(rows)

    table = markdown(rows)
    with open(md_path, "w") as f:
        f.write(f"# {label} grid ({ts})\n\n{table}\n")
    print("\n" + table)
    print(f"\ncsv: {csv_path}\nmd:  {md_path}")
    return 1 if any(r[4] == "error" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
