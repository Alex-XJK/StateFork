# 🌟 StateFork Controller 

## 🔧 Developer Guide

### Design Overview
1. Template Method Pattern
  - Base manager classes (e.g., `EnvironmentManager` and its subclasses)
  - The base class defines the overall workflow, while subclasses implement only the core logic steps. This ensures consistent process flow and reduces code duplication.
2. Factory Method Pattern
  - `create_env_manager` function in `__init__.py`
  - Centralizes the creation of different environment manager instances. Developers can register new manager types and instantiate them through a single interface, improving extensibility.
3. Strategy Pattern
  - **`Decider`** components choose physical vs virtual snapshots at each branch point
  - **`Calculator`** components (e.g., for benchmarking) attach to managers for storage/size measurement
  - Both are interchangeable strategies: managers keep the same workflow while policies or calculators vary.
4. Iterator Pattern
  - Benchmarking components (e.g., `BenchmarkStats`)
  - Used when iterating over a collection of attached `Calculators` to perform operations. This pattern provides a uniform way to access and process multiple strategies or components.

These patterns help keep the codebase modular, extensible, and easy to maintain for future development.


## 🧱 User Guide

### ⚙️ Env Manager
All controllers implement `EnvironmentManager`, which provides the common snapshot/restore capability. Backends with
real concurrent materialization additionally implement `ForkableEnvironmentManager`.

The usage of these controllers is well-documented in the `EnvironmentManager` base class, utilizing IDE-supported docstrings.
They are:
- `.snapshot()`: Create a snapshot of the current environment and return a unique identifier for the snapshot.
- `.restore(snapshot_id: str)`: Restore the environment to a specific snapshot and returns True if successful.
- `.cleanup()`: Clean up all containers and snapshots created by the controller instance.
- `.exec_command(command)`: Execute a command in the managed environment. Commands are logged for virtual snapshot replay, and their elapsed time accumulates for decider policies.

`create_env_from_snapshot()` was removed from the common contract. Creating a new process or container is a backend
implementation detail of `restore()`; creating an additional independently live environment is the separate `fork()`
capability.

### 🔀 Physical vs Virtual Snapshots

`EnvironmentManager.snapshot()` consults the attached **`Decider`** before calling backend-specific logic:

| Type | On `snapshot()` | On `restore()` |
|------|-----------------|----------------|
| **Physical** | Calls `_core_snapshot()`; stores a real backend checkpoint | Calls `_core_restore()` once |
| **Virtual** | Assigns a lightweight id; stores `replay_commands` copied from the command log since the parent | Restores the nearest **physical** ancestor, then replays commands from each virtual node on the path (parent → child) |

The snapshot tree (`snapshot_graph`) tracks parent/child links and whether each node is virtual. Virtual nodes appear in `print_snapshot_tree()` like any other branch point; only restore behavior differs.

**Decider types** (`decider/`): `AlwaysTrueDecider` (default, always physical), `AlwaysFalseDecider` (always virtual), `RandomDecider`, and `ThresholdDecider` (physical when cumulative exec time since the last physical snapshot exceeds a threshold). Sequential managers accept these strategies. The first `ForkableEnvironmentManager` implementation intentionally requires `AlwaysTrueDecider`; concurrent virtual-snapshot policy is deferred.

**Command tracking:** Between snapshots, `exec_command()` appends each command to `_command_log` and adds wall-clock time to `_cumulative_exec_time`. After a **physical** snapshot, cumulative exec time is reset to zero. After a **virtual** snapshot, cumulative time is retained (it still contributes to replay cost on restore). The command log is cleared after every snapshot so each virtual node stores only the commands since its parent.


### 🧩 Controller Helper
All `EnvironmentManager` subclass instance also provides a series of helper methods to assist with common tasks.

The usage of these controllers is well-documented in the `EnvironmentManager` base class, utilizing IDE-supported docstrings.
They are:
- attribute `.backend`: Get the name of the backend used by the controller instance.
- attribute `.current_snapshot`: Get the current snapshot ID of the controller instance.
- `.list_snapshots()`: List all snapshots created by the controller instance.
- `.print_snapshot_tree()`: Print a thread-safe tree view of all snapshots and live branch positions.

