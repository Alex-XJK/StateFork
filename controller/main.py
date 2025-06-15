from container_manager import DockerEnvironmentManager

def main():
    manager = DockerEnvironmentManager()

    print("StateFork Container Manager")
    print("Commands: snapshot, list, restore <id>, step, stats, exit")

    while True:
        cmd = input("StateFork > ").strip()

        if cmd == "exit":
            print("Execution history:")
            manager.stats.print_history()
            print("Overall statistics:")
            manager.stats.print_stats()
            print("Cleaning up resources...")
            manager.cleanup()
            break

        elif cmd == "snapshot":
            sid = manager.snapshot()
            print(f"Snapshot created: {sid}")

        elif cmd == "list":
            print("Available snapshots:")
            for s in manager.list_snapshots():
                print(f" - {s}")

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

        elif cmd == "stats":
            manager.stats.print_stats()

        else:
            print("Unknown command. Available: snapshot, list, restore <id>, step, stats, exit")

if __name__ == "__main__":
    main()
