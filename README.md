# <img src="./docs/StateFork-logo-notext.png" height="30" /> StateFork: A Lightweight Versioned Container Manager

**StateFork** is a simple, modular snapshotting and benchmarking tool for managing long-running applications in a 
version-controlled and reproducible environment. It supports both container-based (Docker, Podman) and process-based (CRIU) backends, 
enabling users to take snapshots, roll back state, and benchmark key operations across different platforms.

## 🌟 Features

- 🌱 Take and manage snapshots of running apps
- 🔀 **Physical or virtual snapshots** via a pluggable decider policy (see [Smart Decider](#-smart-decider))
- 🔁 Restore to previous snapshots instantly
- 🍴 **Concurrent live forks** (Waypoint backend): materialize N isolated running copies of one snapshot and drive them in parallel
- 🧪 Benchmark time and storage performance of snapshot/restore operations
- 🧩 Works with unmodified apps (FastAPI, Python/C++ scripts, etc.)
- ⚙️ CLI-based interactive interface
- 🧱 Easily extendable backend design with Docker, Podman, CRIU, or FirecrackerVM support

## 🗂 Project Structure
```
StateFork/
  ├── Dockerfile
  ├── README.md
  ├── app/                    # Sample applications
  │   ├── stateful_logger.py
  │   ├── api_server.py
  │   └── ...
  ├── controller/            # Core controller package
  │   ├── __init__.py
  │   ├── README.md
  │   ├── base_env_manager.py
  │   ├── benchmark.py
  │   ├── criu_env_manager.py
  │   ├── container_env_manager.py
  │   ├── hybrid_env_manager.py
  │   └── ...
  ├── decider/               # Snapshot decision policies (physical vs virtual)
  │   ├── __init__.py
  │   └── decider.py
  ├── interface/             # Interface entrypoints
  │   ├── README.md
  │   └── shell.py
  ├── logs/                  # Benchmark and package analysis files
  ├── scripts/               # Testing and utility scripts
  └── requirements.txt
```

## 🔧 Environment Manager Variants
StateFork provides multiple environment manager variants, each tailored to different backend technologies and lifecycle scenarios.

They follow the naming convention `{Backend}{Action}Manager`, where:
- **{Backend}** Backend type:
  - `Container` for Docker/Podman (manages file system state only)
  - `CRIU` for process-level CRIU checkpointing
  - `Hybrid` for Podman + CRIU (captures both file and process states) 
  - `Waypoint` for Waypoint, a lightweight checkpointing tool (captures both file and process states)
  - `Gvisor` for Docker in gVisor (captures both file and process states)
  - `Fire` for Firecracker microVM (captures process states)
- **{Action}** Lifecycle mode:
  - `Build` starts a fresh instance (usually from a Dockerfile or base image)
  - `Attach` connects to an existing container or process

### 🏭 Factory Method Support
StateFork encourages a clean and extensible design by providing
a unified **Factory Method** to simplify the instantiation of different environment managers. 

Instead of importing backend-specific classes, users can create the appropriate manager by calling a single 
`create_env_manager(method=..., **kwargs)` function. This design improves usability, 
decouples interface logic from implementation, and enables easier CLI/RPC/agent integration.

Simply specify the desired environment type (e.g., `"criu_attach"`, `"docker_build"`) along with the required parameters, and the 
factory will handle the rest.
```python
from controller import create_env_manager
manager = create_env_manager(method="criu_attach", target_pid=12345)
```
See the full method table below for supported types and arguments.

| Factory Call Name | Env Manager                 | Backend         | Required Arguments                       | Optional Arguments                                                                       |
|-------------------|-----------------------------|-----------------|------------------------------------------|------------------------------------------------------------------------------------------|
| `docker_build`    | ContainerBuildManager       | Docker          |                                          | `dockerfile_dir(str)`, `base_image(str)`, `extra_args(List[str])`                        |
| `docker_attach`   | ContainerAttachManager      | Docker          | `container_name(str)`, `base_image(str)` | `extra_args(List[str])`                                                                  |
| `podman_build`    | ContainerBuildManager       | Podman          |                                          | `dockerfile_dir(str)`, `base_image(str)`, `extra_args(List[str])`                        |
| `podman_attach`   | ContainerAttachManager      | Podman          | `container_name(str)`, `base_image(str)` | `extra_args(List[str])`                                                                  |
| `criu_build`      | CRIUBuildManager            | CRIU            |                                          | `work_dir(str)`, `command(List[str])`                                                    |
| `criu_attach`     | CRIUAttachManager           | CRIU            | `target_pid(int)`                        | `work_dir(str)`                                                                          |
| `hybrid_build`    | HybridBuildManager          | Podman + CRIU   |                                          | `container_name(str)`, `dockerfile_dir(str)`, `export_dir(str)`, `extra_args(List[str])` |
| `hybrid_attach`   | HybridAttachManager         | Podman + CRIU   | `container_name(str)`                    | `export_dir(str)`                                                                        |
| `waypoint_build`  | WaypointBuildManager        | Waypoint        |                                          | `dockerfile_dir(str)`, `build(bool)`                                                    |
| `waypoint_attach` | WaypointAttachManager       | Waypoint        | `session_id(str)`                        | `target_pid(int)` *(obsolete; ignored)*                                                  |
| `gvisor_build`    | GvisorBuildManager          | gVisor + Docker |                                          | `dockerfile_dir(str)`, `base_image(str)`, `extra_args(List[str])`                        |
| `gvisor_attach`   | GvisorAttachManager         | gVisor + Docker | `container_name(str)`, `base_image(str)` | `extra_args(List[str])`                                                                  |
|`firecracker_build`| FireBuildManager            | Firecracker     |                                          | `fire_parent_dir(str)`, `inject_dir(str)`                                                |
|`firecracker_attach`| FireAttachManager          | Firecracker     |`pid(int)`, `microvm_ip(str)`, `tap_dev(str)`, `key(str)`, `checkpoint_dir(str)`, `vm_dir(str)`, `fire_binary(str)`, `api_socket(str)`| |

## 🔀 Smart Decider

When `snapshot()` is called, StateFork can create either a **physical** or **virtual** snapshot:

- **Physical snapshot** — invokes the backend’s full checkpoint path (e.g., container commit, CRIU dump, or Waypoint capture). Restore reloads that state in one step.
- **Virtual snapshot** — records only the commands executed since the last snapshot, without calling the backend checkpoint. Restore walks up to the nearest physical ancestor, restores it, then replays the stored commands in order.

This lets callers trade capture cost and storage against restore-time replay. The choice is made by a **`Decider`** strategy passed into any `EnvironmentManager` (default: always physical). Built-in policies live in `decider/`; the interactive shell selects them with `--decider`.

See `controller/README.md` for snapshot/restore behavior and all built-in deciders.

## 🧪 Benchmarking Support
StateFork automatically logs and benchmarks the performance of:

- Snapshot creation
- Restore operations
- Tree-based version tracking
- Time-based operation history
- Storage usage

## ⚙️ Core Controller Design
![Design Diagram](docs/design.drawio.png)

## 🔧 Requirements
### Python Environment
- Python 3.10+
- Install Python dependencies:
```bash
pip install -r requirements.txt
```

### Docker Method
- Docker must be installed and running.
- Make sure your user has permission to run Docker commands.

### Podman Method
- Podman must be installed and running.
- Make sure your user has permission to run Podman commands.

### CRIU Method
- Linux kernel compiled with CRIU support.
    - You may use the provided universal AKCS helper `scripts/kconfig.sh` with the `-r` option to generate a compatible kernel config.
- Install `criu` tool from: https://launchpad.net/~criu/+archive/ubuntu/ppa or your system package manager.
- Root or `sudo` privileges are required.

### Hybrid Method (Podman + CRIU)
- CRIU and Podman must be installed as above.
- Must use Root or `sudo` privileges to run Podman commands, as [the checkpoints currently work with root containers only](https://podman.io/docs/checkpoint).
- Manually set the OCI runtime in `/usr/share/containers/containers.conf` to use **runc** instead of the default **crun**.

### Waypoint Method (CRIU + OverlayFS)
- Waypoint must be installed from: [github.com/Alex-XJK/waypoint](https://github.com/Alex-XJK/waypoint)
- **A fork-capable Waypoint build is required** (the concurrent-forking model with `checkpoint` / `fork` / `snapshot` / `exec <fork> --` commands). StateFork probes the binary at startup and refuses older `create`/`restore`-style builds with a clear error. Generic verbs act on the **current branch** (a movable current fork, initially `main`) and take an optional `fork_id=` to target any live fork: `snapshot([fork_id=][, park=True])` seals a fork into an immutable checkpoint (`park` seals without resuming — the lossless retire), `exec_command(cmd[, fork_id=])` runs commands in a fork's persistent shell, `restore(id)` moves the current branch (destroying the departing fork — seal or `park` it first to keep its state), and `fork(n)` / `destroy_fork` / `list_forks` manage concurrent live forks. The redundant `create_env_from_snapshot()` is refused by design — `fork()` is the only bare materialization verb. See `scripts/waypoint_fork_demo.py` for an end-to-end tour.
- Make the `waypoint` binary discoverable in one of three ways: set the `WAYPOINT_BIN` environment variable to its full path, place it on your `PATH`, or symlink it into the repository root (e.g. `ln -s /path/to/waypoint ./waypoint`). The binary is intentionally not committed. Other Waypoint runtime settings, such as `bash_init`, session storage, and cleanup behavior, are resolved by Waypoint using its own environment/config/default precedence.
- Root or `sudo` privileges are required.

#### Environment variables (optional)

Copy `env.sample.sh` to `env.local.sh`, edit it, and `source ./env.local.sh` before running. All variables are optional.

| Variable | Purpose | Default / fallback |
|---|---|---|
| `WAYPOINT_BIN` | Path to the `waypoint` binary used by **StateFork** | `$PATH`, then `<repo>/waypoint` |
| `WAYPOINT_CONFIG` | Waypoint config file for runtime settings | Waypoint config search order |
| `WAYPOINT_BASH_INIT_SRC` | Path to the `bash_init` helper, read by Waypoint | Waypoint config/default |
| `WAYPOINT_SESSIONS_DIR` | Directory for Waypoint session state | Waypoint config/default |
| `WAYPOINT_PRESERVE_SESSION_ON_CLEANUP` | Preserve session directories during Waypoint cleanup | Waypoint config/default |

Check the Waypoint [user guide](https://github.com/Alex-XJK/waypoint#0-optional-configure-global-settings) for more details on these variables and their precedence.

### gVisor Method (with Docker)
- Docker must be installed and running.
- Docker must use the runsc runtime (see [gVisor installation guide](https://gvisor.dev/docs/user_guide/install/) and then [configuring Docker](https://gvisor.dev/docs/user_guide/quick_start/docker/)).
- Docker must have experimental features enabled (add `"experimental": true` to `/etc/docker/daemon.json`, and then run `sudo systemctl restart docker`)
- Make sure your user has permission to run Docker commands.

#### gVisor Limitation: networking is not compatible with checkpoint/restore
- The `--network=host` flag must be passed to Docker for checkpoint/restore to work at all (see [Moby issue](https://github.com/moby/moby/issues/50750)).
- For gVisor to use the host network stack and fully achieve network passthrough, the `--network=host` flag must also be passed to runsc (see [gVisor user guide](https://gvisor.dev/docs/user_guide/networking/)).
- However, this full network passthrough then breaks checkpoint/restore (see gvisor [source code](https://github.com/google/gvisor/blob/d23cf24593c311b17f79b6350fe0f629423a98ba/runsc/boot/restore.go#L472) and [issue](https://github.com/google/gvisor/issues/6243)).

The result is a catch-22 in network functionality: checkpoint/restore requires `--network=host` passed to Docker, but full network passthrough requires `--network=host` passed to runsc, breaking checkpoint/restore.

### Firecracker Method
- Root or `sudo` privileges are required.

---
For core controller usage, see the `controller/README.md` file.  
For interface usage, see the `interface/README.md` file.

> Want to contribute? File issues or PRs in the GitHub repo!

## Citation

If you use StateFork in academic research, please cite:

```bibtex
@misc{xu2025systemsfoundationsagenticexploration,
      title={Toward Systems Foundations for Agentic Exploration}, 
      author={Jiakai Xu and Tianle Zhou and Eugene Wu and Kostis Kaffes},
      year={2025},
      eprint={2510.05556},
      archivePrefix={arXiv},
      primaryClass={cs.DC},
      url={https://arxiv.org/abs/2510.05556}, 
}
```
