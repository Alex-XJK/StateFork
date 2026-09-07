#!/usr/bin/env python3
"""End-to-end fork-workflow demo and regression test for the Waypoint backend.

The script drives one Waypoint session through the sequence StateFork is built
for, an agent-style search:

    build one session from a Dockerfile (or ``init --shell`` from a rootfs)
      -> prepare state in ``main`` (files, cwd, a variable, a function, a job)
      -> seal it as the first checkpoint
      -> fork N candidates, run their work concurrently, score them
      -> seal the winner, park the runner-up, discard the rest
      -> fork the next generation from the winner ... (``--generations`` times)
      -> backtrack with ``restore()``, branch an alternative, revive a parked fork
      -> export the winning state to the host, print statistics, clean up

Every step asserts the semantics documented in ``controller/README.md``:
isolation between forks, inheritance of hidden shell state, the protected
``main`` fork, the explicit two-primitive ``restore`` macro, lossless ``park``,
and a clean teardown.  The exit code is the number of failed assertions, so the
script doubles as the integration test for the Waypoint backend.

Requirements: root (CRIU/OverlayFS), Waypoint v0.7.0+, and either buildah with
a Dockerfile context (default: this repository's Dockerfile) or a root
filesystem directory that contains ``/bin/bash`` (``--rootfs``).

Examples::

    sudo -E .venv/bin/python scripts/search_workflow_demo.py
    sudo -E .venv/bin/python scripts/search_workflow_demo.py --dockerfile-dir ctx --fanout 8 --generations 4
    sudo -E .venv/bin/python scripts/search_workflow_demo.py --rootfs /srv/rootfs --json result.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from controller import ForkableEnvironmentManager, create_env_manager  # noqa: E402
from controller.base_env_manager import DEFAULT_BRANCH_ID  # noqa: E402

WORKSPACE = "/statefork-search"  # working directory of the workload inside the environment
TOKEN = "warm-token"  # hidden shell state that must survive every checkpoint and fork


class Report:
    """Collects PASS/FAIL checks and phase timings."""

    def __init__(self, quiet: bool = False) -> None:
        self.checks: list[tuple[str, bool, str]] = []
        self.timings: list[dict] = []
        self.quiet = quiet

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.checks.append((name, ok, detail))
        if not ok or not self.quiet:
            suffix = f"  -- {detail}" if (detail and not ok) else ""
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}", flush=True)
        return ok

    def timed(self, label: str, fn, *args, **kwargs):
        start = time.perf_counter()
        result = fn(*args, **kwargs)
        elapsed = time.perf_counter() - start
        self.timings.append({"phase": label, "seconds": round(elapsed, 4)})
        return result, elapsed

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if not ok)

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.checks if ok)


@dataclass
class Generation:
    number: int
    frontier: str
    candidates: list[str]
    moves: dict[str, Optional[int]]
    expected_winner: str
    values: dict[str, Optional[int]] = field(default_factory=dict)
    winner: Optional[str] = None
    winner_value: Optional[int] = None
    winner_checkpoint: Optional[str] = None
    runner_up: Optional[str] = None
    runner_up_value: Optional[int] = None
    parked_checkpoint: Optional[str] = None
    fork_seconds: float = 0.0
    exec_seconds: float = 0.0
    seal_seconds: float = 0.0
    backend_restore_durations: list[str] = field(default_factory=list)


def section(title: str) -> None:
    print(f"\n== {title}", flush=True)


def live_ids(manager: ForkableEnvironmentManager) -> set[str]:
    return {branch.id for branch in manager.list_branches()}


def candidate_command(candidate: str, move: int) -> str:
    """One bash command line per candidate.

    The checks run in a subshell so a failed precondition cannot end the
    fork's persistent shell (a bare ``exit`` would).  ``LAST_MOVE`` is exported
    outside the subshell so hidden shell state diverges between candidates too.
    """
    return (
        f"export LAST_MOVE={move}; ("
        f'test "$SEARCH_TOKEN" = {TOKEN} || exit 90; '
        f'test "$PWD" = {WORKSPACE} || exit 91; '
        f'kill -0 "$SEARCH_JOB" 2>/dev/null || exit 92; '
        f"search_helper >/dev/null || exit 93; "
        f"value=$(( $(cat state.txt) + {move} )); "
        f'echo "$value" > state.txt; echo {candidate} > candidate.txt; echo "$value")'
    )


def build_manager(args: argparse.Namespace) -> ForkableEnvironmentManager:
    if args.rootfs:
        manager = create_env_manager("waypoint_build", dockerfile_dir=args.rootfs, build=False)
    else:
        manager = create_env_manager("waypoint_build", dockerfile_dir=args.dockerfile_dir, build=True)
    if not isinstance(manager, ForkableEnvironmentManager):
        raise RuntimeError("the Waypoint backend did not provide the forkable capability")
    return manager


def prepare(manager: ForkableEnvironmentManager, report: Report, target: int, prep_dir: Path) -> str:
    section("Prepare main: copy preparation files in, build hidden shell state, seal")
    (prep_dir / "config.txt").write_text(f"target={target}\n")
    (prep_dir / "note with spaces.txt").write_text("preparation payload\n")

    rc, _, err = manager.exec_command(f"mkdir -p {WORKSPACE}")
    report.check("workspace directory created in main", rc == 0, err)
    report.check(
        "preparation directory copied into main (copy_in)",
        manager.copy_in(str(prep_dir), f"{WORKSPACE}/prep"),
    )
    rc, out, err = manager.exec_command(
        f"cd {WORKSPACE}; echo 0 > state.txt; export SEARCH_TOKEN={TOKEN}; "
        "search_helper() { echo helper-ok; }; "
        "sleep 86400 </dev/null >/dev/null 2>&1 & export SEARCH_JOB=$!; "
        "cat prep/config.txt 'prep/note with spaces.txt'; search_helper"
    )
    report.check(
        "main prepared: cwd, variable, function, background job, copied files",
        rc == 0 and f"target={target}" in out and "preparation payload" in out and "helper-ok" in out,
        f"rc={rc} out={out!r} err={err!r}",
    )

    prepared, seconds = report.timed("seal prepared", manager.snapshot)
    report.check("prepared state sealed as a checkpoint", prepared is not None)
    print(f"  prepared checkpoint: {prepared} ({seconds:.2f}s)")

    rc, out, _ = manager.exec_command(
        'echo "$PWD $SEARCH_TOKEN"; kill -0 "$SEARCH_JOB" && echo job-alive; search_helper'
    )
    report.check(
        "hidden shell state survived sealing main (dump + resume)",
        out.split() == [WORKSPACE, TOKEN, "job-alive", "helper-ok"],
        out,
    )
    return prepared


def run_generation(
    manager: ForkableEnvironmentManager,
    report: Report,
    number: int,
    frontier: str,
    frontier_fork: Optional[str],
    current_value: int,
    fanout: int,
    target: int,
    exec_timeout: float,
    winners_so_far: list[str],
) -> Generation:
    section(f"Generation {number}: fork {fanout} candidates from {frontier}, run, score, seal")
    ids = [f"g{number}-c{i}" for i in range(1, fanout + 1)]
    remaining = max(0, target - current_value)
    step = max(1, remaining // fanout)
    moves: dict[str, Optional[int]] = {ids[0]: None}  # candidate 1 deliberately fails
    for index, candidate in enumerate(ids[1:], start=1):
        moves[candidate] = index * step
    expected = {c: current_value + m for c, m in moves.items() if m is not None}
    expected_winner = min(expected, key=lambda c: (abs(target - expected[c]), ids.index(c)))
    gen = Generation(number, frontier, ids, moves, expected_winner)

    forks, gen.fork_seconds = report.timed(f"g{number} fork x{fanout}", manager.fork, frontier, ids=ids)
    gen.backend_restore_durations = [getattr(f, "restore_duration", None) or "" for f in forks]
    report.check(
        f"g{number}: {fanout} candidates materialized concurrently",
        [f.id for f in forks] == ids,
        str([f.id for f in forks]),
    )
    print(f"  fork batch {gen.fork_seconds:.2f}s; backend restore durations: {gen.backend_restore_durations}")

    def work(candidate: str):
        move = moves[candidate]
        if move is None:
            command = f'export LAST_MOVE=none; echo "{candidate}: deliberately failing candidate"; false'
        else:
            command = candidate_command(candidate, move)
        return candidate, manager.exec_on_branch(candidate, command, timeout=exec_timeout)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        results = dict(pool.map(work, ids))
    gen.exec_seconds = time.perf_counter() - start
    report.timings.append({"phase": f"g{number} exec x{fanout}", "seconds": round(gen.exec_seconds, 4)})
    print(f"  exec batch {gen.exec_seconds:.2f}s")

    rc, _, _ = results[ids[0]]
    report.check(f"g{number}: rejected candidate reports a non-zero exit code", rc != 0, f"rc={rc}")
    rc, out, _ = manager.exec_on_branch(ids[0], "echo still-usable")
    report.check(f"g{number}: rejected candidate's shell remains usable", rc == 0 and out.strip() == "still-usable", out)

    for candidate in ids[1:]:
        rc, out, err = results[candidate]
        ok = rc == 0 and out.strip().isdigit()
        report.check(f"{candidate}: ran with inherited cwd/variable/function/job", ok, f"rc={rc} out={out!r} err={err!r}")
        gen.values[candidate] = int(out.strip()) if ok else None
    report.check(f"g{number}: candidate arithmetic matches the expected moves", gen.values == expected, f"{gen.values} vs {expected}")

    scored = sorted((abs(target - v), ids.index(c), c) for c, v in gen.values.items() if v is not None)
    if not scored:
        report.check(f"g{number}: at least one candidate produced a score", False)
        return gen
    gen.winner = scored[0][2]
    gen.winner_value = gen.values[gen.winner]
    report.check(f"g{number}: expected winner {expected_winner} selected", gen.winner == expected_winner, gen.winner)
    if len(scored) > 1:
        gen.runner_up = scored[1][2]
        gen.runner_up_value = gen.values[gen.runner_up]

    for candidate in ids[1:]:
        rc, out, _ = manager.exec_on_branch(candidate, f"cd {WORKSPACE} && cat candidate.txt state.txt && echo $LAST_MOVE")
        report.check(
            f"{candidate}: sees only its own edits after all candidates finished",
            out.split() == [candidate, str(gen.values[candidate]), str(moves[candidate])],
            out,
        )
    if frontier_fork is not None:
        rc, out, _ = manager.exec_on_branch(frontier_fork, f"cat {WORKSPACE}/state.txt")
        report.check(f"g{number}: frontier fork {frontier_fork} untouched by its descendants", out.strip() == str(current_value), out)

    gen.winner_checkpoint, gen.seal_seconds = report.timed(f"g{number} seal winner", manager.snapshot_branch, gen.winner)
    report.check(f"g{number}: winner {gen.winner} sealed as a checkpoint", gen.winner_checkpoint is not None)
    if gen.winner_checkpoint:
        report.check(
            f"g{number}: winner checkpoint parents onto the frontier",
            manager.snapshot_graph[gen.winner_checkpoint].parent_id == frontier,
            str(manager.snapshot_graph[gen.winner_checkpoint]),
        )
    rc, out, _ = manager.exec_on_branch(gen.winner, f"cat {WORKSPACE}/state.txt; echo $LAST_MOVE")
    report.check(
        f"g{number}: winner resumed with its state after the destructive seal",
        out.split() == [str(gen.winner_value), str(moves[gen.winner])],
        out,
    )
    print(f"  winner {gen.winner} value={gen.winner_value} -> checkpoint {gen.winner_checkpoint} ({gen.seal_seconds:.2f}s)")

    if gen.runner_up:
        gen.parked_checkpoint = manager.park_branch(gen.runner_up)
        report.check(f"g{number}: runner-up {gen.runner_up} parked losslessly", gen.parked_checkpoint is not None)
        if gen.parked_checkpoint:
            report.check(
                f"g{number}: parked checkpoint parents onto the frontier",
                manager.snapshot_graph[gen.parked_checkpoint].parent_id == frontier,
            )
    for candidate in ids:
        if candidate in (gen.winner, gen.runner_up):
            continue
        report.check(f"{candidate}: discarded", manager.discard_branch(candidate))
    expected_live = {DEFAULT_BRANCH_ID, gen.winner, *winners_so_far}
    report.check(f"g{number}: live forks are main plus the winners", live_ids(manager) == expected_live, str(sorted(live_ids(manager))))
    return gen


def backtrack(
    manager: ForkableEnvironmentManager,
    report: Report,
    prepared: str,
    generations: list[Generation],
) -> None:
    section("Backtrack: restore(), alternative branch, restore again, park back to main, revive")
    last = generations[-1]
    report.check("current branch is still main after the search", manager.current_branch_id == DEFAULT_BRANCH_ID)
    rc, out, _ = manager.exec_command(f"cat {WORKSPACE}/state.txt")
    report.check("main's own state is untouched by the search", out.strip() == "0", out)

    ok, seconds = report.timed("restore(prepared)", manager.restore, prepared)
    backtrack_fork = manager.current_branch_id
    report.check("restore(prepared) succeeded", ok)
    report.check(
        "restore moved the current branch to a fresh fork (main is never destroyed)",
        backtrack_fork != DEFAULT_BRANCH_ID and {backtrack_fork, DEFAULT_BRANCH_ID} <= live_ids(manager),
        backtrack_fork,
    )
    rc, out, _ = manager.exec_command(f'cd {WORKSPACE}; cat state.txt; echo "$SEARCH_TOKEN"; test ! -f candidate.txt && echo no-candidate')
    report.check("backtracked fork has the prepared files and warm shell state", out.split() == ["0", TOKEN, "no-candidate"], out)

    manager.exec_command(f"echo 7 > {WORKSPACE}/state.txt; echo alternate > {WORKSPACE}/candidate.txt")
    alternate = manager.snapshot()
    report.check("alternative branch sealed from the backtracked fork", alternate is not None and manager.snapshot_graph[alternate].parent_id == prepared)
    children = manager.fork(alternate, ids=["alt-child"])
    rc, out, _ = manager.exec_on_branch("alt-child", f"cd {WORKSPACE}; cat state.txt candidate.txt")
    report.check("alternative checkpoint can be forked recursively", len(children) == 1 and out.split() == ["7", "alternate"], out)

    ok = manager.restore(last.winner_checkpoint)
    moved_fork = manager.current_branch_id
    report.check("restore(last winner) from a non-main fork succeeded", ok and moved_fork not in (backtrack_fork, DEFAULT_BRANCH_ID))
    report.check("the departing fork was destroyed by contract", backtrack_fork not in live_ids(manager))
    rc, out, _ = manager.exec_command(f"cat {WORKSPACE}/state.txt")
    report.check("current branch now carries the last winner's state", out.strip() == str(last.winner_value), out)

    parked = manager.park_branch(moved_fork)
    report.check("parking the current fork returns to main", parked is not None and manager.current_branch_id == DEFAULT_BRANCH_ID)
    report.check("parked checkpoint parents onto the last winner", parked is not None and manager.snapshot_graph[parked].parent_id == last.winner_checkpoint)

    first = generations[0]
    if first.parked_checkpoint:
        revived = manager.fork(first.parked_checkpoint, ids=["revived"])
        rc, out, _ = manager.exec_on_branch("revived", f"cat {WORKSPACE}/state.txt; echo $LAST_MOVE")
        report.check(
            "generation-1 runner-up revived from its parked checkpoint with its state",
            len(revived) == 1 and out.split() == [str(first.runner_up_value), str(first.moves[first.runner_up])],
            out,
        )
    for gen in generations:
        rc, out, _ = manager.exec_on_branch(gen.winner, f"cat {WORKSPACE}/state.txt")
        report.check(f"winner {gen.winner} is unaffected by later generations and backtracking", out.strip() == str(gen.winner_value), out)
    report.check("discard refuses main", manager.discard_branch(DEFAULT_BRANCH_ID) is False)
    report.check("park refuses main", manager.park_branch(DEFAULT_BRANCH_ID) is None)


def export(manager: ForkableEnvironmentManager, report: Report, last: Generation, export_dir: Path) -> None:
    section(f"Export the winning state to {export_dir}")
    export_dir.mkdir(parents=True, exist_ok=True)
    final = export_dir / "final"
    report.check("copy_out_branch of the winner's workspace", manager.copy_out_branch(last.winner, WORKSPACE, str(final)))
    state = final / "state.txt"
    report.check("exported state.txt holds the winning value", state.exists() and state.read_text().strip() == str(last.winner_value))
    report.check("copied-in preparation survived the whole checkpoint chain", (final / "prep" / "note with spaces.txt").exists())
    single = export_dir / "candidate.txt"
    report.check(
        "copy_out_branch of a single file",
        manager.copy_out_branch(last.winner, f"{WORKSPACE}/candidate.txt", str(single)) and single.read_text().strip() == last.winner,
    )
    report.check("copy_out of a missing path is reported, not raised", manager.copy_out_branch(last.winner, f"{WORKSPACE}/missing", str(export_dir / "x")) is False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dockerfile-dir", default=str(REPO_ROOT), help="Dockerfile context to build (default: this repository)")
    source.add_argument("--rootfs", help="root filesystem directory for `waypoint init --shell` instead of a build")
    parser.add_argument("--fanout", type=int, default=4, help="candidates per generation (>= 2; candidate 1 always fails)")
    parser.add_argument("--generations", type=int, default=3, help="search generations (>= 1)")
    parser.add_argument("--target", type=int, default=1000, help="value the search moves towards")
    parser.add_argument("--exec-timeout", type=float, default=120.0, help="seconds allowed per candidate command")
    parser.add_argument("--export-dir", help="host directory for exported results (default: a temp dir)")
    parser.add_argument("--json", help="write a machine-readable summary to this file")
    parser.add_argument("--keep-session", action="store_true", help="skip cleanup and print the session id")
    parser.add_argument("--quiet", action="store_true", help="print only failures and phase summaries")
    parser.add_argument("--verbose", action="store_true", help="show controller INFO logging")
    args = parser.parse_args()
    if args.fanout < 2:
        parser.error("--fanout must be at least 2")
    if args.generations < 1:
        parser.error("--generations must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        print("This demo drives Waypoint (CRIU/OverlayFS) and must run as root.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    report = Report(quiet=args.quiet)
    export_dir = Path(args.export_dir) if args.export_dir else Path(tempfile.mkdtemp(prefix="statefork-search-export-"))
    summary: dict = {"fanout": args.fanout, "generations": args.generations, "target": args.target}

    section("Build one session" + (f" from rootfs {args.rootfs}" if args.rootfs else f" from Dockerfile in {args.dockerfile_dir}"))
    manager, seconds = report.timed("build session", build_manager, args)
    session_id = getattr(manager, "session_id", "?")
    work_dir = getattr(manager, "work_dir", None)
    summary["session"] = session_id
    print(f"  session {session_id} ready in {seconds:.1f}s; initial checkpoint {manager.current_snapshot}")
    report.check("manager exposes the forkable capability", isinstance(manager, ForkableEnvironmentManager))
    report.check("main is the only live fork after build", live_ids(manager) == {DEFAULT_BRANCH_ID}, str(live_ids(manager)))

    generations: list[Generation] = []
    try:
        with tempfile.TemporaryDirectory(prefix="statefork-search-prep-") as prep:
            prepared = prepare(manager, report, args.target, Path(prep))
        frontier, frontier_fork, value = prepared, None, 0
        for number in range(1, args.generations + 1):
            gen = run_generation(
                manager, report, number, frontier, frontier_fork, value, args.fanout, args.target,
                args.exec_timeout, [g.winner for g in generations],
            )
            generations.append(gen)
            if gen.winner_checkpoint is None:
                print("  stopping: no winner checkpoint", flush=True)
                break
            frontier, frontier_fork, value = gen.winner_checkpoint, gen.winner, gen.winner_value
        if generations and generations[-1].winner_checkpoint:
            backtrack(manager, report, prepared, generations)
            export(manager, report, generations[-1], export_dir)

        section("Snapshot tree, timings and statistics")
        print(manager.print_snapshot_tree())
        print(f"  {'generation':<11}{'fork batch':>12}{'exec batch':>12}{'seal':>8}   winner value")
        for gen in generations:
            print(f"  g{gen.number:<10}{gen.fork_seconds:>11.2f}s{gen.exec_seconds:>11.2f}s{gen.seal_seconds:>7.2f}s   {gen.winner_value}")
        print(manager.stats.print_stats())
        if not args.quiet:
            print(manager.stats.print_size_details())
        stats = manager.stats.get_all_statistics()
        summary["time_stats"] = {op: asdict(s) for op, s in stats.time.items()}
        summary["size_stats"] = {name: asdict(s) for name, s in stats.size.items()}
        summary["generations"] = [asdict(g) for g in generations]
        summary["final_value"] = generations[-1].winner_value if generations else None
        summary["export_dir"] = str(export_dir)
    finally:
        if args.keep_session:
            print(f"\n== keeping session {session_id} (cleanup skipped)")
        else:
            section("Cleanup")
            ok, seconds = report.timed("cleanup", manager.cleanup)
            report.check("cleanup succeeded", ok)
            if work_dir:
                session_dir = os.path.dirname(work_dir)
                if os.path.exists(session_dir):
                    print(f"  note: {session_dir} still exists (Waypoint preserve_session_on_cleanup?)")
            print(f"  cleanup took {seconds:.2f}s")
        summary["timings"] = report.timings
        summary["checks_passed"], summary["checks_failed"] = report.passed, report.failed
        summary["failures"] = [{"check": n, "detail": d} for n, ok, d in report.checks if not ok]
        if args.json:
            Path(args.json).write_text(json.dumps(summary, indent=2) + "\n")
        print(f"\n===== {report.passed} passed, {report.failed} failed =====")
        for name, ok, detail in report.checks:
            if not ok:
                print(f"  FAIL {name}: {detail[:300]}")
    return min(report.failed, 125)


if __name__ == "__main__":
    sys.exit(main())
