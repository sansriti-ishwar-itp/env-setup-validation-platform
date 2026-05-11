"""FastAPI entrypoint: operator APIs + mounted ADK REST server."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .adk_services import (
    DEFAULT_CORS_ORIGINS,
    adk_session_service_uri,
    agents_dir,
    build_adk_fastapi_app,
)
from .api.routes_adk_meta import router as adk_meta_router
from .api.routes_runs import router as runs_router
from .config import get_database_url
from .services.run_store import RunStore
from .services.store_singleton import set_run_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_AGENTS_DIR = agents_dir()
_ADK_SESSION_URI = adk_session_service_uri()
_CORS = DEFAULT_CORS_ORIGINS

adk_fastapi = build_adk_fastapi_app(allow_origins=_CORS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_url = get_database_url()
    logger.info("Using RunStore database %s", db_url)
    logger.info("ADK agents_dir=%s session_service_uri=%s", _AGENTS_DIR, _ADK_SESSION_URI)
    store = RunStore(db_url)
    app.state.store = store
    app.state.adk_app = adk_fastapi
    set_run_store(store)
    yield


app = FastAPI(
    title="Environment Setup Validation Platform",
    description="Operator APIs + ADK REST (/adk) for run_sse, sessions, traces, resume.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs_router)
app.include_router(adk_meta_router)
app.mount("/adk", adk_fastapi)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "adk": "mounted at /adk"}
