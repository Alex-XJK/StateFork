# StateFork Interfaces

## ⚙️ Interactive CLI Interface

### 1. Launch the Interactive Shell
```bash
(sudo) python3 -m interface.shell --method {method} --decider {decider} --threshold {seconds}
```
Currently supported methods:
- `docker` (default) - for Docker-based environments focused on filesystem snapshots
- `podman` - for Podman-based environments focused on filesystem snapshots
- `criu` - for CRIU-based environments focused on process state snapshots
- `hybrid` - for Podman+CRIU environments combining filesystem and process state snapshots
- `ckpt` - for our own [Checkpoint-lite](https://github.com/Alex-XJK/checkpoint-lite/) environments
Currently supported deciders:
- `always_true` (default) - always take a physical snapshot
- `always_false` - always take a virtual snapshot
- `random` - randomly decide between physical and virtual
- `threshold` - take a physical snapshot only after cumulative execution time exceeds `--threshold`


### 2. Inside the Interactive Shell
After launching the shell with the desired method, you will see a prompt similar to this:
```
StateFork Container Manager - Interactive Shell
commands: snapshot, restore <id>, fork <branch> <snapshot_id>, checkout <branch>, branches, 
cmd <command>, tree, stats, history, storage, set, exit

StateFork > _
```
See the sample run screenshot below.

### 3. Common Commands
| Command	                    | Description                                              |
|-------------------------------|----------------------------------------------------------|
| snapshot	                    | Take a snapshot of the current state                     |
| restore {id}	                | Roll back to a given snapshot ID                         |
| step	                        | Snapshot and restore immediately to simulate progression |
| cmd {command}                 | Execute a shell command inside the managed environment   |
| fork {branch} {snapshot_id}   | Create a new branch from a given snapshot                |
| checkout {branch}             | Switch the active branch                                 |
| branches                      | List all branches with their head snapshot and container |
| tree	                        | Show snapshot tree structure                             |
| stats	                        | Show benchmarking results                                |
| history	                    | Show operation history                                   |
| storage	                    | Show storage usage and details                           |
| exit	                        | Clean up and exit the manager                            |

### 📸 Sample Run
![Sample Run Screenshot](../docs/sample_run.png)


## 🚀 RPC Interface

To be implemented in the future, allowing remote management of snapshots and state.