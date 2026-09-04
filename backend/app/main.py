import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import config
from app.api.routes import router as api_router
from app.api.ws import router as ws_router
from app.db import Database
from app.jobs.manager import JobManager
from app.version import __version__

logging.basicConfig(
    level=os.environ.get("SCTE35_ANALYZER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("scte35_analyzer")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    app.state.db = Database(config.DB_PATH)
    app.state.jobs = JobManager(app.state.db)
    app.state.jobs.bind_event_loop(asyncio.get_running_loop())
    app.state.jobs.reconcile_orphaned_jobs()
    app.state.jobs.start_retention_sweep()
    log.info("scte35-analyzer backend ready. Storage: %s", config.STORAGE_DIR)
    yield
    await app.state.jobs.stop_retention_sweep()


app = FastAPI(title="SCTE35 Analyzer", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(ws_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built frontend (see the top-level Dockerfile's frontend build
# stage) as a single-page app. Mounted LAST and at "/" so it only catches
# requests the API routers above didn't already claim -- see the README
# for why this app deliberately avoids client-side URL routing (job
# selection is in-page state, not a route), which keeps this a plain
# static-file mount instead of needing an SPA fallback-to-index.html rule.
if os.path.isdir(config.FRONTEND_DIST_DIR):
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIST_DIR, html=True), name="frontend")
else:
    log.warning("Frontend build directory %s not found -- API-only mode "
                "(expected in local backend-only development; the Docker "
                "image always has this directory).", config.FRONTEND_DIST_DIR)
