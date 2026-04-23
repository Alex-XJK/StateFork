#!/usr/bin/env python3
"""
Run the StateFork simulator over real artifact data under artifacts/artifacts/.

For each distinct benchmark *test name* (second segment of folder names split by __),
only the artifact folder with trace_type=ckptlite_trace and latest date+time
(last two segments) is used.

Writes JSON with signed percentage strings only (no absolute time or bytes), matching
simulator/main.py: time is signed % of total trace time; memory is % of restore bytes saved
(same line as "(memory saved)"). A leading '-' on speed means slower vs baseline.
Also includes simulation_failed (and simulation_incomplete) when a folder cannot be fully processed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# This file lives next to main.py under StateFork/simulator/.
_SIM_ROOT = Path(__file__).resolve().parent
if str(_SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SIM_ROOT))

from trace import CSVEnricher, TraceBuilder  # noqa: E402
from tree.tree_builder import TreeBuilder  # noqa: E402
from utils.ndjson_reader import read_ndjson  # noqa: E402

TARGET_TRACE_TYPE = "ckptlite_trace"


def format_signed_percent(value: float | None) -> str | None:
    """Format a signed percentage for JSON: '+1.23' = favorable, '-0.45' = unfavorable."""
    if value is None:
        return None
    v = 0.0 if abs(value) < 1e-15 else value
    raw = f"{v:+.6f}"
    s = raw.rstrip("0").rstrip(".")
    if s in ("+", "-"):
        s += "0"
    return s


def parse_artifact_dirname(name: str) -> tuple[str, str, tuple[str, str]] | None:
    """
    Expect: trace_type__test_name__YYYYMMDD__time_token
    Returns (trace_type, test_name, (date_s, time_s)) for ordering, or None if invalid.
    """
    parts = name.split("__")
    if len(parts) < 4:
        return None
    trace_type = parts[0]
    test_name = "__".join(parts[1:-2])
    date_s, time_s = parts[-2], parts[-1]
    return trace_type, test_name, (date_s, time_s)


def pick_latest_artifact_dirs(
    artifacts_root: Path, trace_type: str
) -> tuple[dict[str, Path], int]:
    """Map test_name -> newest artifact folder, filtered by trace type."""
    by_test: dict[str, tuple[tuple[str, str], Path]] = {}
    skipped_other_trace_types = 0
    for p in sorted(artifacts_root.iterdir()):
        if not p.is_dir():
            continue
        parsed = parse_artifact_dirname(p.name)
        if parsed is None:
            continue
        parsed_trace, test_name, key = parsed
        if parsed_trace != trace_type:
            skipped_other_trace_types += 1
            continue
        prev = by_test.get(test_name)
        if prev is None or key > prev[0]:
            by_test[test_name] = (key, p)
    return {name: tup[1] for name, tup in by_test.items()}, skipped_other_trace_types


def latest_results_run_dir(artifact_path: Path) -> Path | None:
    results = artifact_path / "results"
    if not results.is_dir():
        return None
    runs = sorted(d for d in results.iterdir() if d.is_dir())
    return runs[-1] if runs else None


def find_events_ndjson_paths(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("**/agent-logs/state/events.ndjson"))


def _pick_csv_from_list(logs: list[Path]) -> Path | None:
    """Prefer merge_*, then mcts_adaptive_log_*, then any other CSV (sorted)."""
    if not logs:
        return None

    def key(p: Path) -> tuple[int, str]:
        n = p.name.lower()
        if n.startswith("merge_"):
            return (0, n)
        if n.startswith("mcts_adaptive_log"):
            return (1, n)
        return (2, n)

    return sorted(logs, key=key)[0]


def pick_events_csv(agent_logs: Path) -> Path | None:
    """
    Use the same events CSV as a manual run: consider *.csv in agent-logs/ and in
    agent-logs/state/ together. If any file name starts with merge_, use that
    (e.g. merge_mcts_adaptive_log_*.csv next to state/events.ndjson); otherwise
    prefer mcts_adaptive_log_*, then any other CSV.
    """
    candidates: list[Path] = []
    candidates.extend(agent_logs.glob("*.csv"))
    state_dir = agent_logs / "state"
    if state_dir.is_dir():
        candidates.extend(state_dir.glob("*.csv"))

    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in candidates:
        key = p.resolve()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)

    if not uniq:
        return None

    merges = [p for p in uniq if p.name.lower().startswith("merge_")]
    if merges:
        return sorted(merges)[0]
    return _pick_csv_from_list(uniq)


def run_one_simulation(ndjson_path: Path, csv_path: Path) -> dict[str, Any]:
    """Run simulator in-process; suppress tree/trace print noise."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        events = read_ndjson(str(ndjson_path))
        treebuilder = TreeBuilder()
        treebuilder.build_from_events(events)
        tracebuilder = TraceBuilder()
        tracebuilder.build_from_events(events)
        enricher = CSVEnricher(tracebuilder)
        enricher.parse_csv(str(csv_path))
        enricher.attach_to_trace()
        treebuilder.annotate_virtual_physical(tracebuilder)
        delta, total_trace_time, bytes_saved, total_bytes = treebuilder.compute_total_delta(
            tracebuilder
        )

    # Signed % of total trace time: + = faster (net time saved vs replay), - = slower.
    saved_speed: float | None
    if total_trace_time and total_trace_time > 0:
        saved_speed = 100.0 * delta / total_trace_time
    else:
        saved_speed = None

    # Same as simulator/main.py "memory saved": share of restore bytes avoided on virtual path.
    saved_memory: float | None
    if total_bytes and total_bytes > 0:
        saved_memory = 100.0 * bytes_saved / total_bytes
    else:
        saved_memory = None

    return {
        "saved_speed": saved_speed,
        "saved_memory": saved_memory,
    }


