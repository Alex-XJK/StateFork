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
Commands: snapshot, restore <id>, step, tree, stats, history, storage, exit

StateFork > _
```
See the sample run screenshot below.

### 3. Common Commands
| Command	      | Description                                              |
|---------------|----------------------------------------------------------|
| snapshot	     | Take a snapshot of the current state                     |
| restore {id}	 | Roll back to a given snapshot ID                         |
| step	         | Snapshot and restore immediately to simulate progression |
| cmd {command} | Execute a shell command inside the managed environment   |
| fork {id} [n] | Materialize n live forks of a snapshot (Waypoint only)   |
| forks         | List live forks (Waypoint only)                          |
| fexec {fork} {command} | Execute a command in a specific live fork (Waypoint only) |
| snapfork {fork} | Seal a live fork into a new snapshot (Waypoint only)   |
| destroy {fork} | Destroy a live fork (Waypoint only)                     |
| tree	         | Show snapshot tree structure                             |
| stats	        | Show benchmarking results                                |
| history	      | Show operation history                                   |
| storage	      | Show storage usage and details                           |
| exit	         | Clean up and exit the manager                            |

### 📸 Sample Run
![Sample Run Screenshot](../docs/sample_run.png)


## 🚀 RPC Interface

To be implemented in the future, allowing remote management of snapshots and state.