from typing import Dict, List


class SnapshotManager:
    def __init__(self):
        self._snapshots: Dict[str, Dict[str, str]] = {}
        self._counter = 0

    def create_snapshot(self, kv_store: Dict[str, str]) -> str:
        snapshot_id = f"snap-{self._counter}"
        self._snapshots[snapshot_id] = kv_store.copy()
        self._counter += 1
        return snapshot_id

    def restore_snapshot(self, snapshot_id: str) -> Dict[str, str] | None:
        return self._snapshots.get(snapshot_id)

    def list_snapshots(self) -> List[str]:
        return list(self._snapshots.keys())
