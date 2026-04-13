from enum import Enum


class CommandType(Enum):
    SNAPSHOT = "snapshot"
    RESTORE = "restore"


class Command:
    def __init__(self, cmd_type, src_id, dst_id):
        self.cmd_type = cmd_type
        self.src_id = src_id
        self.dst_id = dst_id

        self.execution_time = None
        self.vmrss_mb = None
        # Bytes of restore stats attributed to this snapshot (delta from cumulative CSV).
        self.restore_stats_size = None

    def __repr__(self):
        extra = ""
        if self.execution_time is not None:
            extra += f", t={self.execution_time:.5f}"
        if self.vmrss_mb is not None:
            extra += f", mem={self.vmrss_mb:.5f}MB"
        if self.restore_stats_size is not None:
            extra += f", restore_sz={self.restore_stats_size}"

        return f"{self.cmd_type.value.upper()}({self.src_id} -> {self.dst_id}{extra})"