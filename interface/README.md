# StateFork Interfaces

## ⚙️ Interactive CLI Interface

### 1. Launch the Interactive Shell
```bash
(sudo) python3 -m interface.shell --method {method}
```
Currently supported methods:
- `docker` (default) - for Docker-based environments focused on filesystem snapshots
- `podman` - for Podman-based environments focused on filesystem snapshots
- `criu` - for CRIU-based environments focused on process state snapshots
- `hybrid` - for Podman+CRIU environments combining filesystem and process state snapshots
- `waypoint` - for Waypoint environments
- `ckpt` - legacy alias for `waypoint`

### 2. Inside the Interactive Shell
After launching the shell with the desired method, you will see a prompt similar to this:
```
StateFork Container Manager - Interactive Shell
Commands: snapshot, restore <id>, tree, stats, history, storage, exit

StateFork > _
```
See the sample run screenshot below.

### 3. Common Commands
| Command	      | Description                                              |
|---------------|----------------------------------------------------------|
| snapshot [fork] [--park] | Take a snapshot of the current fork, or of a named live fork (`[fork]`/`--park` require a forkable backend, currently Waypoint). `--park` seals **without resuming** — a lossless retire; parking the current fork stashes it back to `main` |
| restore {id}	 | Roll back to a given snapshot ID                         |
| cmd {command} | Execute a shell command inside the managed environment   |
| fork {id} [n] | Materialize n live forks of a snapshot (forkable backends only)   |
| forks         | List live forks (forkable backends only)                          |
| fexec {fork} {command} | Execute a command in a specific live fork (forkable backends only) |
| destroy {fork} | Destroy a live fork — state is lost (forkable backends only)     |

> On the fork-based Waypoint backend, generic commands (`cmd`, plain `snapshot`) target the **current fork** (initially `main`). `restore <id>` moves the current branch: the target is materialized as a fresh fork and the departing fork is **destroyed** — seal it first (`snapshot` / `park`) to keep its state; `main` stays live instead. Shell exit codes: 0 = clean exit (`exit` or stdin EOF), 1 = crash, 2 = startup failure.
| tree	         | Show snapshot tree structure                             |
| stats	        | Show benchmarking results                                |
| history	      | Show operation history                                   |
| storage	      | Show storage usage and details                           |
| exit	         | Clean up and exit the manager                            |

### 📸 Sample Run
![Sample Run Screenshot](../docs/sample_run.png)


## 🚀 RPC Interface

To be implemented in the future, allowing remote management of snapshots and state.
