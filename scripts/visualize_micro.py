#!/usr/bin/env python3

import argparse
import csv
import math
import os
from collections import defaultdict
from typing import Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt


# Built-in reference datasets for overlay (line-only, no distribution).
# Units: x in MB, y in milliseconds.
REF_DATA = {
	"fsinit_only": {
		"Docker": [
			(0.0, 56.0),
			(1024.0, 4586.0),
			(2048.0, 6391.0),
		],
		"Podman": [
            (0.0, 54.0),
            (1024.0, 6693.0),
            (2048.0, 11214.0),
        ],
        "Podman-Hybrid": [
            (0.0, 524.0),
            (1024.0, 6063.0),
            (2048.0, 10457.0),
        ],
	},
	"mem_only": {
		# Example: fill with (VmRSS_MB, time_ms) pairs when available
		"Podman-Hybrid": [
            (0.0, 524.0),
            (1024.0, 2897.0),
            (2048.0, 5239.0),
        ],
        "CRIU": [
            (0.0, 54.0),
            (1024.0, 718.0),
            (2048.0, 1371.0),
        ]
	},
}


def _parse_rows(csv_path: str) -> List[dict]:
	rows: List[dict] = []
	with open(csv_path, "r", newline="") as f:
		reader = csv.DictReader(f)
		for r in reader:
			try:
				# Normalize and convert numeric fields
				r["mem_mb"] = int(r.get("mem_mb", 0) or 0)
				r["fs_init_mb"] = int(r.get("fs_init_mb", 0) or 0)
				r["fs_delta_mb"] = int(r.get("fs_delta_mb", 0) or 0)
				r["elapsed_ms"] = float(r.get("elapsed_ms", 0.0) or 0.0)
				r["operation"] = (r.get("operation", "") or "").strip().upper()
			except Exception:
				# Skip malformed rows
				continue
			rows.append(r)
	return rows


def _aggregate_mem_only(rows: List[dict]) -> Tuple[List[float], Dict[float, Dict[str, float]]]:
	# mem-only: fs_init_mb == 0, fs_delta_mb == 0; x-axis uses average VmRSS (kB -> MB)
	vmrss_kb_by_mem: Dict[int, List[float]] = defaultdict(list)
	samples_by_mem: Dict[int, List[float]] = defaultdict(list)

	for r in rows:
		mem_mb = r["mem_mb"]
		fs_init_mb = r["fs_init_mb"]
		fs_delta_mb = r["fs_delta_mb"]
		op = r["operation"]
		val = r["elapsed_ms"]

		if fs_init_mb == 0 and fs_delta_mb == 0:
			if op == "INFO.VMRSS":
				# elapsed_ms stores VmRSS_kB for INFO.vmrss entries
				vmrss_kb_by_mem[mem_mb].append(val)
			elif op == "SNAPSHOT":
				samples_by_mem[mem_mb].append(val)

	# Build stats per mem level
	x_vals: List[float] = []
	stats_by_x: Dict[float, Dict[str, float]] = {}

	for mem_mb, ys in samples_by_mem.items():
		if not ys:
			continue
		vmrss_list = vmrss_kb_by_mem.get(mem_mb, [])
		if vmrss_list:
			x_mb = float(np.mean(vmrss_list)) / 1024.0
		else:
			x_mb = float(mem_mb)

		y = np.asarray(ys, dtype=float)
		q1, q3 = np.quantile(y, [0.25, 0.75])
		stats = {
			"count": float(y.size),
			"mean": float(np.mean(y)),
			"median": float(np.median(y)),
			"min": float(np.min(y)),
			"q1": float(q1),
			"q3": float(q3),
			"max": float(np.max(y)),
		}
		x_vals.append(x_mb)
		stats_by_x[x_mb] = stats

	# Sort x values ascending
	x_vals.sort()
	return x_vals, stats_by_x


def _aggregate_fsinit_only(rows: List[dict]) -> Tuple[List[float], Dict[float, Dict[str, float]]]:
	# fsInit-only: mem_mb == 0, fs_delta_mb == 0; x-axis uses fs_init_mb directly (MB)
	samples_by_fsinit: Dict[int, List[float]] = defaultdict(list)

	for r in rows:
		mem_mb = r["mem_mb"]
		fs_init_mb = r["fs_init_mb"]
		fs_delta_mb = r["fs_delta_mb"]
		op = r["operation"]
		val = r["elapsed_ms"]

		if mem_mb == 0 and fs_delta_mb == 0 and op == "SNAPSHOT":
			samples_by_fsinit[fs_init_mb].append(val)

	x_vals: List[float] = []
	stats_by_x: Dict[float, Dict[str, float]] = {}

	for fs_init_mb, ys in samples_by_fsinit.items():
		if not ys:
			continue
		y = np.asarray(ys, dtype=float)
		q1, q3 = np.quantile(y, [0.25, 0.75])
		stats = {
			"count": float(y.size),
			"mean": float(np.mean(y)),
			"median": float(np.median(y)),
			"min": float(np.min(y)),
			"q1": float(q1),
			"q3": float(q3),
			"max": float(np.max(y)),
		}
		x_vals.append(float(fs_init_mb))
		stats_by_x[float(fs_init_mb)] = stats

	x_vals.sort()
	return x_vals, stats_by_x


