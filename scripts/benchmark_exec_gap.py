#!/usr/bin/env python3
"""Benchmark Waypoint's restore-return-to-exec-ready gap.

The benchmark immediately issues repeated ``exec`` calls after restore returns.
It preserves every attempt and summarizes contiguous phases of like errors,
for example::

    socket_missing [0.01, 1.83] ms -> connection_refused [2.01, 4.12] ms
        -> success [4.31, 9.20] ms

Those phases are observations, not root-cause claims.  They provide evidence
for later correlation with scheduler, CRIU, and Unix-domain-socket tracing.
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from controller import EnvironmentManager, create_env_manager


ATTEMPT_FIELDS = [
    "timestamp",
    "memory_mb",
    "round",
    "snapshot_id",
    "restore_ms",
    "attempt",
    "attempt_start_ms",
    "attempt_elapsed_ms",
    "attempt_end_ms",
    "outcome",
    "return_code",
    "stdout",
    "stderr",
]

PHASE_FIELDS = [
    "timestamp",
    "memory_mb",
    "round",
    "snapshot_id",
    "phase",
    "outcome",
    "first_attempt",
    "last_attempt",
    "attempt_count",
    "start_ms",
    "end_ms",
    "duration_ms",
]


@dataclass(frozen=True)
class Attempt:
    number: int
    start_ms: float
    elapsed_ms: float
    return_code: int
    stdout: str
    stderr: str
    outcome: str

    @property
    def end_ms(self) -> float:
        return self.start_ms + self.elapsed_ms

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0


@dataclass(frozen=True)
class Phase:
    number: int
    outcome: str
    attempts: tuple[Attempt, ...]

    @property
    def start_ms(self) -> float:
        return self.attempts[0].start_ms

    @property
    def end_ms(self) -> float:
        return self.attempts[-1].end_ms


@dataclass(frozen=True)
class RoundResult:
    memory_mb: int
    round_number: int
    snapshot_id: str
    restore_ms: float
    attempts: tuple[Attempt, ...]

    @property
    def successful_attempt(self) -> Optional[Attempt]:
        return next((attempt for attempt in self.attempts if attempt.succeeded), None)

    @property
    def phases(self) -> tuple[Phase, ...]:
        return group_phases(self.attempts)


def parse_nonnegative_ints(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not values or any(item < 0 for item in values):
        raise argparse.ArgumentTypeError("values must be non-negative")
    return values


def classify_attempt(return_code: int, stderr: str) -> str:
    """Classify an observed result using return status and error text."""
    if return_code == 0:
        return "success"

    message = stderr.lower()
    # Test the specific transport errors before the generic socket class.
    if "no such file or directory" in message:
        return "socket_missing"
    if "connection refused" in message or "connect: refused" in message:
        return "connection_refused"
    if "permission denied" in message:
        return "permission_denied"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "connection reset" in message:
        return "connection_reset"
    if "dial unix" in message or ".sock" in message or "socket" in message:
        return "other_socket_error"
    return "exec_error"


def group_phases(attempts: Sequence[Attempt]) -> tuple[Phase, ...]:
    """Run-length encode adjacent attempts with the same outcome."""
    if not attempts:
        return ()

    phases: list[Phase] = []
    current: list[Attempt] = [attempts[0]]
    for attempt in attempts[1:]:
        if attempt.outcome == current[-1].outcome:
            current.append(attempt)
        else:
            phases.append(Phase(len(phases) + 1, current[0].outcome, tuple(current)))
            current = [attempt]
    phases.append(Phase(len(phases) + 1, current[0].outcome, tuple(current)))
    return tuple(phases)


def probe_until_ready(
    manager: EnvironmentManager,
    command: str,
    readiness_timeout_s: float,
    exec_timeout_s: float,
    retry_interval_s: float,
    restore_return_ns: int,
) -> tuple[Attempt, ...]:
    """Start immediately and retain every exec result until success/timeout."""
    deadline_ns = restore_return_ns + int(readiness_timeout_s * 1_000_000_000)
    attempts: list[Attempt] = []

    while True:
        start_ns = time.perf_counter_ns()
        return_code, stdout, stderr = manager.exec_command(
            command, timeout=exec_timeout_s
        )
        end_ns = time.perf_counter_ns()
        attempts.append(
            Attempt(
                number=len(attempts) + 1,
                start_ms=(start_ns - restore_return_ns) / 1_000_000,
                elapsed_ms=(end_ns - start_ns) / 1_000_000,
                return_code=return_code,
                stdout=stdout or "",
                stderr=stderr or "",
                outcome=classify_attempt(return_code, stderr or ""),
            )
        )
        if return_code == 0 or end_ns >= deadline_ns:
            return tuple(attempts)
        if retry_interval_s:
            time.sleep(retry_interval_s)


def run_round(
    manager: EnvironmentManager,
    memory_mb: int,
    round_number: int,
    command: str,
    readiness_timeout_s: float,
    exec_timeout_s: float,
    retry_interval_s: float,
    pre_snapshot_settle_s: float,
    post_snapshot_settle_s: float,
) -> RoundResult:
    # Setup tolerance is deliberately outside the measured restore-to-exec gap.
    if pre_snapshot_settle_s:
        time.sleep(pre_snapshot_settle_s)

    print("  checking exec before snapshot...", flush=True)
    return_code, _, stderr = manager.exec_command(command, timeout=exec_timeout_s)
    if return_code != 0:
        raise RuntimeError(f"pre-snapshot exec failed (rc={return_code}): {stderr}")

    print("  creating snapshot...", flush=True)
    snapshot_id = manager.snapshot()
    if snapshot_id is None:
        raise RuntimeError("Waypoint snapshot failed")

    if post_snapshot_settle_s:
        time.sleep(post_snapshot_settle_s)

    print("  restoring, then probing exec immediately...", flush=True)
    restore_start_ns = time.perf_counter_ns()
    # Keep this benchmark on the manager's current (default) branch. Waypoint
    # also exposes fork-specific operations, but restore() is the sequential
    # snapshot/restore/exec workflow this benchmark measures.
    restored = manager.restore(snapshot_id)
    # Benchmark origin: keep the path to the first exec free of prints/sleeps.
    restore_end_ns = time.perf_counter_ns()
    if not restored:
        raise RuntimeError(f"Waypoint restore failed for snapshot {snapshot_id}")

    attempts = probe_until_ready(
        manager,
        command,
        readiness_timeout_s,
        exec_timeout_s,
        retry_interval_s,
        restore_end_ns,
    )
    return RoundResult(
        memory_mb=memory_mb,
        round_number=round_number,
        snapshot_id=snapshot_id,
        restore_ms=(restore_end_ns - restore_start_ns) / 1_000_000,
        attempts=attempts,
    )


def append_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)
        output.flush()


def write_result(attempts_path: Path, phases_path: Path, result: RoundResult) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    common = {
        "timestamp": timestamp,
        "memory_mb": result.memory_mb,
        "round": result.round_number,
        "snapshot_id": result.snapshot_id,
    }
    append_csv(
        attempts_path,
        ATTEMPT_FIELDS,
        [
            {
                **common,
                "restore_ms": f"{result.restore_ms:.6f}",
                "attempt": attempt.number,
                "attempt_start_ms": f"{attempt.start_ms:.6f}",
                "attempt_elapsed_ms": f"{attempt.elapsed_ms:.6f}",
                "attempt_end_ms": f"{attempt.end_ms:.6f}",
                "outcome": attempt.outcome,
                "return_code": attempt.return_code,
                "stdout": attempt.stdout.strip(),
                "stderr": attempt.stderr.strip(),
            }
            for attempt in result.attempts
        ],
    )
    append_csv(
        phases_path,
        PHASE_FIELDS,
        [
            {
                **common,
                "phase": phase.number,
                "outcome": phase.outcome,
                "first_attempt": phase.attempts[0].number,
                "last_attempt": phase.attempts[-1].number,
                "attempt_count": len(phase.attempts),
                "start_ms": f"{phase.start_ms:.6f}",
                "end_ms": f"{phase.end_ms:.6f}",
                "duration_ms": f"{phase.end_ms - phase.start_ms:.6f}",
            }
            for phase in result.phases
        ],
    )


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def start_memory_workload(
    manager: EnvironmentManager, memory_mb: int, timeout_s: float = 30.0
) -> None:
    """Start and page-touch a resident child process before snapshots."""
    if memory_mb == 0:
        return

    ready_file = f"/tmp/statefork-memory-{os.getpid()}-{memory_mb}.ready"
    code = (
        "import pathlib,sys,time;"
        "data=bytearray(int(sys.argv[1])*1024*1024);"
        "data[::4096]=b'x'*len(data[::4096]);"
        "pathlib.Path(sys.argv[2]).touch();"
        "time.sleep(86400)"
    )
    command = (
        f"rm -f {ready_file}; "
        f"python3 -c {shell_quote(code)} {memory_mb} {ready_file} "
        "</dev/null >/dev/null 2>&1 &"
    )
    return_code, _, stderr = manager.exec_command(command, timeout=5.0)
    if return_code != 0:
        raise RuntimeError(f"could not start memory workload: {stderr}")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        return_code, _, _ = manager.exec_command(
            f"test -f {ready_file}", timeout=5.0
        )
        if return_code == 0:
            return
        time.sleep(0.05)
    raise TimeoutError(f"{memory_mb} MB workload did not become ready")


def percentile(values: Sequence[float], proportion: float) -> float:
    ordered = sorted(values)
    index = max(0, int(len(ordered) * proportion + 0.999999) - 1)
    return ordered[index]


def print_round(result: RoundResult) -> None:
    phase_text = " -> ".join(
        f"{phase.outcome}[{phase.start_ms:.3f},{phase.end_ms:.3f}]ms"
        f"x{len(phase.attempts)}"
        for phase in result.phases
    )
    print(
        f"memory={result.memory_mb}MB round={result.round_number} "
        f"restore={result.restore_ms:.3f}ms: {phase_text}"
    )


def print_summary(results: list[RoundResult]) -> None:
    print("\nSummary (gap upper bound = first successful exec return)")
    for memory_mb in sorted({result.memory_mb for result in results}):
        group = [result for result in results if result.memory_mb == memory_mb]
        gaps = [
            result.successful_attempt.end_ms
            for result in group
            if result.successful_attempt is not None
        ]
        errors = Counter(
            attempt.outcome
            for result in group
            for attempt in result.attempts
            if not attempt.succeeded
        )
        phase_paths = Counter(
            " -> ".join(phase.outcome for phase in result.phases)
            for result in group
        )
        if gaps:
            timing = (
                f"median={statistics.median(gaps):.3f}ms, "
                f"p95={percentile(gaps, 0.95):.3f}ms, max={max(gaps):.3f}ms"
            )
        else:
            timing = "no successful exec"
        common_path = phase_paths.most_common(1)[0]
        print(
            f"  {memory_mb}MB: {len(gaps)}/{len(group)} ready; {timing}; "
            f"errors={dict(errors)}; common_path={common_path[0]} "
            f"({common_path[1]}/{len(group)})"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Waypoint restore-to-exec readiness phases"
    )
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument(
        "--memory-mb",
        type=parse_nonnegative_ints,
        default=[0],
        help="comma-separated resident process sizes; each uses a fresh session",
    )
    parser.add_argument(
        "--attempts-csv", type=Path, default=Path("waypoint-exec-attempts.csv")
    )
    parser.add_argument(
        "--phases-csv", type=Path, default=Path("waypoint-exec-phases.csv")
    )
    parser.add_argument("--dockerfile-dir", default=str(REPO_ROOT))
    parser.add_argument("--command", default="true")
    parser.add_argument("--readiness-timeout", type=float, default=10.0)
    parser.add_argument("--exec-timeout", type=float, default=5.0)
    parser.add_argument(
        "--retry-interval",
        type=float,
        default=0.0,
        help="seconds between attempts; zero retries as quickly as exec permits",
    )
    parser.add_argument("--round-pause", type=float, default=0.0)
    parser.add_argument(
        "--session-settle", type=float, default=5.0,
        help="unmeasured seconds after creating each Waypoint session",
    )
    parser.add_argument(
        "--pre-snapshot-settle", type=float, default=2.0,
        help="unmeasured seconds before each preflight exec and snapshot",
    )
    parser.add_argument(
        "--post-snapshot-settle", type=float, default=1.0,
        help="unmeasured seconds between snapshot completion and restore",
    )
    args = parser.parse_args()

    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    if args.readiness_timeout <= 0 or args.exec_timeout <= 0:
        parser.error("timeouts must be positive")
    intervals = (
        args.retry_interval,
        args.round_pause,
        args.session_settle,
        args.pre_snapshot_settle,
        args.post_snapshot_settle,
    )
    if any(interval < 0 for interval in intervals):
        parser.error("intervals must be non-negative")
    return args


def main() -> int:
    args = parse_args()
    results: list[RoundResult] = []

    for memory_mb in args.memory_mb:
        print(f"===== Memory Workload: {memory_mb} MB =====", flush=True)
        manager: Optional[EnvironmentManager] = None
        try:
            print("Creating Waypoint session...", flush=True)
            manager = create_env_manager(
                "waypoint_build", dockerfile_dir=args.dockerfile_dir
            )
            if args.session_settle:
                time.sleep(args.session_settle)
            print(f"Preparing {memory_mb} MB memory workload...", flush=True)
            start_memory_workload(manager, memory_mb)
            for round_number in range(1, args.rounds + 1):
                print(
                    f"----- Round {round_number}/{args.rounds} "
                    f"(memory={memory_mb} MB) -----",
                    flush=True,
                )
                result = run_round(
                    manager=manager,
                    memory_mb=memory_mb,
                    round_number=round_number,
                    command=args.command,
                    readiness_timeout_s=args.readiness_timeout,
                    exec_timeout_s=args.exec_timeout,
                    retry_interval_s=args.retry_interval,
                    pre_snapshot_settle_s=args.pre_snapshot_settle,
                    post_snapshot_settle_s=args.post_snapshot_settle,
                )
                write_result(args.attempts_csv, args.phases_csv, result)
                results.append(result)
                print_round(result)
                if args.round_pause:
                    print(f"  pausing {args.round_pause:g}s before next round...", flush=True)
                    time.sleep(args.round_pause)
        finally:
            if manager is not None:
                print("Cleaning up Waypoint session...", flush=True)
                manager.cleanup()

    print_summary(results)
    print(f"\nAttempt data: {args.attempts_csv}")
    print(f"Phase data:   {args.phases_csv}")
    return 0 if all(result.successful_attempt for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
