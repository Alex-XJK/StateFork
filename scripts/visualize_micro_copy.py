#!/usr/bin/env python3

import argparse
import csv
import math
import os
from collections import defaultdict
from typing import Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt

plt.rcParams.update({
	"font.size": 16,
	"axes.titlesize": 20,
	"axes.labelsize": 18,
	"legend.fontsize": 15,
	"xtick.labelsize": 15,
	"ytick.labelsize": 15,
	"pdf.fonttype": 42,
	"ps.fonttype": 42,
})


# Built-in reference datasets for overlay (line-only, no distribution).
# Units: x in MB, y in milliseconds.
REF_DATA = {
	"fsinit_only": {
		"Docker": [
			(0.0, 56.0),
			(1024.0, 4586.0),
			(2048.0, 6391.0),
			(4096.0, 12256.0),
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
		"gVisor": [
			(0.0, 330.0),
			(128.0, 341.0),
			(256.0, 375.0),
			(512.0, 377.0),
			(1024.0, 480.0),
			(2048.0, 0479.0),
			(4096.0, 1025.0),
		],
	},
	"mem_only": {
		# Example: fill with (VmRSS_MB, time_ms) pairs when available
		"Podman-Hybrid": [
            (0.0, 524.0),
            (1024.0, 2897.0),
            (2048.0, 5239.0),
			(4096.0, 8477.5),
        ],
        "CRIU": [
            (0.0, 54.0),
			(16.0, 92.807),
			(64.0, 121.398),
			(128.0, 162.1),
			(256.0, 238.4),
			(512.0, 399.2),
            (1024.0, 718.0),
            (2048.0, 1371.0),
			(4096.0, 2653.4),
        ],
		"gVisor": [
			(0.0, 330.0),
			(128.0, 361.0),
			(256.0, 377.0),
			(512.0, 425.0),
			(1024.0, 521.0),
			(2048.0, 721.0),
			(4096.0, 749.0),
		],
		"Firecracker": [
			(0.0, 1280.2),
			(128.0, 1319.4),
			(256.0, 1336.4),
			(512.0, 1358.2),
			(1024.0, 1391.4),
		],
	},
	"mem_fs": {
		"Podman-Hybrid": [
			(0.0, 524.0),
			(256.0, 3612.5),
			(512.0, 5286.4),
			(1024.0, 8844.0),
		],
		"gVisor": [
			(0.0, 330.0),
			(128.0, 349.0),
			(256.0, 404.0),
			(512.0, 509.0),
			(1024.0, 503.0),
			(2048.0, 856.0),
			(4096.0, 963.0),
		],
	},
}

# Brand color mapping for reference overlays
BRAND_COLORS: Dict[str, str] = {
	"Docker": "lightskyblue",      # light blue
	"Podman": "tab:purple",       # purple
	"CRIU": "tab:red",            # red
	"Podman-Hybrid": "tab:pink",  # pink
	"gVisor": "navy",             # dark blue
	"Firecracker": "tab:orange",  # orange
}

OURS_COLOR = "tab:green"

EXPORT_COLUMNS = [
	"benchmark",
	"chart",
	"source_type",
	"dataset",
	"tool",
	"input_csv",
	"mem_mb",
	"fs_mb",
	"count",
	"mean_ms",
	"min_ms",
	"q1_ms",
	"median_ms",
	"q3_ms",
	"max_ms",
]


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
				r["pair_idx"] = int(r.get("pair_idx", 0) or 0)
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


def _aggregate_mem_fs_equal(rows: List[dict]) -> Tuple[List[float], Dict[float, Dict[str, float]]]:
	"""Aggregate scenarios where mem_mb == fs_init_mb and fs_delta_mb == 0.

	Groups by size (MB), collecting SNAPSHOT times across repeats and pairs, then
	computes median/IQR/min/max.
	"""
	samples_by_size: Dict[int, List[float]] = defaultdict(list)

	for r in rows:
		mem_mb = r["mem_mb"]
		fs_init_mb = r["fs_init_mb"]
		fs_delta_mb = r["fs_delta_mb"]
		op = r["operation"]
		val = r["elapsed_ms"]

		if op == "SNAPSHOT" and fs_delta_mb == 0 and mem_mb == fs_init_mb:
			samples_by_size[mem_mb].append(val)

	xs: List[float] = []
	stats_by_x: Dict[float, Dict[str, float]] = {}

	for size_mb, ys in samples_by_size.items():
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
		xs.append(float(size_mb))
		stats_by_x[float(size_mb)] = stats

	xs.sort()
	return xs, stats_by_x


def _plot_distribution_with_median(
	ax: plt.Axes,
	xs: List[float],
	stats_by_x: Dict[float, Dict[str, float]],
	color: str,
	label: str,
	x_label: str,
	is_ours: bool = False,
):
	medians = [stats_by_x[x]["median"] for x in xs]
	q1s = [stats_by_x[x]["q1"] for x in xs]
	q3s = [stats_by_x[x]["q3"] for x in xs]
	mins = [stats_by_x[x]["min"] for x in xs]
	maxs = [stats_by_x[x]["max"] for x in xs]
	linewidth = 4.0 if is_ours else 2.8
	scatter_size = 68 if is_ours else 48
	iqr_width = 11 if is_ours else 7
	whisker_width = 2.2 if is_ours else 1.6
	alpha = 0.95 if is_ours else 0.75

	# Draw whiskers (min-max) with low alpha
	for x, y_min, y_max in zip(xs, mins, maxs):
		ax.vlines(x, y_min, y_max, colors=color, alpha=0.32, linewidth=whisker_width, zorder=2 if is_ours else 1)

	# Draw interquartile range (Q1-Q3) as thicker, semi-transparent line
	for x, y_q1, y_q3 in zip(xs, q1s, q3s):
		ax.vlines(x, y_q1, y_q3, colors=color, alpha=0.5 if is_ours else 0.35, linewidth=iqr_width, zorder=3 if is_ours else 2)

	# Median points and trend line
	ax.plot(xs, medians, color=color, linewidth=linewidth, alpha=alpha, zorder=4 if is_ours else 3)
	ax.scatter(xs, medians, color=color, s=scatter_size, label=label, zorder=5 if is_ours else 4, edgecolor="white", linewidth=0.6)

	ax.set_xlabel(x_label)
	ax.set_ylabel("Snapshot latency (ms)")
	_style_axis(ax)


def _style_axis(ax: plt.Axes):
	ax.grid(True, axis="y", linestyle=":", linewidth=1.25, alpha=0.5)
	ax.grid(False, axis="x")
	# Use symlog so 0-valued inputs remain visible while applying log-like scaling.
	ax.set_xscale("symlog", linthresh=1.0, base=10)
	ax.set_yscale("symlog", linthresh=1.0, base=10)
	for spine in ax.spines.values():
		spine.set_visible(True)
		spine.set_color("0.25")
		spine.set_linewidth(1.2)
	ax.tick_params(axis="both", which="major", length=6, width=1.2, color="0.25")
	ax.set_axisbelow(True)


def _plot_reference_lines(
	ax: plt.Axes,
	series: Dict[str, List[Tuple[float, float]]],
	base_color_cycle: List[str] = None,
	linestyle: str = "--",
	alpha: float = 0.58,
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
		color = BRAND_COLORS.get(name)
		if color is None:
			try:
				color = next(colors)
			except StopIteration:
				color = None
		ax.plot(xs, ys, linestyle=linestyle, linewidth=2.6, alpha=alpha, color=color, label=name, zorder=1)


def _append_distribution_rows(
	export_rows: List[Dict[str, object]],
	benchmark: str,
	chart: str,
	dataset: str,
	input_csv: str,
	xs: List[float],
	stats_by_x: Dict[float, Dict[str, float]],
	mem_from_x: bool,
	fs_from_x: bool,
):
	for x in xs:
		stats = stats_by_x[x]
		export_rows.append({
			"benchmark": benchmark,
			"chart": chart,
			"source_type": "measured",
			"dataset": dataset,
			"tool": "",
			"input_csv": input_csv,
			"mem_mb": x if mem_from_x else 0.0,
			"fs_mb": x if fs_from_x else 0.0,
			"count": int(stats["count"]),
			"mean_ms": stats["mean"],
			"min_ms": stats["min"],
			"q1_ms": stats["q1"],
			"median_ms": stats["median"],
			"q3_ms": stats["q3"],
			"max_ms": stats["max"],
		})


def _append_reference_rows(
	export_rows: List[Dict[str, object]],
	benchmark: str,
	chart: str,
	series: Dict[str, List[Tuple[float, float]]],
):
	for name, pts in series.items():
		for x, median in sorted(pts, key=lambda t: t[0]):
			export_rows.append({
				"benchmark": benchmark,
				"chart": chart,
				"source_type": "reference",
				"dataset": "",
				"tool": name,
				"input_csv": "",
				"mem_mb": x if chart in ("mem_only", "mem_fs") else 0.0,
				"fs_mb": x if chart in ("fsinit_only", "mem_fs") else 0.0,
				"count": "",
				"mean_ms": "",
				"min_ms": "",
				"q1_ms": "",
				"median_ms": median,
				"q3_ms": "",
				"max_ms": "",
			})


def _write_export_csv(path: str, rows: List[Dict[str, object]]):
	parent = os.path.dirname(os.path.abspath(path))
	if parent:
		os.makedirs(parent, exist_ok=True)
	with open(path, "w", newline="") as f:
		writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
		writer.writeheader()
		writer.writerows(rows)


def main():
	parser = argparse.ArgumentParser(description="Visualize microbenchmark results (mem-only, fsInit-only, mem==fs)")
	default_csv = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "micro.csv"))
	parser.add_argument("--csv", dest="csv_paths", nargs="+", default=None, help="One or more CSV files")
	parser.add_argument("inputs", nargs="*", help="CSV file(s)")
	parser.add_argument("--labels", dest="labels", nargs="+", default=None, help="Optional labels for CSVs; defaults to filename stems")
	parser.add_argument("--outdir", dest="outdir", default=None, help="Output directory for figures (default: alongside first CSV)")
	parser.add_argument("--stats-only", dest="stats_only", action="store_true", help="Print mean/median for our data at major sizes and exit")
	parser.add_argument("--max-mb", dest="max_mb", type=float, default=None, help="Upper limit in MB for plotting (applies to major 3 charts)")
	parser.add_argument("--max-ms", dest="max_ms", type=float, default=None, help="Upper limit in milliseconds for Y-axis (applies to major 3 charts; data not dropped, just clipped)")
	parser.add_argument("--export-csv", dest="export_csv", nargs="?", const="", default=None, help="Export plot-ready CSV data; optionally provide output path (default: OUTDIR/micro_plot_data.csv)")
	args = parser.parse_args()

	# Resolve CSV list
	csv_list = args.csv_paths or args.inputs
	if not csv_list:
		csv_list = [default_csv]
	csv_list = [os.path.abspath(p) for p in csv_list]

	# Resolve labels
	if args.labels and len(args.labels) == len(csv_list):
		labels = list(args.labels)
	else:
		labels = [os.path.splitext(os.path.basename(p))[0] for p in csv_list]
	plot_labels = list(labels)
	if plot_labels:
		plot_labels[0] = f"{plot_labels[0]}" if args.labels else "Checkpoint-lite (ours)"

	# Prepare output directory
	if args.outdir is None:
		outdir = os.path.dirname(os.path.abspath(csv_list[0]))
	else:
		outdir = os.path.abspath(args.outdir)
	os.makedirs(outdir, exist_ok=True)

	if args.export_csv == "":
		export_csv = os.path.join(outdir, "micro_plot_data.csv")
	elif args.export_csv is None:
		export_csv = None
	else:
		export_csv = os.path.abspath(args.export_csv)

	# If only stats are requested, compute for the first dataset and exit
	if args.stats_only:
		def _print_stats_major(rows: List[dict], op: str):
			# Only major sizes
			major_sizes = {0, 128, 256, 512, 1024, 2048, 4096}

			def summarize(values: List[float]) -> Tuple[float, float, int]:
				a = np.asarray(values, dtype=float)
				return float(np.median(a)), float(np.mean(a)), int(a.size)

			# mem-only by declared mem_mb
			mem_groups: Dict[int, List[float]] = defaultdict(list)
			for r in rows:
				if r["operation"] == op and r["fs_init_mb"] == 0 and r["fs_delta_mb"] == 0:
					mem_groups[r["mem_mb"]].append(r["elapsed_ms"])

			# fsInit-only by fs_init_mb
			fs_groups: Dict[int, List[float]] = defaultdict(list)
			for r in rows:
				if r["operation"] == op and r["mem_mb"] == 0 and r["fs_delta_mb"] == 0:
					fs_groups[r["fs_init_mb"]].append(r["elapsed_ms"])

			# mem==fs by size (mem_mb)
			memfs_groups: Dict[int, List[float]] = defaultdict(list)
			for r in rows:
				if r["operation"] == op and r["fs_delta_mb"] == 0 and r["mem_mb"] == r["fs_init_mb"]:
					memfs_groups[r["mem_mb"]].append(r["elapsed_ms"])

			print("== Stats (median / mean / count) ==\n")
			print("-- mem-only --")
			for x in sorted(major_sizes.intersection(mem_groups.keys())):
				med, mean, cnt = summarize(mem_groups[x])
				print(f"{x:>5} MB: median={med:.3f} ms, mean={mean:.3f} ms, n={cnt}")

			print("\n-- fsInit-only --")
			for x in sorted(major_sizes.intersection(fs_groups.keys())):
				med, mean, cnt = summarize(fs_groups[x])
				print(f"{x:>5} MB: median={med:.3f} ms, mean={mean:.3f} ms, n={cnt}")

			print("\n-- mem==fs --")
			for x in sorted(major_sizes.intersection(memfs_groups.keys())):
				med, mean, cnt = summarize(memfs_groups[x])
				print(f"{x:>5} MB: median={med:.3f} ms, mean={mean:.3f} ms, n={cnt}")

		first_csv = (args.csv_paths or args.inputs or [default_csv])[0]
		rows = _parse_rows(os.path.abspath(first_csv))
		_print_stats_major(rows, op="SNAPSHOT")
		return

	# Axis limit helpers: crop visuals without dropping data
	def _first_order_of_magnitude(v: float) -> float:
		if v <= 0:
			return 1.0
		return 10.0 ** math.floor(math.log10(v))

	def _positive_min_from_axis_data(ax: plt.Axes, axis: str) -> float | None:
		vals: List[float] = []
		idx = 0 if axis == "x" else 1

		# Line artists
		for line in ax.lines:
			try:
				data = line.get_xdata() if axis == "x" else line.get_ydata()
				arr = np.asarray(data, dtype=float).ravel()
				vals.extend(arr[arr > 0].tolist())
			except Exception:
				pass

		# Scatter points and line collections (e.g., vlines)
		for coll in ax.collections:
			try:
				if hasattr(coll, "get_offsets"):
					offsets = np.asarray(coll.get_offsets(), dtype=float)
					if offsets.size:
						arr = offsets[:, idx]
						vals.extend(arr[arr > 0].tolist())
			except Exception:
				pass
			try:
				if hasattr(coll, "get_segments"):
					segs = coll.get_segments()
					for seg in segs:
						seg_arr = np.asarray(seg, dtype=float)
						if seg_arr.size == 0:
							continue
						arr = seg_arr[:, idx]
						vals.extend(arr[arr > 0].tolist())
			except Exception:
				pass

		if not vals:
			return None
		return float(min(vals))

	def _apply_major_limits(ax: plt.Axes):
		if args.max_mb is not None:
			ax.set_xlim(right=args.max_mb)
		if args.max_ms is not None:
			ax.set_ylim(top=args.max_ms)
		# In log-style view, start from the first recorded order of magnitude.
		x_min_pos = _positive_min_from_axis_data(ax, "x")
		y_min_pos = _positive_min_from_axis_data(ax, "y")
		if x_min_pos is not None:
			ax.set_xlim(left=_first_order_of_magnitude(x_min_pos))
		if y_min_pos is not None:
			ax.set_ylim(bottom=_first_order_of_magnitude(y_min_pos))

	# Figures (create lazily on first dataset that has data)
	fig1 = ax1 = None  # mem-only
	fig2 = ax2 = None  # fsInit-only
	fig4 = ax4 = None  # mem==fs

	# Color cycles for datasets other than the first; first dataset is forced to green
	mem_colors = ["tab:blue", "tab:orange", "tab:red", "tab:purple", "tab:brown", "tab:pink", "tab:gray", "tab:olive", "tab:cyan"]
	fsinit_colors = ["tab:blue", "tab:orange", "tab:red", "tab:purple", "tab:pink", "tab:gray", "tab:olive", "tab:cyan", "tab:brown"]
	memfs_colors = ["tab:cyan", "tab:olive", "tab:gray", "tab:brown", "tab:pink", "tab:blue", "tab:red", "tab:purple"]

	def _dataset_color(idx: int, cycle: List[str]) -> str:
		return OURS_COLOR if idx == 0 else cycle[(idx - 1) % len(cycle)]
	export_rows: List[Dict[str, object]] = []

	for i, (csv_path, ds_label, plot_label) in enumerate(zip(csv_list, labels, plot_labels)):
		rows = _parse_rows(csv_path)
		if not rows:
			print(f"Warning: no rows parsed from {csv_path}")
			continue

		mem_xs, mem_stats = _aggregate_mem_only(rows)
		fs_xs, fs_stats = _aggregate_fsinit_only(rows)
		memfs_xs, memfs_stats = _aggregate_mem_fs_equal(rows)

		if not mem_xs:
			print(f"[{ds_label}] no mem-only dataset (fs_init_mb=0, fs_delta_mb=0)")
		else:
			if ax1 is None:
				fig1, ax1 = plt.subplots(figsize=(7.2, 4.2))
				ax1.set_title("Snapshot latency as application memory grows")
			color = _dataset_color(i, mem_colors)
			_plot_distribution_with_median(
				ax1,
				mem_xs,
				mem_stats,
				color=color,
				label=plot_label,
				x_label="Resident memory, VmRSS (MB)",
				is_ours=(i == 0),
			)
			_append_distribution_rows(export_rows, "snapshot", "mem_only", ds_label, csv_path, mem_xs, mem_stats, mem_from_x=True, fs_from_x=False)
			_apply_major_limits(ax1)

		if not fs_xs:
			print(f"[{ds_label}] no fsInit-only dataset (mem_mb=0, fs_delta_mb=0, fs_init_mb>0)")
		else:
			if ax2 is None:
				fig2, ax2 = plt.subplots(figsize=(7.2, 4.2))
				ax2.set_title("Snapshot latency as initial filesystem size grows")
			color = _dataset_color(i, fsinit_colors)
			_plot_distribution_with_median(
				ax2,
				fs_xs,
				fs_stats,
				color=color,
				label=plot_label,
				x_label="Initial filesystem size (MB)",
				is_ours=(i == 0),
			)
			_append_distribution_rows(export_rows, "snapshot", "fsinit_only", ds_label, csv_path, fs_xs, fs_stats, mem_from_x=False, fs_from_x=True)
			_apply_major_limits(ax2)

		if not memfs_xs:
			# Not printing to reduce noise; it's a niche view
			pass
		else:
			if ax4 is None:
				fig4, ax4 = plt.subplots(figsize=(7.2, 4.2))
				ax4.set_title("Snapshot latency when memory and filesystem grow together")
			color = _dataset_color(i, memfs_colors)
			_plot_distribution_with_median(
				ax4,
				memfs_xs,
				memfs_stats,
				color=color,
				label=plot_label,
				x_label="Memory and filesystem size (MB)",
				is_ours=(i == 0),
			)
			_append_distribution_rows(export_rows, "snapshot", "mem_fs", ds_label, csv_path, memfs_xs, memfs_stats, mem_from_x=True, fs_from_x=True)
			_apply_major_limits(ax4)

	# After plotting all datasets, overlay references where applicable
	if ax1 is not None:
		_plot_reference_lines(ax1, REF_DATA.get("mem_only", {}))
		_append_reference_rows(export_rows, "snapshot", "mem_only", REF_DATA.get("mem_only", {}))
		_apply_major_limits(ax1)
		ax1.legend(
			loc="upper left",
			frameon=True,
			framealpha=1.0,
			facecolor="white",
			edgecolor="#b8b8b8",
			fancybox=False,
			fontsize=13,
			labelspacing=0.14,
			handlelength=1.6,
			handletextpad=0.42,
			borderpad=0.24,
			borderaxespad=0.24,
		)
		mem_out = os.path.join(outdir, "micro_mem_only.png")
		fig1.tight_layout(); fig1.savefig(mem_out, dpi=300, bbox_inches="tight"); print(f"Saved: {mem_out}")

	if ax2 is not None:
		_plot_reference_lines(ax2, REF_DATA.get("fsinit_only", {}))
		_append_reference_rows(export_rows, "snapshot", "fsinit_only", REF_DATA.get("fsinit_only", {}))
		_apply_major_limits(ax2)
		ax2.legend(
			loc="upper left",
			frameon=True,
			framealpha=1.0,
			facecolor="white",
			edgecolor="#b8b8b8",
			fancybox=False,
			fontsize=13,
			labelspacing=0.14,
			handlelength=1.6,
			handletextpad=0.42,
			borderpad=0.24,
			borderaxespad=0.24,
		)
		fs_out = os.path.join(outdir, "micro_fsinit_only.png")
		fig2.tight_layout(); fig2.savefig(fs_out, dpi=300, bbox_inches="tight"); print(f"Saved: {fs_out}")

	if ax4 is not None:
		_plot_reference_lines(ax4, REF_DATA.get("mem_fs", {}))
		_append_reference_rows(export_rows, "snapshot", "mem_fs", REF_DATA.get("mem_fs", {}))
		_apply_major_limits(ax4)
		ax4.legend(
			loc="upper left",
			frameon=True,
			framealpha=1.0,
			facecolor="white",
			edgecolor="#b8b8b8",
			fancybox=False,
			fontsize=13,
			labelspacing=0.14,
			handlelength=1.6,
			handletextpad=0.42,
			borderpad=0.24,
			borderaxespad=0.24,
		)
		memfs_out = os.path.join(outdir, "micro_mem_fs.png")
		fig4.tight_layout(); fig4.savefig(memfs_out, dpi=300, bbox_inches="tight"); print(f"Saved: {memfs_out}")

	if export_csv is not None:
		_write_export_csv(export_csv, export_rows)
		print(f"Saved: {export_csv}")


if __name__ == "__main__":
	main()
