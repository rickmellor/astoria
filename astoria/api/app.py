"""Astoria HTTP entrypoint — one FastAPI app serving REST + MCP (/mcp/) + the in-process worker.

    uvicorn astoria.api.app:app --host 0.0.0.0 --port 8933

Lifespan (the FastMCP gotcha): FastMCP's streamable-HTTP session manager only runs inside
`mcp_app.lifespan`, so mounting the sub-app is not enough — the parent's lifespan must enter it.
We wrap it: enter MCP lifespan → migrate DB → start the worker task → serve → stop worker →
close the pool. If FastMCP fails to import/build, REST still comes up (MCP disabled, logged).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from datetime import UTC, datetime

from fastapi import FastAPI

from astoria.api.auth import client_from_headers
from astoria.api.rest import router
from astoria.config import settings
from astoria.store import db

log = logging.getLogger("astoria.app")
logging.basicConfig(level=getattr(logging, settings().log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---- MCP (best-effort) -------------------------------------------------------

mcp = None
mcp_app = None
try:
    from astoria.api.mcp_tools import build_mcp
    mcp = build_mcp()
    mcp_app = mcp.http_app(path="/")
except Exception as e:  # noqa: BLE001 — REST must survive
    log.warning("MCP endpoint disabled: %s", e)


# ---- worker supervision ------------------------------------------------------

async def _start_worker(stop: asyncio.Event) -> asyncio.Task | None:
    """Start cognify/curator loop if enabled and the module exists (it may still be being written)."""
    if not settings().worker_enabled:
        log.info("worker disabled (ASTORIA_WORKER_ENABLED=false)")
        return None
    try:
        from astoria.cognify import worker
    except ImportError as e:
        log.warning("worker module not available (%s); running API-only", e)
        return None

    async def _supervised():
        try:
            await worker.run_forever(stop)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker loop crashed; API keeps serving")

    return asyncio.create_task(_supervised(), name="astoria-worker")


@contextlib.asynccontextmanager
async def _core_lifespan():
    """DB migrate → worker task → serve → stop worker → close pool."""
    stop = asyncio.Event()
    try:
        applied = await asyncio.to_thread(db.migrate)
        if applied:
            log.info("migrations applied: %s", applied)
    except Exception:
        log.exception("db.migrate failed at startup; continuing (health will report)")
    task = await _start_worker(stop)
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            except Exception:  # noqa: BLE001
                pass
        with contextlib.suppress(Exception):
            db.close_pool()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    if mcp_app is not None:
        async with mcp_app.lifespan(app), _core_lifespan():
            yield
    else:
        async with _core_lifespan():
            yield


app = FastAPI(title="Astoria memory service", version=settings().version, lifespan=_lifespan,
              description="Layered, trusted, bitemporal memory for Nova's agents — REST + MCP (/mcp/).")


# ---- JSON-lines request log (pure ASGI so SSE/MCP streams are untouched) -------

class _JsonLog:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        status = {"code": 0}

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
                rec = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                       "method": scope.get("method"), "path": scope.get("path"),
                       "status": status["code"], "ms": round((time.perf_counter() - t0) * 1000, 1),
                       "client": client_from_headers(headers)}
                sys.stdout.write(json.dumps(rec, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001
                pass


app.add_middleware(_JsonLog)

if mcp_app is not None:
    app.mount("/mcp", mcp_app)

app.include_router(router)


@app.get("/")
def root():
    return {"service": "astoria", "version": settings().version, "docs": "/docs",
            "mcp": "/mcp/" if mcp_app is not None else None}
