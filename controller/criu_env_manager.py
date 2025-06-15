import subprocess
import time
import uuid
import logging
from typing import Optional
from base_env_manager import EnvironmentManager, SnapshotNode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CRIUEnvironmentManager(EnvironmentManager):
    def __init__(self, entry_command=None):
        super().__init__()
        if entry_command is None:
            entry_command = ["uvicorn", "app.api_server:app", "--host", "0.0.0.0", "--port", "8000"]


    def _core_snapshot(self) -> tuple[Optional[str], float]:
        pass

    def _core_create_env(self, snapshot_id: str) -> tuple[Optional[str], float]:
        pass

    def _core_restore(self, snapshot_id: str) -> tuple[bool, float]:
        pass

    def _core_cleanup(self):
        pass