def process_artifact_folder(artifact_path: Path) -> dict[str, Any]:
    run_dir = latest_results_run_dir(artifact_path)
    if run_dir is None:
        return {
            "simulation_failed": True,
            "failure_reason": "no results/ run directory found",
        }

    ndjson_paths = find_events_ndjson_paths(run_dir)
    if not ndjson_paths:
        return {
            "simulation_failed": True,
            "failure_reason": f"no agent-logs/state/events.ndjson under {run_dir}",
        }

    # One benchmark trial per artifact in practice; if several, average metrics.
    metrics_list: list[dict[str, Any]] = []
    errors: list[str] = []
    for ndjson_path in ndjson_paths:
        agent_logs = ndjson_path.parent.parent
        csv_path = pick_events_csv(agent_logs)
        if csv_path is None:
            errors.append(f"no CSV in {agent_logs}")
            continue
        try:
            metrics_list.append(run_one_simulation(ndjson_path, csv_path))
        except Exception as e:  # noqa: BLE001 — surface per-trial failures in batch output
            errors.append(f"{ndjson_path.relative_to(artifact_path)}: {e}")

    if not metrics_list:
        return {
            "simulation_failed": True,
            "failure_reason": "; ".join(errors) if errors else "no successful simulation",
        }

    def avg(key: str) -> float | None:
        vals = [m[key] for m in metrics_list if m.get(key) is not None]
        if not vals:
            return None
        return sum(vals) / len(vals)

    out: dict[str, Any] = {
        "simulation_failed": False,
        "simulation_incomplete": bool(errors),
        "saved_speed": format_signed_percent(avg("saved_speed")),
        "saved_memory": format_signed_percent(avg("saved_memory")),
    }
    if errors:
        out["trial_errors"] = errors
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("/users/alexxjk/artifacts/artifacts"),
        help="Directory containing artifact folders (trace__name__date__time)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_SIM_ROOT / "simulator_batch_results.json",
        help="Output JSON path (default: simulator_batch_results.json in this directory)",
    )
    args = parser.parse_args()
    root = args.artifacts_root.expanduser().resolve()
    out_path = args.output.expanduser().resolve()

    latest, skipped_other_trace_types = pick_latest_artifact_dirs(root, TARGET_TRACE_TYPE)
    results: list[dict[str, Any]] = []
    for test_name in sorted(latest.keys()):
        folder = latest[test_name]
        parsed = parse_artifact_dirname(folder.name)
        trace_type = parsed[0] if parsed else None
        merged = process_artifact_folder(folder)
        row: dict[str, Any] = {
            "name": test_name,
            "artifact_folder": folder.name,
            "trace_type": trace_type,
            "simulation_failed": merged.get("simulation_failed", True),
        }
        if merged.get("simulation_failed"):
            row["failure_reason"] = merged.get("failure_reason", "unknown")
            row["saved_speed"] = None
            row["saved_memory"] = None
        else:
            row["saved_speed"] = merged.get("saved_speed")
            row["saved_memory"] = merged.get("saved_memory")
            if merged.get("simulation_incomplete"):
                row["simulation_incomplete"] = True
                if merged.get("trial_errors"):
                    row["trial_errors"] = merged["trial_errors"]

        results.append(row)

    payload = {
        "artifacts_root": str(root),
        "trace_type_filter": TARGET_TRACE_TYPE,
        "skipped_other_trace_type_folders": skipped_other_trace_types,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "StateFork/simulator/batch_simulate_artifacts.py",
        "tests": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(results)} test rows to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
