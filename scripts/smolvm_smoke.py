#!/usr/bin/env python3
"""End-to-end smoke test for the smolvm backend (runs as an ordinary user).

Drives one smolvm session through the sequence StateFork is built for:

    build a branchable microVM from an image
      -> prepare state in ``main`` (files, a background job writing a tick file)
      -> seal it as a snapshot
      -> fork 3 candidates, mutate each differently, assert isolation
      -> assert the frozen snapshot does not advance while its children do
      -> restore ``main`` to the snapshot and assert the prepared state is back
      -> park one fork and revive it, discard the rest
      -> export a file to the host, print statistics, clean up

The exit code is the number of failed assertions, so the script doubles as
the integration test for the backend. No root, no CRIU: it needs only the
``smolvm`` binary and KVM (or Hypervisor.framework on macOS).

Examples::

    .venv/bin/python scripts/smolvm_smoke.py
    .venv/bin/python scripts/smolvm_smoke.py --image alpine --net
    .venv/bin/python scripts/smolvm_smoke.py --snapshot-mode checkpoint
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from controller import create_env_manager  # noqa: E402
from controller.smolvm_env_manager import SMOLVM_BIN  # noqa: E402


class Check:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def __call__(self, cond: bool, what: str) -> bool:
        if cond:
            self.passed += 1
            print(f"  ok   {what}")
        else:
            self.failed += 1
            print(f"  FAIL {what}")
        return cond


TICK_JOB = (
    "sh -c 'i=0; while true; do i=$((i+1)); echo $i > /tmp/tick; sleep 0.2; done' "
    ">/dev/null 2>&1 &"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default="debian:bookworm-slim")
    ap.add_argument("--net", action="store_true", help="give the machine outbound network")
    ap.add_argument("--snapshot-mode", choices=["live", "checkpoint"], default="live")
    ap.add_argument("--fanout", type=int, default=3)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    check = Check()

    print(f"smolvm binary: {SMOLVM_BIN}")
    t0 = time.time()
    manager = create_env_manager(
        "smolvm_build", base_image=args.image, net=args.net, snapshot_mode=args.snapshot_mode
    )
    print(f"built {manager.root_machine} and took the initial snapshot in {time.time() - t0:.2f}s "
          f"(mode={manager.snapshot_mode})")
    try:
        # -- prepare state in main ------------------------------------------
        rc, out, err = manager.exec_command(f"echo hello > /tmp/f && mkdir -p /tmp/d && {TICK_JOB} echo started")
        check(rc == 0 and "started" in out, f"prepare state (rc={rc} {err.strip()[:80]})")
        time.sleep(1.0)
        tick0 = manager.exec_command("cat /tmp/tick")[1].strip()
        check(tick0.isdigit() and int(tick0) >= 1, f"background job is ticking ({tick0!r})")

        # -- seal -------------------------------------------------------------
        sid = manager.snapshot()
        check(sid is not None, f"snapshot main -> {sid}")
        if sid is None:
            return 1
        snap_entry = [e for e in manager.stats.log if e.operation == "snapshot"][-1]
        print(f"  snapshot latency {snap_entry.elapsed_time * 1000:.0f} ms")

        # main keeps running after the seal
        time.sleep(0.6)
        tick1 = manager.exec_command("cat /tmp/tick")[1].strip()
        check(tick1.isdigit() and int(tick1) > int(tick0), f"main keeps running after seal ({tick0}->{tick1})")

        # -- fork N -----------------------------------------------------------
        ids = [f"cand{i}" for i in range(args.fanout)]
        t0 = time.time()
        kids = manager.fork(sid, ids=ids)
        check([k.id for k in kids] == ids, f"forked {len(kids)}/{len(ids)} in {time.time() - t0:.2f}s")
        for k in kids:
            rc, _, _ = manager.exec_on_branch(k.id, f"echo {k.id} >> /tmp/f; echo {k.id} > /tmp/who")
            check(rc == 0, f"mutate {k.id}")
        time.sleep(0.8)
        for k in kids:
            f = manager.exec_on_branch(k.id, "cat /tmp/f")[1].split()
            who = manager.exec_on_branch(k.id, "cat /tmp/who")[1].strip()
            tick = manager.exec_on_branch(k.id, "cat /tmp/tick")[1].strip()
            check(f == ["hello", k.id] and who == k.id, f"{k.id} sees only its own writes ({f})")
            check(tick.isdigit() and int(tick) > int(tick0), f"{k.id} background job resumed ({tick0}->{tick})")
        main_f = manager.exec_command("cat /tmp/f")[1].split()
        check(main_f == ["hello"], f"main unaffected by forks ({main_f})")

        # -- the snapshot itself is frozen -----------------------------------
        if manager.snapshot_mode == "live":
            snap_machine = manager.snapshots[sid]
            def snap_tick() -> str:
                return subprocess.run(
                    [SMOLVM_BIN, "machine", "exec", "--name", snap_machine, "--", "cat", "/tmp/tick"],
                    capture_output=True, text=True, timeout=60,
                ).stdout.strip()
            a = snap_tick(); time.sleep(1.0); b = snap_tick()
            check(a == b, f"frozen snapshot machine does not advance ({a}=={b})")

        # -- restore main to the snapshot -------------------------------------
        manager.exec_command("echo scratch >> /tmp/f")
        t0 = time.time()
        ok = manager.restore(sid)
        check(ok, f"restore main -> {sid} in {time.time() - t0:.2f}s (now on branch {manager.current_branch_id})")
        f = manager.exec_command("cat /tmp/f")[1].split()
        check(f == ["hello"], f"restored state matches the snapshot ({f})")
        time.sleep(0.6)
        tick2 = manager.exec_command("cat /tmp/tick")[1].strip()
        check(tick2.isdigit() and int(tick2) > int(tick0), f"restored branch is live ({tick2})")

        # -- park and revive, discard the rest --------------------------------
        parked = manager.park_branch(kids[0].id)
        check(parked is not None, f"park {kids[0].id} -> {parked}")
        check(kids[0].id not in [b.id for b in manager.live_branches], "parked branch is no longer live")
        revived = manager.fork(parked, n=1) if parked else []
        check(len(revived) == 1, "revive the parked snapshot")
        if revived:
            who = manager.exec_on_branch(revived[0].id, "cat /tmp/who")[1].strip()
            check(who == kids[0].id, f"revived fork carries the parked state ({who})")
        for k in kids[1:]:
            check(manager.discard_branch(k.id), f"discard {k.id}")

        # -- copy out ----------------------------------------------------------
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "f.txt")
            ok = manager.copy_out("/tmp/f", dst)
            content = open(dst).read().split() if ok and os.path.exists(dst) else None
            check(content == ["hello"], f"copy_out /tmp/f -> host ({content})")
            src = os.path.join(tmp, "in.txt")
            open(src, "w").write("from-host\n")
            ok = manager.copy_in(src, "/tmp/in.txt")
            got = manager.exec_command("cat /tmp/in.txt")[1].strip()
            check(ok and got == "from-host", f"copy_in host -> /tmp/in.txt ({got!r})")

        print()
        print(manager.stats.print_stats())
        print(manager.print_snapshot_tree())
    finally:
        tag = manager.session_tag
        ok = manager.cleanup()
        left = subprocess.run([SMOLVM_BIN, "machine", "ls"], capture_output=True, text=True).stdout.count(f"sf-{tag}-")
        check(ok and left == 0, f"cleanup removed every machine of this session ({left} left)")

    print(f"\n{check.passed} passed, {check.failed} failed")
    return check.failed


if __name__ == "__main__":
    sys.exit(main())
