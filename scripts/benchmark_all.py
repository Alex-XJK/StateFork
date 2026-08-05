import argparse
import requests
import time
from enum import Enum

from controller import create_env_manager, EnvironmentManager

URL = "http://127.0.0.1:8000/all"
REPEATS = 5
ROUND = 2

class OpType(Enum):
    SNAPSHOT = "blue"
    RESTORE = "green"
    NETWORK = "pink"
    INTERNAL = "purple"
    WAIT = "orange"
    FAILURE = "gray"


class TimelineManager:
    def __init__(self, filename: str = "timeline_data.txt"):
        self._filename = filename
        self._write_cache = []
        self._write(f"# Timeline Data for RunID {int(time.time())}")
        self._write(f"# Created at UTC {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
        self._write("# Duration unit is milliseconds(ms)")

    def _write(self, content: str):
        with open(self._filename, "a") as f:
            f.write(content + "\n")

    def _cache(self, content: str):
        self._write_cache.append(content)

    def flush(self):
        if self._write_cache:
            with open(self._filename, "a") as f:
                f.write("\n".join(self._write_cache) + "\n")
            self._write_cache = []

    def log_section(self, section_name: str, compressed: bool = False):
        if compressed:
            section_name = f"*{section_name}"
        self._cache(f"[{section_name}]")
        print(f"Section: {section_name}")

    def log_step(self, step_name: str, sec: float, optype: OpType):
        self._cache(f"{step_name}, {sec * 1000:.3f}, {optype.value}")

    def log_color(self, color_id: str, color_name: str):
        self._cache(f">{color_id}: {color_name}")

    def log_default_color(self):
        for ot in OpType:
            self.log_color(ot.value, ot.name)


# Global instance of TimelineManager
tm = TimelineManager()
tm.log_default_color()
tm.flush()

def request_url(url: str) -> bool:
    """
    Send a GET request to the specified URL and print the response status.
    """
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return True
        else:
            return False
    except requests.RequestException as e:
        # print(f"Request failed for {url}: {e}")
        return False


def wait_for_ready(interval: float = 0.002, timeout: float = 5.0) -> float:
    """
    Wait for the server to be ready by repeatedly sending requests until a successful response is received or timeout occurs.
    Logging the time taken for each attempt during the wait.
    Return the time taken for the first successful request (not yet logged).
    """
    counter = 0
    start = time.time()
    while time.time() - start < timeout:
        counter += 1

        if request_url(URL):
            return time.time() - start

        step_time = time.time() - start
        tm.log_step(f"Try {counter}", step_time, OpType.NETWORK)

        time.sleep(interval)
        tm.log_step("Wait", interval, OpType.WAIT)
        start = time.time()
    raise TimeoutError(f"Timed out after {timeout} seconds waiting for the server to be ready.")


def test_time(env: EnvironmentManager) -> None:
    for i in range(REPEATS):
        tm.log_section(f"Init Requests {i+1}")
        start_time = time.time()
        res = request_url(URL)
        duration = time.time() - start_time
        if not res:
            tm.log_step("Failed", duration, OpType.FAILURE)
        else:
            tm.log_step("Req", duration, OpType.NETWORK)

    for r in range(ROUND):
        time.sleep(5)  # Allow socket to close gracefully

        tm.log_section(f"Env Step {r+1}")

        start_snap = time.time()
        sid = env.snapshot()
        tm.log_step("Snapshot", time.time() - start_snap, OpType.SNAPSHOT)

        start_restore = time.time()
        _ = env.restore(sid)
        tm.log_step("Restore", time.time() - start_restore, OpType.RESTORE)

        tm.log_section(f"Wait for Recovery {r+1}", compressed=True)
        first_success = wait_for_ready()

        tm.log_section(f"After Recovery {r+1}")
        tm.log_step(" ", first_success, OpType.NETWORK)

        for k in range(1, REPEATS):
            tm.log_section(f"Following Requests {r+1}.{k+1}")
            start_time = time.time()
            res = request_url(URL)
            duration = time.time() - start_time
            if not res:
                tm.log_step(f"Failed", duration, OpType.FAILURE)
            else:
                tm.log_step(f"Req", duration, OpType.NETWORK)

    env.cleanup()
    tm.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detailed Time Benchmark")
    parser.add_argument("--method", choices=["docker", "criu", "podman", "hybrid"], help="Choose the environment manager backend")
    args = parser.parse_args()

    manager = None

    if args.method == "docker":
        manager = create_env_manager("docker_build")
    elif args.method == "criu":
        manager = create_env_manager("criu_build")
    elif args.method == "podman":
        manager = create_env_manager("podman_build")
    elif args.method == "hybrid":
        manager = create_env_manager("hybrid_build")
    else:
        raise ValueError(f"Unsupported command method: {args.method}")

    print(f"Using {manager.backend} backend for environment management.")
    time.sleep(5)  # Allow time for the environment to initialize
    test_time(manager)
