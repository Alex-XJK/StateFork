import csv
from datetime import datetime


class CSVEnricher:
    def __init__(self, trace_builder):
        self.trace_builder = trace_builder

        # Map node_id -> metadata
        self.node_info = {}

    def parse_time(self, ts):
        if not ts:
            return None
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)

    def parse_csv(self, file_path):

        current_id = None
        start_time = None
        end_time = None
        vmrss = None

        def flush():
            nonlocal current_id, start_time, end_time, vmrss

            if current_id is None:
                return

            execution_time = None
            if start_time and end_time:
                execution_time = (end_time - start_time).total_seconds()

            # Only store if meaningful (skip restore-only)
            if execution_time is not None or vmrss is not None:
                self.node_info.setdefault(current_id, {
                    "execution_time": execution_time,
                    "vmrss_mb": vmrss,
                })

            # reset
            current_id = None
            start_time = None
            end_time = None
            vmrss = None

        with open(file_path, newline="") as f:
            reader = csv.DictReader(f)

            for row in reader:
                cid = row.get("checkpoint_id")

                # Detect checkpoint change
                if cid and cid != current_id:
                    flush()
                    current_id = cid

                # Parse fields
                if row.get("command_execution_start_time"):
                    start_time = self.parse_time(row["command_execution_start_time"])

                if row.get("command_execution_end_time"):
                    end_time = self.parse_time(row["command_execution_end_time"])

                if row.get("ckptlite_snapshot_vmrss_mb"):
                    vmrss = float(row["ckptlite_snapshot_vmrss_mb"])

            # flush last
            flush()

    def attach_to_trace(self):
        node_items = list(self.node_info.items())
        node_idx = 0

        for cmd_idx, cmd in enumerate(self.trace_builder.commands):

            # Only SNAPSHOT commands correspond to CSV entries
            if cmd.cmd_type.value != "snapshot":
                node_idx += 1
                continue

            # Stop if CSV runs out
            if node_idx >= len(node_items):
                break

            expected_node_id, info = node_items[node_idx]

            # strictly match src_id instead of dst_id
            if cmd.src_id != expected_node_id:
                raise ValueError(
                    f"[CSVEnricher] Mismatch at command index {cmd_idx}:\n"
                    f"  Trace node (src): {cmd.src_id}\n"
                    f"  CSV node:         {expected_node_id}"
                )

            # Attach data to THIS command
            cmd.execution_time = info["execution_time"]
            cmd.vmrss_mb = info["vmrss_mb"]

            node_idx += 1