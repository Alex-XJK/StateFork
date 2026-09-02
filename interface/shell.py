import argparse
import logging
import sys

from controller import ForkableEnvironmentManager, create_env_manager
from decider import RandomDecider, AlwaysTrueDecider, AlwaysFalseDecider, ThresholdDecider


AVAILABLE_COMMANDS = [
    "snapshot [fork] [--park]",
    "restore <id>",
    "cmd <command>",
    "fork <id> [n]",
    "forks",
    "fexec <fork> <cmd>",
    "destroy <fork>",
    "tree",
    "stats",
    "history",
    "storage",
    "exit",
    "set",
]


# -------- Backend Mapping --------
BACKEND_MAP = {
    "docker": "docker_build",
    "podman": "podman_build",
    "criu": "criu_build",
    "hybrid": "hybrid_build",
    "waypoint": "waypoint_build",
    "ckpt": "waypoint_build",  # legacy alias
    "gvisor": "gvisor_build",
    "firecracker": "firecracker_build"
}


# -------- Decider Mapping --------
DECIDER_MAP = {
    "random": RandomDecider,
    "always_true": AlwaysTrueDecider,
    "always_false": AlwaysFalseDecider,
    "threshold": ThresholdDecider,
}


def build_manager(method: str, decider_name: str, threshold: float):
    method_key = BACKEND_MAP[method]

    if decider_name == "threshold":
        decider_instance = ThresholdDecider(threshold)
    else:
        decider_cls = DECIDER_MAP[decider_name]
        decider_instance = decider_cls()

    return create_env_manager(
        method_key,
        decider=decider_instance,
    )

def print_welcome_message(manager):
    print("==========================================")
    print("StateFork Container Manager - Interactive Shell")
    print(f"Using {manager.__class__.__name__} with {manager.backend} backend")
    print("")
    print(f"Available commands: {', '.join(AVAILABLE_COMMANDS)}")

def has_fork_api(manager) -> bool:
    if isinstance(manager, ForkableEnvironmentManager):
        return True
    print(f"This command requires a forkable backend (current: {manager.backend}).")
    return False


def execute_command(manager, command_text):
    rc, out, err = manager.exec_command(command_text)

    if out.strip():
        print("--- stdout ---")
        print(out.strip())
    if err.strip():
        print("--- stderr ---")
        print(err.strip())

