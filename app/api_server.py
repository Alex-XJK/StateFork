import logging
import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette import status
from app.kv_store import KVStore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global in-memory key-value store
kv = KVStore(preload=True)  # Preload with some initial data for testing

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

# Logging middleware to log requests and responses
rr_counter = 0

@app.middleware("http")
async def log_requests(request, call_next):
    """
    Middleware to log incoming requests and their responses.
    """
    ip = request.client.host
    port = request.client.port
    method = request.method
    url = request.url.path

    global rr_counter
    rr_counter += 1
    local_rr_counter = rr_counter

    logger.info(f"[{local_rr_counter:3d}] Request\tFrom {ip}:{port} - {method} {url}")
    response = await call_next(request)
    logger.info(f"[{local_rr_counter:3d}] Response\tStatus: {response.status_code}")

    return response

"""
I deliberately make every endpoint use the GET method for simplicity and testability.
"""

@app.get("/")
async def root():
    """
    Root endpoint to check if the API is running.
    """
    return {"message": "Welcome to the StateFork API!"}

@app.get("/get/{key}", status_code=status.HTTP_200_OK)
async def get_value(key: str):
    """
    Retrieve the value for a given key from the KV store.
    """
    value = kv.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"key": key, "value": value}


@app.get("/set/{key}/{value}", status_code=status.HTTP_200_OK)
async def set_value(key: str, value: str):
    """
    Set a value for a given key in the KV store.
    """
    kv.set(key, value)
    return {"key": key, "value": value}

@app.get("/all", status_code=status.HTTP_200_OK)
async def list_all():
    """
    List all key-value pairs in the KV store.
    """
    all_items = kv.all()
    count = len(all_items)
    if not all_items:
        return {"items": {}, "count": 0}
    return {"items": all_items, "count": count}


if __name__ == "__main__":
    uvicorn.run(
        "app.api_server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )