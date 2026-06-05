"""Folder analysis endpoints.

POST /api/analysis/run       – scan a folder tree and persist sizes
GET  /api/analysis/summary   – read saved sizes for one folder
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.state import state
from mainFunctions import format_bytes

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


# --------------------------------------------------------------------------
# POST /api/analysis/run
# --------------------------------------------------------------------------

class AnalyseRequest(BaseModel):
    path: str


@router.post("/run")
async def run_analysis(req: AnalyseRequest):
    """Recursively scan a folder tree and persist the result to SQLite.

    This may take several seconds for large directories.
    The call runs in a thread so the event loop is not blocked.
    """
    folder = Path(req.path)
    if not folder.exists():
        raise HTTPException(404, f"Path not found: {req.path}")
    if not folder.is_dir():
        raise HTTPException(400, f"Not a directory: {req.path}")

    try:
        record = await asyncio.to_thread(
            state.analysis_store.analyse_and_store, req.path
        )
    except OSError as exc:
        raise HTTPException(500, str(exc))

    return {
        "root_path": record.root_path,
        "folder_path": record.folder_path,
        "size_bytes": record.size_bytes,
        "size": format_bytes(record.size_bytes),
        "file_count": record.file_count,
        "folder_count": record.folder_count,
        "error_count": record.error_count,
        "analysed_at": record.analysed_at,
    }


# --------------------------------------------------------------------------
# GET /api/analysis/summary
# --------------------------------------------------------------------------

@router.get("/summary")
async def get_summary(path: str = Query(..., description="Absolute folder path")):
    """Return the saved analysis summary and direct-child sizes for a folder."""
    summary = state.analysis_store.folder_summary(path)
    child_sizes_raw = state.analysis_store.child_folder_size_map(path)

    child_sizes = {
        p: {"size_bytes": s, "size": format_bytes(s)}
        for p, s in child_sizes_raw.items()
    }

    return {
        "path": path,
        "summary": (
            {**summary, "size": format_bytes(summary["size_bytes"])}
            if summary else None
        ),
        "child_sizes": child_sizes,
    }
