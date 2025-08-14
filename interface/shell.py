import argparse
import logging

from controller import create_env_manager

def main(args):
    available_commands = ["snapshot", "restore <id>", "step", "tree", "stats", "history", "storage", "exit"]

    if args.method == "docker":
        manager = create_env_manager("docker_build")
    elif args.method == "podman":
        manager = create_env_manager("podman_build")
    elif args.method == "criu":
        manager = create_env_manager("criu_build")
    elif args.method == "hybrid":
        manager = create_env_manager("hybrid_build")
    elif args.method == "ckpt":
        manager = create_env_manager("ckpt_build")
    else:
        raise ValueError(f"Unsupported command method: {args.method}")

    print("==========================================")
    print("StateFork Container Manager - Interactive Shell")
    print(f"Using {manager.__class__.__name__} with {manager.backend} backend")
    print("")
    print(f"Available commands: {', '.join(available_commands)}")

    while True:
        cmd = input("\nStateFork > ").strip()

        if cmd == "snapshot":
            sid = manager.snapshot()
            print(f"Snapshot created: {sid}")

        elif cmd.startswith("restore"):
            _, _, sid = cmd.partition(" ")
            if not sid:
                print("Usage: restore <snapshot_id>")
                continue
            ok = manager.restore(sid)
            if ok:
                print(f"Restored to snapshot {sid}")
            else:
                print(f"Snapshot {sid} not found.")

        elif cmd == "step":
            sid = manager.snapshot()
            container = manager.create_env_from_snapshot(sid)
            if container is None:
                print("Failed to create new container from snapshot.")
            else:
                print(f"Stepped to new container with snapshot {sid}")

        elif cmd == "tree":
            print(manager.print_snapshot_tree())

        elif cmd == "stats":
            print(manager.stats.print_stats())

        elif cmd == "history":
            print(manager.stats.print_history())

        elif cmd == "storage":
            print(manager.stats.print_size_details())

        elif cmd == "exit":
            print(manager.stats.print_stats())
            print("Cleaning up resources...")
            manager.cleanup()
            break

        else:
            print(f"Unknown command: {cmd}")
            print(f"Available commands: {', '.join(available_commands)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Environment Manager Launcher")
    parser.add_argument("--method", choices=["docker", "criu", "podman", "hybrid", "ckpt"], default="docker",
                        help="Choose the environment manager backend")
    args_ns = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    main(args_ns)
