#!/usr/bin/env python3

import argparse
import csv
import os
from collections import defaultdict
from typing import Dict, List, Tuple

import numpy as np
import matplotlib.pyplot as plt

plt.rcParams.update({
	"font.size": 11,
	"axes.titlesize": 13,
	"axes.labelsize": 12,
	"legend.fontsize": 9,
	"xtick.labelsize": 10,
	"ytick.labelsize": 10,
	"pdf.fonttype": 42,
	"ps.fonttype": 42,
})


# Built-in reference datasets for overlay (line-only, no distribution).
# Units: x in MB, y in milliseconds.
REF_DATA_RESTORE = {
	"fsinit_only": {
		"Docker": [
			(0.0, 360.0),
			(1024.0, 511.0),
			(2048.0, 524.0),
			(4096.0, 709.0),
		],
		"Podman": [
			(0.0, 782.0),
			(1024.0, 911.0),
			(2048.0, 976.0),
			(4096.0, 1221.0),
		],
		"Podman-Hybrid": [
			(0.0, 1133.0),
			(1024.0, 8672.0),
			(2048.0, 16191.0),
		],
		"gVisor": [
			(0.0, 628.0),
			(128.0, 734.0),
			(256.0, 765.0),
			(512.0, 800.0),
			(1024.0, 975.0),
			(2048.0, 1364.0),
			(4096.0, 2164.0),
		]
	},
	"mem_only": {
		"Podman-Hybrid": [
			(0.0, 1133.0),
			(1024.0, 7024.0),
			(2048.0, 12915.0),
		],
		"CRIU": [
			(0.0, 6.0),
			(1024.0, 42.0),
			(2048.0, 74.0),
			(4096.0, 112.0),
		],
		"gVisor": [
			(0.0, 628.0),
			(128.0, 728.0),
			(256.0, 796.0),
			(512.0, 856.0),
			(1024.0, 1260.0),
			(2048.0, 1186.0),
			(4096.0, 1779.0),
		],
		"Firecracker": [
			(0.0, 485.2),
			(128.0, 420.9),
			(256.0, 438.6),
			(512.0, 429.0),
			(1024.0, 460.8),
		]
	},
	"mem_fs": {
		"Podman-Hybrid": [
			(0.0, 1133.0),
			(256.0, 4745.0),
			(512.0, 7992.0),
			(1024.0, 14579.0),
		],
		"gVisor": [
			(0.0, 628.0),
			(128.0, 791.0),
			(256.0, 951.0),
			(512.0, 1364.0),
			(1024.0, 1920.0),
			(2048.0, 3018.0),
			(4096.0, 4952.0),
		]
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
				r["mem_mb"] = int(r.get("mem_mb", 0) or 0)
				r["fs_init_mb"] = int(r.get("fs_init_mb", 0) or 0)
				r["fs_delta_mb"] = int(r.get("fs_delta_mb", 0) or 0)
				r["pair_idx"] = int(r.get("pair_idx", 0) or 0)
				r["elapsed_ms"] = float(r.get("elapsed_ms", 0.0) or 0.0)
				r["operation"] = (r.get("operation", "") or "").strip().upper()
			except Exception:
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
				vmrss_kb_by_mem[mem_mb].append(val)
			elif op == "RESTORE":
				samples_by_mem[mem_mb].append(val)

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

		if mem_mb == 0 and fs_delta_mb == 0 and op == "RESTORE":
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
	"""Aggregate scenarios where mem_mb == fs_init_mb and fs_delta_mb == 0 using RESTORE times."""
	samples_by_size: Dict[int, List[float]] = defaultdict(list)

	for r in rows:
		mem_mb = r["mem_mb"]
		fs_init_mb = r["fs_init_mb"]
		fs_delta_mb = r["fs_delta_mb"]
		op = r["operation"]
		val = r["elapsed_ms"]

		if op == "RESTORE" and fs_delta_mb == 0 and mem_mb == fs_init_mb:
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
	linewidth = 2.4 if is_ours else 1.5
	scatter_size = 44 if is_ours else 30
	iqr_width = 8 if is_ours else 5
	whisker_width = 1.4 if is_ours else 1.0
	alpha = 0.95 if is_ours else 0.75

	for x, y_min, y_max in zip(xs, mins, maxs):
		ax.vlines(x, y_min, y_max, colors=color, alpha=0.32, linewidth=whisker_width, zorder=2 if is_ours else 1)

	for x, y_q1, y_q3 in zip(xs, q1s, q3s):
		ax.vlines(x, y_q1, y_q3, colors=color, alpha=0.5 if is_ours else 0.35, linewidth=iqr_width, zorder=3 if is_ours else 2)

	ax.plot(xs, medians, color=color, linewidth=linewidth, alpha=alpha, zorder=4 if is_ours else 3)
	ax.scatter(xs, medians, color=color, s=scatter_size, label=label, zorder=5 if is_ours else 4, edgecolor="white", linewidth=0.6)

	ax.set_xlabel(x_label)
	ax.set_ylabel("Restore latency (ms)")
	_style_axis(ax)


def _style_axis(ax: plt.Axes):
	ax.grid(True, axis="y", linestyle=":", linewidth=0.8, alpha=0.45)
	ax.grid(False, axis="x")
	for spine in ax.spines.values():
		spine.set_visible(True)
		spine.set_color("0.25")
		spine.set_linewidth(0.8)
	ax.tick_params(axis="both", which="major", length=4, width=0.8, color="0.25")
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
		xs, ys = zip(*sorted(pts, key=lambda t: t[0]))
		color = BRAND_COLORS.get(name)
		if color is None:
			try:
				color = next(colors)
			except StopIteration:
				color = None
		ax.plot(xs, ys, linestyle=linestyle, linewidth=1.25, alpha=alpha, color=color, label=name, zorder=1)


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
	parser = argparse.ArgumentParser(description="Visualize RESTORE latency (mem-only, fsInit-only, mem==fs)")
	default_csv = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "micro.csv"))
	parser.add_argument("--csv", dest="csv_paths", nargs="+", default=None, help="One or more CSV files")
	parser.add_argument("inputs", nargs="*", help="CSV file(s)")
	parser.add_argument("--labels", dest="labels", nargs="+", default=None, help="Optional labels for CSVs; defaults to filename stems")
	parser.add_argument("--outdir", dest="outdir", default=None, help="Output directory for figures (default: alongside first CSV)")
	parser.add_argument("--stats-only", dest="stats_only", action="store_true", help="Print mean/median for our data at major sizes and exit")
	parser.add_argument("--max-mb", dest="max_mb", type=float, default=None, help="Upper limit in MB for plotting (applies to major 3 charts)")
	parser.add_argument("--max-ms", dest="max_ms", type=float, default=None, help="Upper limit in milliseconds for Y-axis (applies to major 3 charts; data not dropped, just clipped)")
	parser.add_argument("--export-csv", dest="export_csv", nargs="?", const="", default=None, help="Export plot-ready CSV data; optionally provide output path (default: OUTDIR/restore_plot_data.csv)")
	args = parser.parse_args()

	csv_list = args.csv_paths or args.inputs
	if not csv_list:
		csv_list = [default_csv]
	csv_list = [os.path.abspath(p) for p in csv_list]

	if args.labels and len(args.labels) == len(csv_list):
		labels = list(args.labels)
	else:
		labels = [os.path.splitext(os.path.basename(p))[0] for p in csv_list]
	plot_labels = list(labels)
	if plot_labels:
		plot_labels[0] = f"{plot_labels[0]}" if args.labels else "Checkpoint-lite (ours)"

	# If only stats are requested, compute for the first dataset and exit
	if args.stats_only:
		def _print_stats_major(rows: List[dict], op: str):
			major_sizes = {0, 128, 256, 512, 1024, 2048, 4096}

			def summarize(values: List[float]):
				a = np.asarray(values, dtype=float)
				return float(np.median(a)), float(np.mean(a)), int(a.size)

			mem_groups: Dict[int, List[float]] = defaultdict(list)
			for r in rows:
				if r["operation"] == op and r["fs_init_mb"] == 0 and r["fs_delta_mb"] == 0:
					mem_groups[r["mem_mb"]].append(r["elapsed_ms"])

			fs_groups: Dict[int, List[float]] = defaultdict(list)
			for r in rows:
				if r["operation"] == op and r["mem_mb"] == 0 and r["fs_delta_mb"] == 0:
					fs_groups[r["fs_init_mb"]].append(r["elapsed_ms"])

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
		_print_stats_major(rows, op="RESTORE")
		return

	# Helper to cap x-range for major charts
	def _cap_major(xs: List[float], stats: Dict[float, Dict[str, float]], xmax: float | None):
		if xmax is None or not xs:
			return xs, stats
		fxs = [x for x in xs if x <= xmax]
		fstats = {x: stats[x] for x in fxs}
		return fxs, fstats

	# Axis limit helpers: crop visuals without dropping data
	def _apply_major_limits(ax: plt.Axes):
		if args.max_mb is not None:
			ax.set_xlim(right=args.max_mb)
		if args.max_ms is not None:
			ax.set_ylim(top=args.max_ms)
		# Ensure 0 is visible away from border
		_, y_top = ax.get_ylim()
		pad = max(1.0, 0.02 * y_top)
		ax.set_ylim(bottom=-pad)
		ax.set_xlim(left=-pad)

	if args.outdir is None:
		outdir = os.path.dirname(os.path.abspath(csv_list[0]))
	else:
		outdir = os.path.abspath(args.outdir)
	os.makedirs(outdir, exist_ok=True)

	if args.export_csv == "":
		export_csv = os.path.join(outdir, "restore_plot_data.csv")
	elif args.export_csv is None:
		export_csv = None
	else:
		export_csv = os.path.abspath(args.export_csv)

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

		# Do not drop data points; rely on axis limits to crop visuals

		if not mem_xs:
			print(f"[{ds_label}] no mem-only dataset (fs_init_mb=0, fs_delta_mb=0)")
		else:
			if ax1 is None:
				fig1, ax1 = plt.subplots(figsize=(7.2, 4.2))
				ax1.set_title("Restore latency as application memory grows")
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
			_append_distribution_rows(export_rows, "restore", "mem_only", ds_label, csv_path, mem_xs, mem_stats, mem_from_x=True, fs_from_x=False)
			_apply_major_limits(ax1)

		if not fs_xs:
			print(f"[{ds_label}] no fsInit-only dataset (mem_mb=0, fs_delta_mb=0, fs_init_mb>0)")
		else:
			if ax2 is None:
				fig2, ax2 = plt.subplots(figsize=(7.2, 4.2))
				ax2.set_title("Restore latency as initial filesystem size grows")
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
			_append_distribution_rows(export_rows, "restore", "fsinit_only", ds_label, csv_path, fs_xs, fs_stats, mem_from_x=False, fs_from_x=True)
			_apply_major_limits(ax2)

		if not memfs_xs:
			pass
		else:
			if ax4 is None:
				fig4, ax4 = plt.subplots(figsize=(7.2, 4.2))
				ax4.set_title("Restore latency when memory and filesystem grow together")
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
			_append_distribution_rows(export_rows, "restore", "mem_fs", ds_label, csv_path, memfs_xs, memfs_stats, mem_from_x=True, fs_from_x=True)
			_apply_major_limits(ax4)

	if ax1 is not None:
		_plot_reference_lines(ax1, REF_DATA_RESTORE.get("mem_only", {}))
		_append_reference_rows(export_rows, "restore", "mem_only", REF_DATA_RESTORE.get("mem_only", {}))
		ax1.legend(loc="best", frameon=False)
		mem_out = os.path.join(outdir, "restore_mem_only.png")
		fig1.tight_layout(); fig1.savefig(mem_out, dpi=300, bbox_inches="tight"); print(f"Saved: {mem_out}")

	if ax2 is not None:
		_plot_reference_lines(ax2, REF_DATA_RESTORE.get("fsinit_only", {}))
		_append_reference_rows(export_rows, "restore", "fsinit_only", REF_DATA_RESTORE.get("fsinit_only", {}))
		ax2.legend(loc="best", frameon=False)
		fs_out = os.path.join(outdir, "restore_fsinit_only.png")
		fig2.tight_layout(); fig2.savefig(fs_out, dpi=300, bbox_inches="tight"); print(f"Saved: {fs_out}")

	if ax4 is not None:
		_plot_reference_lines(ax4, REF_DATA_RESTORE.get("mem_fs", {}))
		_append_reference_rows(export_rows, "restore", "mem_fs", REF_DATA_RESTORE.get("mem_fs", {}))
		ax4.legend(loc="best", frameon=False)
		memfs_out = os.path.join(outdir, "restore_mem_fs.png")
		fig4.tight_layout(); fig4.savefig(memfs_out, dpi=300, bbox_inches="tight"); print(f"Saved: {memfs_out}")

	if export_csv is not None:
		_write_export_csv(export_csv, export_rows)
		print(f"Saved: {export_csv}")

	# No interactive show in CLI mode


if __name__ == "__main__":
	main()
