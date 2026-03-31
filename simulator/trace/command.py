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

    def __repr__(self):
        extra = ""
        if self.execution_time is not None:
            extra += f", t={self.execution_time:.2f}"
        if self.vmrss_mb is not None:
            extra += f", mem={self.vmrss_mb}MB"

        return f"{self.cmd_type.value.upper()}({self.src_id} -> {self.dst_id}{extra})"