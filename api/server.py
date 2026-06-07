"""FastAPI application entry point for AIFM.

Production mode (single command):
    python start.py

Manual start:
    uvicorn api.server:app --host 127.0.0.1 --port 8000

When 'AI Chat and Browser App/dist/' exists the server also serves the built
React app at http://127.0.0.1:8000/. API and WebSocket routes take precedence.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure the project root is importable so routers can do
# `from mainFunctions import ...` and `from agent import ...`.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.state import state
from api.routers import ai_folders, analysis, app_files, apps, config, fs, search, ws_agent

_REACT_DIST = Path(__file__).resolve().parent.parent / "AI Chat and Browser App" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # ---- startup ----
    # Remove DB entries for AI folders whose directories no longer exist.
    state.ai_folder_store.cleanup_missing_records()
    # Ensure the Everything search process is running.
    state.ensure_everything_started()
    yield
    # ---- shutdown ----
    # Remove the app-trash directory created by delete_for_undo().
    state.file_operations.clear_trash()


app = FastAPI(
    title="AIFM API",
    version="0.1.0",
    description="File-manager backend service for the React frontend.",
    lifespan=lifespan,
)

# Allow all origins for local development (React dev server on :5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(apps.router)
app.include_router(app_files.router)
app.include_router(fs.router)
app.include_router(search.router)
app.include_router(analysis.router)
app.include_router(ai_folders.router)
app.include_router(config.router)
app.include_router(ws_agent.router)


@app.get("/api/health", tags=["meta"])
async def health() -> dict:
    """Quick liveness check."""
    return {
        "status": "ok",
        "everything_started": state._everything_started,
        "can_undo": state.can_undo,
        "can_redo": state.can_redo,
    }


# ── Serve React build (must come last, after all API routes) ──────────────────
if _REACT_DIST.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(_REACT_DIST), html=True),
        name="react",
    )
