from docker_env_manager import DockerContainerManager

def main():
    available_commands = ["snapshot", "restore <id>", "step", "tree", "stats", "history", "exit"]
    manager = DockerContainerManager()

    print("StateFork Container Manager")
    print(f"Available commands: {', '.join(available_commands)}")

    while True:
        cmd = input("StateFork > ").strip()

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
            manager.print_snapshot_tree()

        elif cmd == "stats":
            manager.stats.print_stats()

        elif cmd == "history":
            manager.stats.print_history()

        elif cmd == "exit":
            manager.stats.print_stats()
            print("Cleaning up resources...")
            manager.cleanup()
            break

        else:
            print(f"Unknown command: {cmd}")
            print(f"Available commands: {', '.join(available_commands)}")

if __name__ == "__main__":
    main()
