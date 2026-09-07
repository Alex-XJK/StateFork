# Contributors

This project thrives thanks to the efforts and expertise of the following people.

## Architect & Project Lead

### Alex Jiakai Xu

- Project founder, principal architect, and active maintainer.
- Continues to lead StateFork's technical direction, system design, implementation, integration, and release planning.
- Designed the core StateFork architecture and system model: the `EnvironmentManager` Template Method contract, the factory, the pluggable backend design, and the benchmarking framework.
- Led the implementation of the core controller, the interactive shell, and the backend integration direction across Docker, Podman, CRIU, and Waypoint.
- Designed the v0.7.0 forkable architecture (`ForkableEnvironmentManager`) with its branch-aware, thread-safe manager core; implemented its base skeleton and guided the Waypoint v0.7.0 integration through implementation, testing, and release.


## Project Contributors


### Ruizhe Fu

- Served as the primary owner of the Smart Decider plug-in.
- Designed and implemented the Smart Decider architecture and the initial decider policies for physical and virtual snapshot selection.
- Documented physical vs. virtual snapshots and the built-in decider policies.


### Danielle Gillai

- Extended the original design with the gVisor and Firecracker microVM backends, including their build and attach modes.
- Documented backend setup and helped with testing, benchmarking, and backend evaluation.


### Andy Tiancheng Ge

- Implemented the detailed functions of the v0.7.0 concurrent forking model on the Waypoint backend: ported the backend to the fork-based Waypoint CLI, adopted the single-verb fork API with a current branch, `restore`/`park`, and real return codes, and added checkpoint-DAG hydration when attaching to an existing session.
- Added the `copy_in`/`copy_out` file-transfer verbs and the `snapshot [fork] [--park]` shell command.
- Improved the usability and flexibility of the Waypoint integration: binary resolution and environment-based configuration of the Waypoint launcher, optional-dependency handling, and documentation of the Waypoint-backed workflow.


### Tianle Zhou

- Significantly refactored and improved backend robustness, especially for the Podman-Hybrid backend.
- Led usability integration work between StateFork and the Terminal-Bench framework.



## Advisors

### Prof. Eugene Wu, Prof. Kostis Kaffes

- Serve as project advisors.
- Provide expert guidance on systems, architecture, and research directions.


> To contribute, please submit a PR or contact the maintainers. All contributions, large or small, are appreciated!
