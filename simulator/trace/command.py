from enum import Enum


class CommandType(Enum):
    SNAPSHOT = "snapshot"
    RESTORE = "restore"


class Command:
    def __init__(self, cmd_type, src_id, dst_id):
        self.cmd_type = cmd_type
        self.src_id = src_id
        self.dst_id = dst_id

    def __repr__(self):
        return f"{self.cmd_type.value.upper()}({self.src_id} -> {self.dst_id})"