def _plot_distribution_with_mean(
	ax: plt.Axes,
	xs: List[float],
	stats_by_x: Dict[float, Dict[str, float]],
	color: str,
	label: str,
	x_label: str,
):
	medians = [stats_by_x[x]["median"] for x in xs]
	q1s = [stats_by_x[x]["q1"] for x in xs]
	q3s = [stats_by_x[x]["q3"] for x in xs]
	mins = [stats_by_x[x]["min"] for x in xs]
	maxs = [stats_by_x[x]["max"] for x in xs]

	# Draw whiskers (min-max) with low alpha
	for x, y_min, y_max in zip(xs, mins, maxs):
		ax.vlines(x, y_min, y_max, colors=color, alpha=0.3, linewidth=1)

	# Draw interquartile range (Q1-Q3) as thicker, semi-transparent line
	for x, y_q1, y_q3 in zip(xs, q1s, q3s):
		ax.vlines(x, y_q1, y_q3, colors=color, alpha=0.45, linewidth=6)

	# Median points and trend line
	ax.plot(xs, medians, color=color, linewidth=1.0)
	ax.scatter(xs, medians, color=color, s=28, label=label, zorder=3)

	ax.set_xlabel(x_label)
	ax.set_ylabel("Snapshot time (ms)")
	ax.grid(True, axis="y", linestyle=":", alpha=0.45)


def _plot_reference_lines(
	ax: plt.Axes,
	series: Dict[str, List[Tuple[float, float]]],
	base_color_cycle: List[str] = None,
	linestyle: str = "--",
	alpha: float = 0.7,
):
	if not series:
		return
	if base_color_cycle is None:
		base_color_cycle = [
			"tab:orange",
			"tab:red",
			"tab:purple",
			"tab:brown",
			"tab:gray",
			"black",
		]
	colors = iter(base_color_cycle)
	for name, pts in series.items():
		if not pts:
			continue
		# Sort by x to ensure a nice line
		xs, ys = zip(*sorted(pts, key=lambda t: t[0]))
		try:
			color = next(colors)
		except StopIteration:
			color = None
		ax.plot(xs, ys, linestyle=linestyle, linewidth=1.5, alpha=alpha, color=color, label=name)


def main():
	parser = argparse.ArgumentParser(description="Visualize microbenchmark results (mem-only and fsInit-only)")
	default_csv = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "micro.csv"))
	parser.add_argument("--csv", dest="csv_path", default=default_csv, help="Path to micro.csv (default: ../micro.csv)")
	parser.add_argument("--show", dest="show", action="store_true", help="Show interactive windows in addition to saving PNGs")
	parser.add_argument("--outdir", dest="outdir", default=None, help="Output directory for figures (default: alongside CSV)")
	args = parser.parse_args()

	rows = _parse_rows(args.csv_path)
	if not rows:
		raise SystemExit(f"No rows parsed from {args.csv_path}")

	# Aggregate datasets
	mem_xs, mem_stats = _aggregate_mem_only(rows)
	fs_xs, fs_stats = _aggregate_fsinit_only(rows)

	if not mem_xs:
		print("Warning: no mem-only dataset found (fs_init_mb=0, fs_delta_mb=0)")
	if not fs_xs:
		print("Warning: no fsInit-only dataset found (mem_mb=0, fs_delta_mb=0, fs_init_mb>0)")

	# Prepare output directory
	if args.outdir is None:
		outdir = os.path.dirname(os.path.abspath(args.csv_path))
	else:
		outdir = os.path.abspath(args.outdir)
	os.makedirs(outdir, exist_ok=True)

	# Figure 1: mem-only (x = average VmRSS MB)
	if mem_xs:
		fig1, ax1 = plt.subplots(figsize=(9, 5.2))
		_plot_distribution_with_mean(
			ax1,
			mem_xs,
			mem_stats,
			color="tab:blue",
			label="mem-only",
			x_label="VmRSS (MB, averaged across repeats)",
		)
		# Overlay reference lines for mem-only (if any)
		_plot_reference_lines(ax1, REF_DATA.get("mem_only", {}))
		ax1.set_title("Snapshot time vs memory (mem-only)")
		ax1.legend(loc="best")
		mem_out = os.path.join(outdir, "micro_mem_only.png")
		fig1.tight_layout()
		fig1.savefig(mem_out, dpi=160)
		print(f"Saved: {mem_out}")

	# Figure 2: fsInit-only (x = fs_init_mb)
	if fs_xs:
		fig2, ax2 = plt.subplots(figsize=(9, 5.2))
		_plot_distribution_with_mean(
			ax2,
			fs_xs,
			fs_stats,
			color="tab:green",
			label="fsInit-only",
			x_label="fs_init_mb (MB)",
		)
		# Overlay reference lines for fsinit-only (if any)
		_plot_reference_lines(ax2, REF_DATA.get("fsinit_only", {}))
		ax2.set_title("Snapshot time vs initial filesystem size (fsInit-only)")
		ax2.legend(loc="best")
		fs_out = os.path.join(outdir, "micro_fsinit_only.png")
		fig2.tight_layout()
		fig2.savefig(fs_out, dpi=160)
		print(f"Saved: {fs_out}")

	if args.show:
		plt.show()


if __name__ == "__main__":
	main()

