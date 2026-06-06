"""File-system browsing and mutation endpoints.

GET  /api/fs/list       – list a folder (entries + child analysis sizes)
GET  /api/fs/stat       – metadata + analysis summary for one path
GET  /api/fs/icon       – extract system icon for any file as PNG
POST /api/fs/open       – open a path with the OS default app
POST /api/fs/paste      – copy or move files (pushes undo entry)
POST /api/fs/delete     – move to app-trash (pushes undo entry)
POST /api/fs/undo       – undo last operation
POST /api/fs/redo       – redo last undone operation
GET  /api/fs/undo-state – current can_undo / can_redo flags
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as w
import io
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from api.state import UndoEntry, state
from mainFunctions import format_bytes

router = APIRouter(prefix="/api/fs", tags=["filesystem"])


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

_file_type_names: Optional[dict] = None


def _type_names() -> dict:
    global _file_type_names
    if _file_type_names is None:
        _file_type_names = state.config.get_file_type_names()
    return _file_type_names


def _describe_type(path: Path) -> str:
    """Return a human-readable type label for a file path."""
    suffix = path.suffix.lower()
    names = _type_names()
    if suffix in names:
        return names[suffix]
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        main_t, sub_t = mime.split("/", 1)
        return f"{sub_t.replace('-', ' ').title()} {main_t}"
    if suffix:
        return f"{suffix[1:].upper()} file"
    return "File"


def _entry(path: Path, analysed_size: Optional[int] = None) -> dict:
    """Build a JSON-safe metadata dict for one filesystem path."""
    try:
        stat = path.stat()
    except OSError as exc:
        return {
            "name": path.name or str(path),
            "path": str(path),
            "is_dir": False,
            "is_file": False,
            "error": str(exc),
        }

    is_dir = path.is_dir()
    size_bytes: Optional[int] = analysed_size if is_dir else stat.st_size

    return {
        "name": path.name or str(path),
        "path": str(path),
        "is_dir": is_dir,
        "is_file": not is_dir,
        "type": "Folder" if is_dir else _describe_type(path),
        "size_bytes": size_bytes,
        "size": format_bytes(size_bytes) if size_bytes is not None else "",
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "modified_ts": stat.st_mtime,
    }


def _undo_redo_flags() -> dict:
    return {"can_undo": state.can_undo, "can_redo": state.can_redo}


# --------------------------------------------------------------------------
# GET /api/fs/list
# --------------------------------------------------------------------------

@router.get("/list")
async def list_folder(path: str = Query(..., description="Absolute folder path")):
    """List the contents of a folder with optional analysed sizes."""
    folder = Path(path)
    if not folder.exists():
        raise HTTPException(404, f"Path not found: {path}")
    if not folder.is_dir():
        raise HTTPException(400, f"Not a directory: {path}")

    # Load previously analysed child-folder sizes from SQLite (fast query).
    child_sizes = state.analysis_store.child_folder_size_map(path)

    entries: list[dict] = []
    files = folders = 0
    try:
        for child in folder.iterdir():
            analysed = child_sizes.get(
                state.analysis_store.normalized_path(child)
            ) if child.is_dir() else None
            entries.append(_entry(child, analysed))
            if child.is_dir():
                folders += 1
            else:
                files += 1
    except PermissionError as exc:
        raise HTTPException(403, str(exc))

    return {
        "path": str(folder),
        "entries": entries,
        "total": len(entries),
        "files": files,
        "folders": folders,
    }


# --------------------------------------------------------------------------
# GET /api/fs/stat
# --------------------------------------------------------------------------

@router.get("/stat")
async def stat_path(path: str = Query(..., description="Absolute path")):
    """Return metadata for one file or folder, with analysis data if available."""
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, f"Path not found: {path}")

    analysed: Optional[int] = None
    analysis_record: Optional[dict] = None
    if p.is_dir():
        summary = state.analysis_store.folder_summary(path)
        if summary:
            analysed = summary["size_bytes"]
            analysis_record = {**summary, "size": format_bytes(summary["size_bytes"])}

    result = _entry(p, analysed)
    result["analysis"] = analysis_record
    return result


# --------------------------------------------------------------------------
# POST /api/fs/open
# --------------------------------------------------------------------------

class OpenRequest(BaseModel):
    path: str


@router.post("/open")
async def open_path(req: OpenRequest):
    """Open a file or folder with the Windows default application."""
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(404, f"Path not found: {req.path}")
    try:
        os.startfile(str(p))           # Windows-only; this is a Windows desktop app
    except OSError as exc:
        raise HTTPException(500, str(exc))
    return {"ok": True}


# --------------------------------------------------------------------------
# POST /api/fs/paste
# --------------------------------------------------------------------------

class PasteRequest(BaseModel):
    sources: list[str]
    destination: str
    move: bool = False


@router.post("/paste")
async def paste_files(req: PasteRequest):
    """Copy or move source paths into a destination folder."""
    if not req.sources:
        raise HTTPException(400, "sources list is empty")
    if not Path(req.destination).is_dir():
        raise HTTPException(400, f"Destination is not a directory: {req.destination}")

    try:
        result = await asyncio.to_thread(
            state.file_operations.paste, req.sources, req.destination, req.move
        )
    except (OSError, Exception) as exc:
        raise HTTPException(500, str(exc))

    if result.get("operations"):
        state.push_undo(UndoEntry(
            action="move" if req.move else "copy",
            operations=result["operations"],
        ))

    return {**result, **_undo_redo_flags()}


# --------------------------------------------------------------------------
# POST /api/fs/delete
# --------------------------------------------------------------------------

class DeleteRequest(BaseModel):
    paths: list[str]


@router.post("/delete")
async def delete_files(req: DeleteRequest):
    """Move paths to the app-trash so the action can be undone."""
    if not req.paths:
        raise HTTPException(400, "paths list is empty")

    try:
        result = await asyncio.to_thread(
            state.file_operations.delete_for_undo, req.paths
        )
    except Exception as exc:
        raise HTTPException(500, str(exc))

    # Remove AI-folder DB records for deleted paths (also undoable).
    ai_records = state.ai_folder_store.delete_records_for_paths(result.get("done", []))
    result["ai_folder_records"] = ai_records

    if result.get("operations"):
        state.push_undo(UndoEntry(
            action="delete",
            operations=result["operations"],
            ai_folder_records=ai_records,
        ))

    return {**result, **_undo_redo_flags()}


# --------------------------------------------------------------------------
# POST /api/fs/undo
# --------------------------------------------------------------------------

@router.post("/undo")
async def undo_operation():
    """Undo the most recent file operation."""
    if not state.undo_stack:
        return {"done": [], "skipped": [], "errors": ["Nothing to undo."],
                **_undo_redo_flags()}

    entry = state.undo_stack.pop()
    try:
        result = await asyncio.to_thread(
            state.file_operations.undo, entry.operations
        )
    except Exception as exc:
        state.undo_stack.append(entry)   # restore on failure
        raise HTTPException(500, str(exc))

    if result.get("errors"):
        state.undo_stack.append(entry)   # partial failure — keep on stack
    else:
        if entry.action == "delete":
            state.ai_folder_store.restore_records(entry.ai_folder_records)
        state.redo_stack.append(entry)

    return {**result, **_undo_redo_flags()}


# --------------------------------------------------------------------------
# POST /api/fs/redo
# --------------------------------------------------------------------------

@router.post("/redo")
async def redo_operation():
    """Redo the most recently undone file operation."""
    if not state.redo_stack:
        return {"done": [], "skipped": [], "errors": ["Nothing to redo."],
                **_undo_redo_flags()}

    entry = state.redo_stack.pop()
    try:
        result = await asyncio.to_thread(
            state.file_operations.redo, entry.operations
        )
    except Exception as exc:
        state.redo_stack.append(entry)
        raise HTTPException(500, str(exc))

    if result.get("errors"):
        state.redo_stack.append(entry)
    else:
        if entry.action == "delete":
            state.ai_folder_store.delete_records_for_paths(
                rec["folder_path"] for rec in entry.ai_folder_records
            )
        state.undo_stack.append(entry)

    return {**result, **_undo_redo_flags()}


# --------------------------------------------------------------------------
# GET /api/fs/undo-state
# --------------------------------------------------------------------------

@router.get("/undo-state")
async def undo_state():
    """Return the current undo / redo availability flags."""
    return _undo_redo_flags()


# --------------------------------------------------------------------------
# GET /api/fs/preview  – text content or binary type info
# --------------------------------------------------------------------------

_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".html", ".htm", ".css", ".xml",
    ".csv", ".log", ".bat", ".cmd", ".sh", ".ps1", ".c", ".cpp", ".cs",
    ".java", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".sql",
    ".r", ".tex", ".rst", ".gitignore", ".env", ".conf",
}
_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tiff",
}
_MAX_PREVIEW_BYTES = 64 * 1024    # 64 KB text limit


@router.get("/preview")
async def preview_file(path: str = Query(..., description="Absolute file path")):
    """Return text content, image marker, or binary metadata for a file."""
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, f"Not found: {path}")
    if p.is_dir():
        raise HTTPException(400, "Use /stat for directories, not /preview")

    size = p.stat().st_size
    suffix = p.suffix.lower()
    mime_type, _ = mimetypes.guess_type(p.name)

    if suffix in _IMAGE_EXTS:
        return {
            "kind": "image",
            "size_bytes": size,
            "mime": mime_type or f"image/{suffix.lstrip('.')}",
        }

    # Attempt text decode for known text exts or small unknown-ext files.
    is_known_text = suffix in _TEXT_EXTS
    is_small_unknown = suffix not in _IMAGE_EXTS and size <= _MAX_PREVIEW_BYTES
    if is_known_text or is_small_unknown:
        try:
            raw = p.read_bytes()
            # Reject as binary if the sample contains null bytes (reliable heuristic).
            sample = raw[:4096]
            if not is_known_text and b"\x00" in sample:
                pass   # fall through to binary response
            else:
                text = raw[:_MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
                return {
                    "kind": "text",
                    "content": text,
                    "truncated": size > _MAX_PREVIEW_BYTES,
                    "size_bytes": size,
                    "encoding": "utf-8",
                }
        except OSError:
            pass

    return {
        "kind": "binary",
        "mime": mime_type or "application/octet-stream",
        "size_bytes": size,
    }


# --------------------------------------------------------------------------
# GET /api/fs/image  – serve an image file inline
# --------------------------------------------------------------------------

@router.get("/image")
async def serve_image(path: str = Query(..., description="Absolute image file path")):
    """Return the image file bytes with the correct Content-Type header."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"Not found: {path}")

    suffix = p.suffix.lower()
    if suffix not in _IMAGE_EXTS:
        raise HTTPException(400, f"Not a recognised image extension: {suffix}")

    mime_type, _ = mimetypes.guess_type(p.name)
    mime_type = mime_type or f"image/{suffix.lstrip('.')}"

    # SVG served as text/xml so browsers render it inline.
    if suffix == ".svg":
        mime_type = "image/svg+xml"

    return FileResponse(
        path=str(p),
        media_type=mime_type,
        headers={"Cache-Control": "public, max-age=60"},
    )


