#!/usr/bin/env python3
import argparse
import csv
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

# developer tunables
DEFAULT_LOG_LEVEL = "INFO"             # logging level used in benchmarks
SLEEP_BETWEEN_PAIRS = 1.0                # seconds to wait between each snapshot pair
SLEEP_BETWEEN_CONFIGS = 5.0             # seconds to wait between different configurations

from decider import AlwaysTrueDecider
from controller.ckptlite_env_manager import CheckpointLiteBuildManager


# =========================
# Config and CLI parsing
# =========================


@dataclass
class BenchArgs:
    mem_sizes: List[int]
    fs_init_sizes: List[int]
    fs_delta_sizes: List[int]
    pairs: int
    repeat: int
    csv_path: str
    measure_restore: bool


def parse_csv_ints(s: str) -> List[int]:
    if not s:
        return []
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def parse_args() -> BenchArgs:
    p = argparse.ArgumentParser(description="Checkpoint-lite microbenchmark runner")
    p.add_argument("--mem-sizes", default="64,256,1024", help="Comma-separated MB list, e.g. 64,256,1024")
    p.add_argument("--fs-init-sizes", default="0,256", help="Comma-separated MB list for initial FS")
    p.add_argument("--fs-delta-sizes", default="0,32", help="Comma-separated MB list for per-pair FS delta")
    p.add_argument("--pairs", type=int, default=5, help="Snapshot-restore pairs per run")
    p.add_argument("--repeat", type=int, default=3, help="Repeat times per configuration")
    p.add_argument("--csv", dest="csv_path", default="./microbenchmark.csv", help="CSV output path")
    p.add_argument("--measure-restore", action="store_true", help="also time restore operations")

    a = p.parse_args()
    return BenchArgs(
        mem_sizes=parse_csv_ints(a.mem_sizes),
        fs_init_sizes=parse_csv_ints(a.fs_init_sizes),
        fs_delta_sizes=parse_csv_ints(a.fs_delta_sizes),
        pairs=int(a.pairs),
        repeat=int(a.repeat),
        csv_path=str(a.csv_path),
        measure_restore=bool(a.measure_restore),
    )


# =========================
# CSV Logger
# =========================


class CsvLogger:
    HEADER = [
        "timestamp",
        "mem_mb",
        "fs_init_mb",
        "fs_delta_mb",
        "repeat_idx",
        "pair_idx",
        "operation",
        "elapsed_ms",
        "comment",
    ]

    def __init__(self, path: str):
        self.path = path
        self._ensure_header()

    def _ensure_header(self) -> None:
        new_file = not os.path.exists(self.path)
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(self.HEADER)

    def log(self,
            op: str,
            elapsed_ms: float,
            mem_mb: int,
            fs_init_mb: int,
            fs_delta_mb: int,
            repeat_idx: int,
            pair_idx: int,
            comment: str = "",
            ) -> None:
        ts = time.strftime("%H:%M:%S", time.localtime())
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                ts,
                mem_mb,
                fs_init_mb,
                fs_delta_mb,
                repeat_idx,
                pair_idx,
                op,
                f"{elapsed_ms:.3f}",
                comment,
            ])


# =========================
# Helpers: timing, validation, info
# =========================


def ms_since(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000.0


def validate_exec_ok(manager: CheckpointLiteBuildManager, cmd: str = "pwd", timeout: float = 5.0) -> Tuple[bool, str, float]:
    t0 = time.perf_counter_ns()
    rc, out, err = manager.exec_command(cmd, timeout=timeout)
    elapsed_ms = ms_since(t0)
    ok = (rc == 0) and (not (err or "").strip())
    comment = "" if ok else f"rc={rc}; err={err.strip()}"
    return ok, comment, elapsed_ms


def read_vmrss_kb(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1])  # kB
        return None
    except Exception:
        return None


