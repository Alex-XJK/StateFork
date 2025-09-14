import logging
import os
import time
import uvicorn
import socket

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette import status
from app.kv_store import KVStore
# from app.db_store import DBStore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app.FastAPI")

# Set up logging to file
log_file = "/tmp/fastapi_stats.txt"
file_handler = logging.FileHandler(log_file)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Global database key-value store
kv = KVStore(preload=True)  # Preload with some initial data for testing
# kv = DBStore(preload=True)  # Preload with some initial data for testing

########## Memory Allocation for Testing ##########
ARRAY_SIZE_MB = int(os.getenv("MEMORY_ARRAY_MB", "-1"))  # default None

if ARRAY_SIZE_MB > 0:
    import numpy as np

    NUM_FLOATS = (ARRAY_SIZE_MB * 1024 * 1024) // 8  # 8 bytes for double

    logger.info(f"Allocating large Numpy array of size {ARRAY_SIZE_MB} MB...")
    heavy_array = np.ones(NUM_FLOATS, dtype=np.float64)

def log_memory_usage() -> dict:
    """
    Logs the current process memory usage (VmSize, VmRSS, VmPeak) and returns the stats.
    """
    stats = {}
    with open("/proc/self/status", "r") as f:
        for line in f:
            if line.startswith("VmSize:") or line.startswith("VmRSS:") or line.startswith("VmPeak:"):
                key, val = line.strip().split(":", 1)
                stats[key] = val.strip()

    for key in ("VmSize", "VmRSS", "VmPeak"):
        logger.info(f"[Memory Usage Report] {key}: {stats.get(key, 'N/A')}\n")

    return stats

ls = log_memory_usage()
logger.info(f"System reported memory usage: {ls}")

############ Filesystem Writing for Testing ############

FILE_SIZE_MB = int(os.getenv("FILE_SIZE_MB", "-1"))  # default None
CHUNK_SIZE_MB = 128  # Max per file to avoid tar write-too-long issues
FILE_DIR = "/app/files"

if FILE_SIZE_MB > 0:
    os.makedirs(FILE_DIR, exist_ok=True)
    logger.info(f"Creating dummy files totaling {FILE_SIZE_MB} MB in {FILE_DIR}...")

    num_chunks = (FILE_SIZE_MB + CHUNK_SIZE_MB - 1) // CHUNK_SIZE_MB
    bytes_remaining = FILE_SIZE_MB * 1024 * 1024

    for i in range(num_chunks):
        chunk_path = os.path.join(FILE_DIR, f"chunk_{i:03d}.bin")
        this_chunk_size = min(CHUNK_SIZE_MB * 1024 * 1024, bytes_remaining)
        logger.info(f"Writing {this_chunk_size / (1024*1024):.2f}MB to {chunk_path}")

        with open(chunk_path, "wb") as f:
            for _ in range(this_chunk_size // (1024 * 1024)):
                f.write(os.urandom(1024 * 1024))  # Write 1MB at a time

        bytes_remaining -= this_chunk_size

def log_file_info() -> int:
    """
    Logs the size of all generated chunk files and returns the total size in bytes.
    """
    if FILE_SIZE_MB <= 0:
        return 0

    total_size = 0
    for fname in os.listdir(FILE_DIR):
        path = os.path.join(FILE_DIR, fname)
        total_size += os.path.getsize(path)

    logger.info(f"[File Size Report] Total size of generated chunks: {total_size / (1024*1024):.2f}MB\n")
    return total_size

lf = log_file_info()
logger.info(f"System reported internal file size: {lf / (1024*1024):.2f}MB")

############ FastAPI application setup ############

app = FastAPI(
    title="StateFork API service",
    description="Our testing API service for Container Stateful-branching Benchmark System",
    version="0.1.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for simplicity
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

"""
I deliberately make every endpoint use the GET method for simplicity and testability.
"""

@app.get("/")
def root():
    """
    Root endpoint to check if the API is running.
    """
    return {"message": "Welcome to the StateFork API!"}

@app.get("/get/{key}", status_code=status.HTTP_200_OK)
def get_value(key: str):
    """
    Retrieve the value for a given key from the KV store.
    """
    value = kv.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"key": key, "value": value}


@app.get("/set/{key}/{value}", status_code=status.HTTP_200_OK)
def set_value(key: str, value: str):
    """
    Set a value for a given key in the KV store.
    """
    kv.set(key, value)
    return {"key": key, "value": value}

@app.get("/all", status_code=status.HTTP_200_OK)
def list_all():
    """
    List all key-value pairs in the KV store.
    """
    all_items = kv.all()
    count = len(all_items)
    if not all_items:
        return {"items": {}, "count": 0}
    return {"items": all_items, "count": count}

@app.get("/stats", status_code=status.HTTP_200_OK)
def get_stats():
    """
    Get memory usage and file size statistics for testing purposes.
    """
    memory_stats = log_memory_usage()
    file_size = log_file_info()

    return {
        "file_size_kb": file_size / 1024 if file_size > 0 else 0,
        **memory_stats,
    }
@app.get("/echo")
def echo(msg: str = "hello"):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect("/tmp/echo.sock")
        s.sendall(msg.encode() + b"\n")
        data = s.recv(1024)
    return {"echo": data.decode().strip()}


if __name__ == "__main__":
    uvicorn.run(
        "app.api_server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info"
    )