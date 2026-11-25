"""AI Toy Lab backend.  Run:  .venv/bin/python -m backend.main   (or: uvicorn backend.main:app)"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import admin, conversation, devices, ws
from backend.config import ROOT_DIR, settings
from backend.context import AppContext

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-5s %(name)-14s %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpx2", "huggingface_hub", "faster_whisper"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("main")
FRONTEND_DIR = ROOT_DIR / "frontend"


async def _watchdog(ctx: AppContext):
    """Marks devices offline when heartbeats stop (covers power loss without a clean TCP close)."""
    while True:
        await asyncio.sleep(5)
        for did in ctx.devices.expire_stale():
            log.warning("[device] %s heartbeat timeout -> OFFLINE", did)
            ctx.bus.publish("device_offline", device_id=did, reason="heartbeat_timeout")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = AppContext(settings)
    app.state.ctx = ctx
    # Load the local STT model in the background so the first turn isn't slow.
    asyncio.create_task(_warmup(ctx))
    wd = asyncio.create_task(_watchdog(ctx))
    log.info("AI Toy Lab backend ready on %s", settings.backend_url)
    yield
    wd.cancel()
    ctx.db.close()


async def _warmup(ctx: AppContext):
    try:
        await ctx.conversation.stt.warmup()
    except Exception as e:
        log.warning("STT warmup failed (will retry on first use): %s", e)


app = FastAPI(title="AI Toy Lab", version="0.1.0", lifespan=lifespan)
app.include_router(devices.router)
app.include_router(conversation.router)
app.include_router(admin.router)
app.include_router(ws.router)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, log_level="info")
