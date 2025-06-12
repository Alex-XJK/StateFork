# StateFork: A Lightweight Versioned Container Manager

**StateFork** is a simple container-based snapshotting and benchmarking tool designed to manage runtime environments in a version-controlled manner. It allows you to take snapshots of running containers, restore to previous versions, and benchmark key operations — all without modifying the target application.

## 🌟 Features

- 🌱 Create container snapshots from a running app (`docker commit`)
- 🔁 Restore any previous snapshot and relaunch the container
- 🧪 Measure and log performance of snapshot, restore, and switch operations
- 📦 Designed for unmodified applications (e.g. FastAPI, Flask, etc.)
- 🔧 CLI-based interface for interactive experiment control

## 🗂 Project Structure
```
.
├── Dockerfile
├── README.md
├── app
│   ├── api_server.py
│   └── kv_store.py
├── controller
│   ├── benchmark.py
│   └── container_manager.py
└── requirements.txt
```


## 🚀 Quick Start

### 1. Prepare the Base App

Ensure your application (e.g., `app/api_server.py`) is working and can run via:

```bash
uvicorn app.api_server:app --host 0.0.0.0 --port 8000
```

### 2. Run the Container Manager
```bash
python3 controller/container_manager.py
```
You will enter an interactive shell like:
```
StateFork Container Manager
Commands: snapshot, list, restore <id>, step, stats, exit
StateFork > _
```

### 3. Common Commands
| Command	      | Description                                        |
|---------------|----------------------------------------------------|
| snapshot	     | Commit current container as a new image (snapshot) |
| step	         | Take snapshot and create a new container from it   |
| restore <id>	 | Restore to a previous snapshot (by snapshot ID)    |
| list	         | List all available snapshot IDs                    |
| stats	        | Show timing benchmark for all operations           |
| exit	         | Exit the manager                                   |

## 🔧 Requirements
- Docker (installed and running)
- Python 3.10+
- FastAPI and Uvicorn (see `requirements.txt`)

## 📊 Benchmarking Support
The tool logs and displays operation performance statistics such as:
- Snapshot creation time
- Container restore time
- Sequential operation logging with timestamps