def interactive_shell(manager):
    print_welcome_message(manager)

    need_cmd_heading = True

    while True:
        try:
            cmd = input("\nStateFork > ").strip()
        except EOFError:
            # Piped/scripted input ended: treat as a clean `exit`.
            print()
            cmd = "exit"

        if cmd == "snapshot" or cmd.startswith("snapshot "):
            tokens = cmd.split()[1:]
            park = "--park" in tokens
            rest = [t for t in tokens if t != "--park"]
            if len(rest) > 1:
                print("Usage: snapshot [fork_id] [--park]")
                continue
            fid = rest[0] if rest else None

            if fid is not None or park:
                # Named snapshots and parking require the forkable capability.
                if not has_fork_api(manager):
                    continue
                # Parking the current fork moves the pointer, so name the
                # target before the call.
                target = fid or manager.current_branch_id
                sid = manager.snapshot_branch(target, park=park)
            else:
                sid = manager.snapshot()

            if not sid:
                print("Park failed." if park else "Snapshot failed.")
                continue
            if park:
                print(f"Fork {target} parked as snapshot {sid}")
                if fid is None:
                    print(f"Current branch: {manager.current_branch_id}")
            else:
                print(f"Snapshot created: {sid}")

        elif cmd.startswith("restore"):
            _, _, sid = cmd.partition(" ")
            if not sid:
                print("Usage: restore <snapshot_id>")
                continue

            ok = manager.restore(sid)
            print(f"Restored to snapshot {sid}" if ok else f"Restore failed for {sid}.")
            if ok and isinstance(manager, ForkableEnvironmentManager):
                print(f"Current branch: {manager.current_branch_id}")

        elif cmd.startswith("cmd"):
            _, _, command_text = cmd.partition(" ")
            if not command_text:
                print("Usage: cmd <command>")
                continue
            execute_command(manager, command_text)

        elif cmd == "forks":
            if not has_fork_api(manager):
                continue
            forks = manager.list_branches()
            if not forks:
                print("No live forks.")
            for f in forks:
                marker = " (current)" if f.id == manager.current_branch_id else ""
                print(
                    f"- {f.id}{marker}: base={f.base_snapshot_id} "
                    f"status={f.status}"
                )

        elif cmd.startswith("fork"):
            if not has_fork_api(manager):
                continue
            parts = cmd.split()
            if len(parts) < 2:
                print("Usage: fork <snapshot_id> [n]")
                continue
            try:
                n = int(parts[2]) if len(parts) > 2 else 1
            except ValueError:
                print("Usage: fork <snapshot_id> [n]")
                continue
            forks = manager.fork(parts[1], n=n)
            for f in forks:
                print(f"Forked {f.id} from {f.base_snapshot_id}")
            if not forks:
                print("Fork failed.")

        elif cmd.startswith("fexec"):
            if not has_fork_api(manager):
                continue
            _, _, rest = cmd.partition(" ")
            fork_id, _, command_text = rest.strip().partition(" ")
            if not fork_id or not command_text:
                print("Usage: fexec <fork_id> <command>")
                continue
            rc, out, err = manager.exec_on_branch(fork_id, command_text)
            if out.strip():
                print("--- stdout ---")
                print(out.strip())
            if err.strip():
                print("--- stderr ---")
                print(err.strip())
            if rc != 0:
                print(f"(exit code {rc})")

        elif cmd.startswith("destroy"):
            if not has_fork_api(manager):
                continue
            _, _, fork_id = cmd.partition(" ")
            fork_id = fork_id.strip()
            if not fork_id:
                print("Usage: destroy <fork_id>")
                continue
            ok = manager.discard_branch(fork_id)
            print(f"Fork {fork_id} destroyed." if ok else f"Failed to destroy fork {fork_id}.")

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

        elif cmd.startswith("set"):
            _, _, config_string = cmd.partition(" ")
            if not config_string:
                print("Usage: set <config>")
                continue
            elif config_string == "cmd off":
                need_cmd_heading = False
                print("Command input heading turned OFF.")
            elif config_string == "cmd on":
                need_cmd_heading = True
                print("Command input heading turned ON.")
            else:
                print(f"Unknown config: {config_string}")

        else:
            if need_cmd_heading:
                print(f"Unknown command: {cmd}")
                print(f"Available commands: {', '.join(AVAILABLE_COMMANDS)}")
                continue
            # If heading is turned off, treat unknown commands as direct commands to execute
            execute_command(manager, cmd)


def main():
    parser = argparse.ArgumentParser(description="Environment Manager Launcher")

    parser.add_argument(
        "--method",
        choices=BACKEND_MAP.keys(),
        default="docker",
        help="Choose the environment manager backend"
    )

    parser.add_argument(
        "--decider",
        choices=DECIDER_MAP.keys(),
        default="always_true",
        help="Choose snapshot decision strategy"
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=5,
        help="Threshold (seconds) for threshold decider"
    )

    args = parser.parse_args()

    # Process exit codes: 0 = clean shutdown (exit command or stdin EOF),
    # 1 = unexpected crash, 2 = startup failure (argparse uses 2 as well).
    try:
        manager = build_manager(args.method, args.decider, args.threshold)
    except Exception as e:
        print(f"Failed to start environment manager: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        interactive_shell(manager)
    except KeyboardInterrupt:
        print("\nInterrupted; cleaning up...")
        try:
            manager.cleanup()
        except Exception:
            logging.exception("Cleanup after interrupt failed")
        sys.exit(1)
    except Exception:
        logging.exception("Shell crashed")
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