def du_bytes(path: str) -> Optional[int]:
    try:
        proc = subprocess.run(["du", "-sb", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            return None
        size_str = (proc.stdout or "").strip().split()[0]
        return int(size_str)
    except Exception:
        return None


def log_ps_bash_context(logger: logging.Logger,
                        pattern: str = "/bin/bash --norc --noprofile",
                        context: int = 3) -> None:
    """
    Log ps output around lines matching pattern with given context.
    """
    try:
        proc = subprocess.run(["ps", "-ef", "--forest"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if proc.returncode != 0:
            logger.warning(f"ps failed: {proc.stderr.strip()}")
            return
        lines = (proc.stdout or "").splitlines()
        idxs = [i for i, line in enumerate(lines) if pattern in line]
        if not idxs:
            logger.info(f"ps: no matches for pattern: {pattern}")
            return
        for k, i in enumerate(idxs, 1):
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            block = "\n".join(lines[start:end])
            logger.info(f"ps context ({k}/{len(idxs)}) around '{pattern}':\n{block}")
    except Exception as e:
        logger.warning(f"ps context logging failed: {e}")


# =========================
# Memhog controller
# =========================


class MemhogController:
    """Launch memhog via ``manager.exec_command`` using the wrapper script.

    The helper ``memhog.sh`` is placed in ``/app/app`` and backgrounds the
    real ``memhog`` program with ``&``. After invoking ``source`` we poll the
    pid file inside the chroot to obtain the PID.  The file sits at
    ``/app/app/{pid_filename}`` and may also be inspected from the host via
    ``manager.work_dir`` but we only use the exec interface.
    """

    def __init__(self, manager: CheckpointLiteBuildManager, pid_filename: str = "microbench.pid"):
        self.manager = manager
        self.pid_filename = pid_filename
        self.pid: Optional[int] = None

    def start(self, mem_mb: int, wait_timeout: float = 10.0) -> Optional[int]:
        if mem_mb <= 0:
            return None

        # remove any existing pid file first
        self.manager.exec_command(f"cd /app/app && rm -f {self.pid_filename}")

        cmd = f"source ./memhog.sh {mem_mb} {self.pid_filename}"
        self.manager.exec_command(cmd)

        # poll for the pid inside the chroot
        t0 = time.time()
        while time.time() - t0 < wait_timeout:
            rc, out, err = self.manager.exec_command(f"cat {self.pid_filename}")
            if rc == 0:
                maybe = (out or "").strip()
                if maybe.isdigit():
                    self.pid = int(maybe)
                    return self.pid
            time.sleep(0.05)
        raise TimeoutError("memhog pid file not available")

    def stop(self, timeout: float = 5.0) -> None:
        # try kill inside container
        if self.pid:
            self.manager.exec_command(f"kill {self.pid}")
        # cleanup file
        self.manager.exec_command(f"cd /app/app && rm -f {self.pid_filename}")
        self.pid = None


# =========================
# Benchmark runner
# =========================


def run_one_configuration(csvlog: CsvLogger,
                          mem_mb: int,
                          fs_init_mb: int,
                          fs_delta_mb: int,
                          pairs: int,
                          repeat_idx: int,
                          measure_restore: bool,
                          ) -> None:
    logger = logging.getLogger("microbench")

    # Build a fresh manager for each repeat of a configuration
    t0 = time.perf_counter_ns()
    manager = CheckpointLiteBuildManager()
    build_ms = ms_since(t0)
    csvlog.log("BUILD", build_ms, mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, 0, "")
    logger.info(f"BUILD done in {build_ms:.3f} ms")

    # Gentle pause between operations
    if SLEEP_BETWEEN_PAIRS > 0:
        time.sleep(SLEEP_BETWEEN_PAIRS)

    # Validate Build
    ok_build, comment_build, _ = validate_exec_ok(manager)
    if not ok_build:
        print("Cannot exec after build, with error message:")
        print(f"\t{comment_build}")
        print("Please manualy check. Press ENTER to continue...")
        input()
    else:
        logger.info(f"Post-BUILD exec validation passed")

    memhog = MemhogController(manager)
    try:
        # Initial FS generation (explicit modification) and du sample
        if fs_init_mb > 0:
            rc, out, err = manager.exec_command(f"/app/app/fsgen {fs_init_mb}")
            if rc != 0 or (err or "").strip():
                logging.warning(f"fsgen init failed rc={rc} err={err}")
            du_size = du_bytes(manager.work_dir)
            csvlog.log("INFO.du", du_size, mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, 0,
                       comment=f"du_B={du_size}")

        # Gentle pause between operations
        if SLEEP_BETWEEN_PAIRS > 0:
            time.sleep(SLEEP_BETWEEN_PAIRS)

        # Start memhog (memory modification) and sample VmRSS once
        pid: Optional[int] = None
        if mem_mb > 0:
            try:
                pid = memhog.start(mem_mb)
            except Exception as e:
                logger.error(f"memhog start failed: {e}")
                pid = None
            if pid is not None:
                vmrss = read_vmrss_kb(pid)
                csvlog.log("INFO.vmrss", float(vmrss), mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, 0,
                        comment=f"pid={pid};VmRSS_kB={vmrss}")

        # Main loop: snapshot-restore pairs
        for pair_idx in range(1, pairs + 1):
            # Snapshot
            s0 = time.perf_counter_ns()
            sid = manager.snapshot()
            snap_ms = ms_since(s0)

            # Gentle pause between operations
            if SLEEP_BETWEEN_PAIRS > 0:
                time.sleep(SLEEP_BETWEEN_PAIRS)

            # Validation
            ok_snap, comment_snap, _ = validate_exec_ok(manager)
            if not sid:
                comment_snap = (comment_snap + "; " if comment_snap else "") + "snapshot=None"
            csvlog.log("SNAPSHOT", snap_ms, mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, pair_idx, comment_snap)
            logger.info(f"SNAPSHOT[{pair_idx}] in {snap_ms:.3f} ms ({'ok' if ok_snap else 'bad'})")

            # Gentle pause between operations
            if SLEEP_BETWEEN_PAIRS > 0:
                time.sleep(SLEEP_BETWEEN_PAIRS)

            # optionally restore
            if measure_restore:
                if mem_mb > 0 and pid is not None:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                
                # Gentle pause between operations
                if SLEEP_BETWEEN_PAIRS > 0:
                    time.sleep(SLEEP_BETWEEN_PAIRS)

                # Restore
                r0 = time.perf_counter_ns()
                restored = manager.restore(sid) if sid else False
                rest_ms = ms_since(r0)

                # Gentle pause between operations
                if SLEEP_BETWEEN_PAIRS > 0:
                    time.sleep(SLEEP_BETWEEN_PAIRS)

                # Validation
                ok_res, comment_res, _ = validate_exec_ok(manager)
                if not restored:
                    comment_res = (comment_res + "; " if comment_res else "") + "restore=False"
                csvlog.log("RESTORE", rest_ms, mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, pair_idx, comment_res)
                logger.info(f"RESTORE[{pair_idx}] in {rest_ms:.3f} ms ({'ok' if ok_res else 'bad'})")

            # FS delta (explicit modification) and du sample
            if fs_delta_mb > 0:
                rc, out, err = manager.exec_command(f"/app/app/fsgen {fs_delta_mb}")
                if rc != 0 or (err or "").strip():
                    logging.warning(f"fsgen delta failed rc={rc} err={err}")
                du_size = du_bytes(manager.work_dir)
                csvlog.log("INFO.du", du_size, mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, pair_idx,
                           comment=f"du_B={du_size}")

            # Log host ps bash context after each pair (text log only)
            try:
                log_ps_bash_context(logger)
            except Exception:
                pass

            # Gentle pause between pairs
            if SLEEP_BETWEEN_PAIRS > 0:
                time.sleep(SLEEP_BETWEEN_PAIRS)

    finally:
        # Stop memhog and Cleanup manager
        if mem_mb > 0:
            try:
                memhog.stop()
            except Exception:
                pass
        try:
            c0 = time.perf_counter_ns()
            manager.cleanup()
            clean_ms = ms_since(c0)
            csvlog.log("CLEANUP", clean_ms, mem_mb, fs_init_mb, fs_delta_mb, repeat_idx, 0, "")
            logger.info(f"CLEANUP in {clean_ms:.3f} ms")
        except Exception as e:
            logger.warning(f"cleanup failed: {e}")


def run_benchmark(args: BenchArgs) -> None:
    logging.getLogger().setLevel(getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO))
    csvlog = CsvLogger(args.csv_path)

    configs = [
        (m, fi, fd)
        for m in args.mem_sizes
        for fi in args.fs_init_sizes
        for fd in args.fs_delta_sizes
    ]

    logging.info(f"Total configurations: {len(configs)}; pairs={args.pairs}; repeat={args.repeat}")

    for (mem_mb, fs_init_mb, fs_delta_mb) in configs:
        logging.info(f"=== Config mem={mem_mb}MB fs_init={fs_init_mb}MB fs_delta={fs_delta_mb}MB ===")
        for r in range(1, args.repeat + 1):
            logging.info(f"-- Repeat {r}/{args.repeat}")
            run_one_configuration(
                csvlog=csvlog,
                mem_mb=mem_mb,
                fs_init_mb=fs_init_mb,
                fs_delta_mb=fs_delta_mb,
                pairs=args.pairs,
                repeat_idx=r,
                measure_restore=args.measure_restore,
            )
        # Sleep between configurations for robustness
        if SLEEP_BETWEEN_CONFIGS > 0:
            time.sleep(SLEEP_BETWEEN_CONFIGS)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO),
        format="%(levelname)s %(message)s",
    )
    try:
        run_benchmark(args)
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()