# --------------------------------------------------------------------------
# GET /api/fs/icon  – system file icon as PNG
# --------------------------------------------------------------------------

@router.get("/icon")
async def file_icon(
    path: str = Query(..., description="Absolute file or folder path"),
    size: int = Query(16, ge=16, le=64, description="Icon size in pixels"),
):
    """Return the Windows system icon for *path* as a PNG image."""
    png = await asyncio.to_thread(_extract_file_icon_png, path, size)
    if png is None:
        png = _generic_file_png(size)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


def _extract_file_icon_png(file_path: str, icon_size: int) -> bytes | None:
    """Use ``SHGetFileInfoW`` to get the system icon for any path."""
    try:
        from PIL import Image   # type: ignore[import-untyped]
    except ImportError:
        return None

    p = Path(file_path)
    if not p.exists() and not p.parent.exists():
        return None   # no point trying for nonexistent paths

    shell32 = ctypes.windll.shell32
    user32  = ctypes.windll.user32
    gdi32   = ctypes.windll.gdi32

    SHGFI_ICON        = 0x000000100
    SHGFI_LARGEICON   = 0x000000000   # large icon
    SHGFI_SMALLICON   = 0x000000001   # small icon
    SHGFI_USEFILEATTRIBUTES = 0x000000010

    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [
            ("hIcon",         w.HICON),
            ("iIcon",         w.INT),
            ("dwAttributes",  w.DWORD),
            ("szDisplayName", w.WCHAR * 260),
            ("szTypeName",    w.WCHAR * 80),
        ]

    flags = SHGFI_ICON | SHGFI_LARGEICON
    info = SHFILEINFOW()
    shell32.SHGetFileInfoW(
        str(p), 0, ctypes.byref(info), ctypes.sizeof(info), flags
    )
    hicon = info.hIcon
    if not hicon:
        return None

    try:
        draw_size = icon_size * 2   # supersample
        hdc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        hbmp = gdi32.CreateCompatibleBitmap(hdc, draw_size, draw_size)
        old_bmp = gdi32.SelectObject(mem_dc, hbmp)
        user32.DrawIconEx(mem_dc, 0, 0, hicon, draw_size, draw_size, 0, 0, 3)
        gdi32.SelectObject(mem_dc, old_bmp)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize",          w.DWORD), ("biWidth",         w.LONG),
                ("biHeight",        w.LONG),  ("biPlanes",        w.WORD),
                ("biBitCount",      w.WORD),  ("biCompression",   w.DWORD),
                ("biSizeImage",     w.DWORD), ("biXPelsPerMeter", w.LONG),
                ("biYPelsPerMeter", w.LONG),  ("biClrUsed",       w.DWORD),
                ("biClrImportant",  w.DWORD),
            ]
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(bi)
        bi.biWidth = draw_size
        bi.biHeight = -draw_size
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0

        buf = (w.BYTE * (draw_size * draw_size * 4))()
        gdi32.GetDIBits(mem_dc, hbmp, 0, draw_size, buf, ctypes.byref(bi), 0)

        img = Image.frombuffer("RGBA", (draw_size, draw_size), bytes(buf), "raw", "BGRA", 0, 1)
        img = img.resize((icon_size, icon_size), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    finally:
        user32.DestroyIcon(hicon)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, hdc)


def _generic_file_png(size: int) -> bytes:
    """Return a minimal placeholder PNG for files with no icon."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        margin = max(2, size // 8)
        draw.rounded_rectangle(
            [margin, margin, size - margin, size - margin],
            radius=margin, fill=(180, 180, 190, 255),
        )
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return _MINI_PNG

_MINI_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x06"
    b"\x00\x00\x00\x1f\xf3\xffa\x00\x00\x00\x01sRGB\x00\xae\xce\x1c\xe9"
    b"\x00\x00\x00\nIDATx\xdac\xf8\xcf\xc0\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
