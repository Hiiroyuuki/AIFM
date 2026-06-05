"""Everything search endpoint.

GET /api/search?q=<query>&limit=<n>
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from api.state import state

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search")
async def search_files(
    q: str = Query(..., description="Everything SDK search query"),
    limit: int = Query(500, ge=1, description="Maximum number of results to return"),
):
    """Run an Everything search and return matching paths."""
    # Ensure Everything is running (no-op if already started).
    startup_msg = state.ensure_everything_started()

    paths, error_msg = state.search_engine.search(q)
    total = state.search_engine.total_result_count()
    shown = paths[:limit]

    return {
        "query": q,
        "paths": shown,
        "shown": len(shown),
        "total": total,
        "message": error_msg or "",
        "startup_message": startup_msg,
    }
