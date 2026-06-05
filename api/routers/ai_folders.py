"""AI Folder management endpoints.

GET  /api/ai-folders               – list registered AI folders
POST /api/ai-folders               – create a new AI folder on disk + register it
GET  /api/ai-folders/by-path       – get one AI folder by filesystem path
PUT  /api/ai-folders/by-path/auth-mode    – change authorization mode
PUT  /api/ai-folders/by-path/permissions  – change read/write/delete/execute flags
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.state import state
from mainFunctions import AIFolderStore

router = APIRouter(prefix="/api/ai-folders", tags=["ai-folders"])


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------

class CreateAIFolderRequest(BaseModel):
    parent_path: str
    name: str = "New AIFolder"
    authorization_mode: str = AIFolderStore.AUTH_USER_REQUIRED


class AuthModeRequest(BaseModel):
    path: str
    authorization_mode: str


class PermissionsRequest(BaseModel):
    path: str
    read: bool = True
    write: bool = True
    delete: bool = False
    execute: bool = False


# --------------------------------------------------------------------------
# GET /api/ai-folders
# --------------------------------------------------------------------------

@router.get("")
async def list_ai_folders(parent: str = Query(None, description="Filter by parent folder path")):
    """Return all registered AI folders, optionally filtered by parent."""
    return state.ai_folder_store.query_ai_folders(parent_path=parent)


# --------------------------------------------------------------------------
# POST /api/ai-folders
# --------------------------------------------------------------------------

@router.post("")
async def create_ai_folder(req: CreateAIFolderRequest):
    """Create a new AI-managed folder on disk and register it in the database."""
    if not Path(req.parent_path).is_dir():
        raise HTTPException(400, f"Parent folder not found: {req.parent_path}")

    try:
        mode = state.ai_folder_store.normalized_authorization_mode(req.authorization_mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    try:
        record = state.ai_folder_store.create_ai_folder(
            parent_path=req.parent_path,
            name=req.name,
            authorization_mode=mode,
            aifm_params={"created_by": "api", "visible_in_browser": True},
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc))

    return asdict(record)


# --------------------------------------------------------------------------
# GET /api/ai-folders/by-path
# --------------------------------------------------------------------------

@router.get("/by-path")
async def get_ai_folder(path: str = Query(..., description="Absolute folder path")):
    """Return the AI folder record for a specific folder path."""
    record = state.ai_folder_store.get_ai_folder(path)
    if record is None:
        raise HTTPException(404, f"AI folder not registered: {path}")
    return record


# --------------------------------------------------------------------------
# PUT /api/ai-folders/by-path/auth-mode
# --------------------------------------------------------------------------

@router.put("/by-path/auth-mode")
async def set_auth_mode(req: AuthModeRequest):
    """Change the authorization mode for an existing AI folder."""
    try:
        state.ai_folder_store.set_authorization_mode(req.path, req.authorization_mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


# --------------------------------------------------------------------------
# PUT /api/ai-folders/by-path/permissions
# --------------------------------------------------------------------------

@router.put("/by-path/permissions")
async def set_permissions(req: PermissionsRequest):
    """Update the read/write/delete/execute permission flags for an AI folder."""
    try:
        state.ai_folder_store.set_permissions(
            req.path,
            {"read": req.read, "write": req.write, "delete": req.delete, "execute": req.execute},
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}