### 🍴 Concurrent Forking (`ForkableEnvironmentManager`)

The fork-based Waypoint backend can keep **multiple live instances of one snapshot** running at once.
A *fork* is a running copy (own filesystem layer, own process tree, own shell).

The inherited Template Methods retain their common meaning: `.snapshot()` and `.exec_command()` act on the
current branch (`.current_branch_id`, initially `main`). Named-branch operations belong to the stronger capability,
so clients can depend on `ForkableEnvironmentManager` rather than detecting a concrete Waypoint class.

- `.snapshot()` seals the current fork (always physical in this first forkable implementation; the backend snapshot is a
  destructive dump + re-restore, so the fork resumes under a fresh PID — the registry auto-refreshes);
  `.snapshot_branch(f)` seals fork `f` — always physical, a child of `f`'s previous base in the tree.
- `.snapshot_branch(f, park=True)` / `.park_branch(f)`: **park** — seal *without resuming* (cheapest persist). The fork
  ceases to exist; its state survives as the snapshot and revives via `fork()`. Parking the current fork
  stashes it back to `main`; `main` itself cannot be parked.
- `.restore(id)`: move the current branch — the one **explicit macro**: `fork` the target, switch the
  pointer, `destroy` the departing fork. Its un-sealed state is **discarded by contract** — seal it first
  (`snapshot()`, or `park` for a lossless retire) to keep it; `main` stays live instead. Materialization
  happens before destruction, so a failed restore leaves the current environment untouched.
- `.exec_command(cmd)` runs in the current fork; `.exec_on_branch(f, cmd)` runs in fork `f`. Both feed
  branch-aware benchmark and command bookkeeping. Commands on **different forks**
  run concurrently**; commands on the same fork serialize.
- `.fork(snapshot_id, n=1, ids=None)` is the **only bare materialization verb**: `n` live forks of a
  physical snapshot, returning backend-neutral `EnvironmentBranch` records. Waypoint enriches these as
  `WaypointFork(id, pid, socket, base_checkpoint, ...)` handles.
- `.discard_branch(branch_id)` kills a fork — state is lost (park for a lossless retire); refuses `main` and
  the current fork. `.list_branches()` / `.live_branches` inspect live forks. Andy's `destroy_fork`,
  `list_forks`, `live_forks`, and `current_fork_id` names remain compatibility aliases for now.
- Waypoint's `.sync_snapshot_tree()` hydrates the snapshot tree from the session's checkpoint DAG on disk. It runs
  automatically when a manager attaches, so **`waypoint_attach` to an existing session inherits its full
  history** (parent links included) instead of starting from a blank tree.
- Forkable managers currently accept physical snapshots only and reject non-`AlwaysTrueDecider` policies at construction.

### 🧪 Benchmark
You can enter the benchmark interface through the `.stats` attribute of any `EnvironmentManager` subclass instance.

#### Programmatic Usage
Use the `get_all_statistics()` method to retrieve all statistics, which returns a `BenchmarkResult` object containing 
the time and size statistics for various operations. A sample output is shown below:
```python
BenchmarkResult(
    time={
        'snapshot': Statistics(
            count=6, total=0.738, mean=0.123, median=0.123, min=0.123, max=0.123, unit='seconds'
        ),
        'restore': Statistics(
            count=2, total=0.246, mean=0.123, median=0.123, min=0.123, max=0.123, unit='seconds'
        )
    },
    size={
        'FileSizeCalculator #1': Statistics(
            count=7, total=2048, mean=1024, median=1024, min=512, max=1536, unit='bytes'
        ),
        'ImageCalculator #1': Statistics(
            count=3, total=2048, mean=1024, median=1024, min=512, max=1536, unit='bytes'
        )
    }
)
```

#### Formatted String Usage
We also provide many helper functions to format the statistics into human-readable strings. 

For example, the methods of `print_stats()`, `print_history()`, and `print_size_details()` can be used to retrieve 
formatted strings that summarize different aspects of the statistics at different levels of granularity. 
