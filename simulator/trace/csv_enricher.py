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
        self.node_sequence = []  # ordered list

        current_id = None
        start_time = None
        end_time = None
        vmrss = None

        def flush():
            if current_id and start_time and end_time:
                exec_time = (end_time - start_time).total_seconds()
                self.node_sequence.append({
                    "node_id": current_id,
                    "execution_time": exec_time,
                    "vmrss_mb": vmrss
                })

        with open(file_path, "r") as f:
            reader = csv.DictReader(f)

            for row in reader:
                cid = row.get("checkpoint_id")

                # detect new block
                if cid and cid != current_id:
                    flush()
                    current_id = cid
                    start_time = None
                    end_time = None
                    vmrss = None

                # parse times
                if row.get("command_execution_start_time"):
                    start_time = self.parse_time(row["command_execution_start_time"])

                if row.get("command_execution_end_time"):
                    end_time = self.parse_time(row["command_execution_end_time"])

                # parse memory
                if row.get("ckptlite_snapshot_vmrss_mb"):
                    vmrss = float(row["ckptlite_snapshot_vmrss_mb"])

            flush()

    def attach_to_trace(self):
        node_idx = 0
        nodes = self.node_sequence

        for cmd in self.trace_builder.commands:
            if cmd.cmd_type.value != "snapshot":
                continue

            if node_idx >= len(nodes):
                print("[CSVEnricher] CSV exhausted")
                break

            node = nodes[node_idx]

            # Match using SRC node (correct now)
            if cmd.src_id != node["node_id"]:
                raise ValueError(
                    f"[CSVEnricher] Mismatch:\n"
                    f"  Trace node: {cmd.src_id}\n"
                    f"  CSV node:   {node['node_id']}"
                )

            cmd.execution_time = node["execution_time"]
            cmd.vmrss_mb = node["vmrss_mb"]

            node_idx += 1