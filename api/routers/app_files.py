"""App-file index endpoints.

POST /api/app-files/rebuild       – full rebuild of the app→file index
POST /api/app-files/project       – rebuild the link projection tree
GET  /api/app-files/by-app         – files belonging to one app
GET  /api/app-files/summary        – per-app file counts
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.state import state

router = APIRouter(prefix="/api/app-files", tags=["app-files"])


@router.post("/rebuild")
async def rebuild_index():
    """Rebuild the app-file index from Everything + installed apps registry.

    This runs the full scan in a thread because it may take several seconds.
    """
    stats = await asyncio.to_thread(state.app_file_index.build_index)
    return stats


@router.post("/project")
async def rebuild_projection():
    """Rebuild the link projection tree on disk.

    The file index must already be up-to-date (call ``POST …/rebuild`` first).
    This can be slow when creating many .lnk shortcuts — it runs in a thread.
    """
    stats = await asyncio.to_thread(state.link_projection.rebuild)
    return {
        **stats,
        "hint": "Open the projection root in Explorer to browse files by app. "
                "Run POST …/rebuild first if the index is stale.",
    }


@router.get("/by-app")
async def files_by_app(
    name: str = Query(..., description="Exact installed application name"),
):
    """Return all indexed files for one installed application."""
    files = state.app_file_index.files_for_app(name)
    return files


@router.get("/summary")
async def app_file_summary():
    """Return per-app file counts, ordered by file_count descending."""
    return state.app_file_index.apps_with_files()
