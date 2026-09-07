import argparse
import time
import requests
from controller import create_env_manager, EnvironmentManager

URL = "http://127.0.0.1:8000/all"
REPEATS = 5

def wait_for_ready(interval: float = 0.005, timeout: float = 5.0) -> float:
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = requests.get(URL)
            if response.status_code == 200:
                return time.time() - start
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Timed out after {timeout} seconds waiting for the server to be ready.")

def test_time_b(env: EnvironmentManager) -> float:
    time_b_values = []

    print(f"Starting Time-B benchmark for {REPEATS} steps...")

    time.sleep(5) # Allow time for the server to start

    for i in range(REPEATS):
        print(f"[Round {i+1}] Sending init request...")
        response = requests.get(URL, headers={"Connection": "close"})
        assert response.status_code == 200
        # print(f"[Round {i+1}]     Received length {len(response.text)} bytes")
        print(f"[Round {i+1}] ... Init request successful")

        time.sleep(5)  # Allow time for the socket to close

        print(f"[Round {i+1}] Snapshot and stepping...")
        sid = env.snapshot()
        _ = env.restore(sid)
        print(f"[Round {i+1}] ... Stepping successful")

        print(f"[Round {i+1}] Waiting for recovery...")
        time_b = wait_for_ready()
        time_b_values.append(time_b)
        print(f"[Round {i+1}] ... Ready in {time_b:.6f} seconds\n")

    env.cleanup()

    avg_time_b = sum(time_b_values) / len(time_b_values)
    return avg_time_b

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Time-B Benchmark Script")
    parser.add_argument("--method", choices=["docker", "criu", "podman", "hybrid"],
                        help="Choose the environment manager backend")
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

    result = test_time_b(manager)
    print(f"Average Time-B is {result:.6f} seconds when using {manager.backend} backend.")
