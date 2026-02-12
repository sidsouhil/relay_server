import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Literal, Optional, Set, Tuple

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------
# Config & Logging
# ---------------------------------------------------------------------
API_KEY = os.getenv("RELAY_API_KEY", "").strip()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("relay")
if not API_KEY:
    logger.warning("RELAY_API_KEY is not set! Set it in Render env vars.")

app = FastAPI(title="SDL/Unity Relay", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
Workstation = Literal["elegoo_3d", "pumps_sdl", "opentrons"]
Client = Literal["unity", "sdl"]
Source = Literal["unity", "sdl", "platform", "relay"]
Target = Literal["unity", "sdl", "broadcast"]
MsgType = Literal[
    "waypoint",
    "event",
    "frequency",
    "platform_update",
    "command",
    "status",
    "camera",
]


class Envelope(BaseModel):
    workstation: Workstation
    source: Source
    target: Target
    type: MsgType
    ts: int = Field(..., description="Unix epoch ms")
    payload: Dict[str, Any]

    @field_validator("ts")
    @classmethod
    def ts_positive(cls, v: int):
        if v <= 0:
            raise ValueError("ts must be a positive unix ms timestamp")
        return v


# ---------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------
def require_key(key: Optional[str]) -> None:
    if not API_KEY:
        return
    if key is None or key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# ---------------------------------------------------------------------
# In-memory storage
# ---------------------------------------------------------------------
MAX_BUFFER = 500  # per (workstation, audience)
MAX_QUEUE = 200   # per live subscriber
KEEPALIVE_SEC = 15

buffers: Dict[Tuple[str, str], Deque[Dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=MAX_BUFFER)
)
broadcast_buffers: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
    lambda: deque(maxlen=MAX_BUFFER)
)


class Subscriber:
    def __init__(self, workstation: str, audience: str):
        self.workstation = workstation
        self.audience = audience
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)


subscribers: Set[Subscriber] = set()
sub_lock = asyncio.Lock()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def route_targets(env: Envelope) -> List[str]:
    if env.target == "broadcast":
        return ["unity", "sdl"]
    return [env.target]


async def enqueue_for_audience(workstation: str, audience: str, message: Dict[str, Any]):
    buffers[(workstation, audience)].append(message)
    async with sub_lock:
        for sub in list(subscribers):
            if sub.workstation == workstation and sub.audience == audience:
                if sub.queue.full():
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    sub.queue.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning("Dropping message for slow subscriber (%s)", audience)


async def fan_out(env: Envelope):
    message = env.dict()
    targets = route_targets(env)
    for audience in targets:
        await enqueue_for_audience(env.workstation, audience, message)
    if env.target == "broadcast":
        broadcast_buffers[env.workstation].append(message)


async def sse_stream(workstation: str, audience: str):
    sub = Subscriber(workstation, audience)
    async with sub_lock:
        subscribers.add(sub)
    logger.info("SSE connected: %s/%s", workstation, audience)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(sub.queue.get(), timeout=KEEPALIVE_SEC)
                data = json.dumps(msg)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        async with sub_lock:
            subscribers.discard(sub)
        logger.info("SSE disconnected: %s/%s", workstation, audience)


def merged_recent(workstation: str, audience: str, limit: int) -> List[Dict[str, Any]]:
    own = list(buffers[(workstation, audience)])
    bcast = list(broadcast_buffers[workstation])
    merged = own + bcast
    merged.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"


@app.get("/sse")
async def sse_endpoint(
    workstation: str,
    client: Client,
    key: Optional[str] = None,
):
    require_key(key)
    generator = sse_stream(workstation, client)
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "text/event-stream",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(generator, headers=headers)


@app.post("/send")
async def send_endpoint(
    request: Request,
    key: Optional[str] = None,
):
    require_key(key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        env = Envelope.parse_obj(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid envelope: {exc}")

    await fan_out(env)
    return {"status": "ok"}


@app.get("/recent")
async def recent_endpoint(
    workstation: str,
    client: Client,
    limit: int = 50,
    key: Optional[str] = None,
):
    require_key(key)
    limit = max(1, min(limit, MAX_BUFFER))
    items = merged_recent(workstation, client, limit)
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
