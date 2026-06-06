"""Installed applications endpoints.

GET  /api/apps                    – list installed apps (cached)
GET  /api/apps/{name}/icon-path   – resolved icon path for one app
GET  /api/apps/icon?path=         – extract icon from .exe/.ico as PNG
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as w
import io
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from starlette.responses import Response

from api.state import state

router = APIRouter(prefix="/api/apps", tags=["apps"])


def _scan_apps() -> list[dict]:
    """Run the full registry scan and return serialisable app dicts.

    Each dict includes an ``is_system`` boolean so that ``filter_system`` can
    be applied without re-scanning the registry.
    """
    from installed_apps import (
        get_installed_apps,
        is_system_component,
        resolve_icon_path,
    )

    raw = get_installed_apps()
    result: list[dict] = []
    for app in raw:
        result.append({
            **app.to_dict(),
            "exists": app.exists,
            "is_system": is_system_component(app),
            "icon_path": resolve_icon_path(app),
        })
    return result


async def _get_cached_apps() -> list[dict]:
    """Return the cached app list, filling it on first access."""
    if state._apps_cache is None:
        state._apps_cache = await asyncio.to_thread(_scan_apps)
    return state._apps_cache


# ── GET /api/apps ────────────────────────────────────────────────────────────

@router.get("")
async def list_apps(
    filter_system: bool = Query(True, description="Hide system components"),
    q: Optional[str] = Query(None, description="Case-insensitive name search"),
):
    """Return installed applications, optionally filtered."""
    apps = await _get_cached_apps()

    if filter_system:
        apps = [a for a in apps if not a.get("is_system", False)]

    if q:
        needle = q.strip().lower()
        apps = [a for a in apps if needle in a["name"].lower()]

    return apps


# ── GET /api/apps/{name}/icon-path ──────────────────────────────────────────

@router.get("/{name}/icon-path")
async def get_icon_path(name: str):
    """Return the resolved icon path for a single installed application."""
    apps = await _get_cached_apps()
    needle = name.strip().lower()
    for app in apps:
        if app["name"].lower() == needle:
            return {"name": app["name"], "icon_path": app.get("icon_path")}
    raise HTTPException(404, f"App not found: {name}")


# ── Icon PNG extraction ─────────────────────────────────────────────────────

def _extract_icon_png(file_path: str, icon_size: int = 48) -> bytes | None:
    """Return PNG bytes of the first icon in an .exe/.ico/.dll file.

    For .ico files uses PIL (which picks the highest-resolution frame).
    For .exe/.dll extracts the icon with ``ExtractIconExW``, draws it at 2×
    the requested size with GDI, then downscales with LANCZOS — this
    supersampling gives crisp results even when the source icon is only
    32×32.
    """
    try:
        from PIL import Image   # type: ignore[import-untyped]
    except ImportError:
        return None

    fp = Path(file_path)
    if not fp.is_file():
        return None

    # .ico — PIL picks the best frame automatically.
    if fp.suffix.lower() == ".ico":
        try:
            img = Image.open(fp)
            img = img.resize((icon_size, icon_size), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return None

    # .exe / .dll — ExtractIconExW → GDI draw at 2× → LANCZOS downscale.
    shell32 = ctypes.windll.shell32
    user32  = ctypes.windll.user32
    gdi32   = ctypes.windll.gdi32

    h_large = w.HICON()
    h_small = w.HICON()
    count = shell32.ExtractIconExW(str(fp), 0, ctypes.byref(h_large), ctypes.byref(h_small), 1)
    hicon = h_large.value or h_small.value
    if not hicon or count <= 0:
        idx = ctypes.c_int()
        hicon = shell32.ExtractAssociatedIconW(0, str(fp), ctypes.byref(idx))
    if not hicon:
        return None

    try:
        # Draw at 2× the target size for crisp downscaling.
        draw_size = icon_size * 2

        hdc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        hbmp = gdi32.CreateCompatibleBitmap(hdc, draw_size, draw_size)
        old_bmp = gdi32.SelectObject(mem_dc, hbmp)
        user32.DrawIconEx(mem_dc, 0, 0, hicon, draw_size, draw_size, 0, 0, 3)
        gdi32.SelectObject(mem_dc, old_bmp)

        # Read bitmap pixels.
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize",          w.DWORD),  ("biWidth",         w.LONG),
                ("biHeight",        w.LONG),   ("biPlanes",        w.WORD),
                ("biBitCount",      w.WORD),   ("biCompression",   w.DWORD),
                ("biSizeImage",     w.DWORD),  ("biXPelsPerMeter", w.LONG),
                ("biYPelsPerMeter", w.LONG),   ("biClrUsed",       w.DWORD),
                ("biClrImportant",  w.DWORD),
            ]
        bi = BITMAPINFOHEADER()
        bi.biSize        = ctypes.sizeof(bi)
        bi.biWidth       = draw_size
        bi.biHeight      = -draw_size
        bi.biPlanes      = 1
        bi.biBitCount    = 32
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


# ── GET /api/apps/icon ─────────────────────────────────────────────────────

_GENERIC_PNG: bytes | None = None


def _placeholder_png() -> bytes:
    """Return a 48×48 grey placeholder icon as PNG bytes."""
    global _GENERIC_PNG
    if _GENERIC_PNG is not None:
        return _GENERIC_PNG
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([4, 4, 44, 44], radius=6, fill=(200, 200, 210, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        _GENERIC_PNG = buf.getvalue()
    except Exception:
        _GENERIC_PNG = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x000\x00\x00\x000\x08\x06"
            b"\x00\x00\x00W\x02\xf9\x87\x00\x00\x00\x01sRGB\x00\xae\xce\x1c\xe9"
            b"\x00\x00\x00\nIDATx\xdac\xf8\xcf\xc0\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )   # minimal 48×48 grey PNG
    return _GENERIC_PNG


@router.get("/icon")
async def get_app_icon(
    path: str = Query(..., description="Path to .exe/.ico/.dll"),
    size: int = Query(48, ge=16, le=256, description="Icon size in pixels"),
):
    """Return the extracted icon as a PNG image."""
    png = await asyncio.to_thread(_extract_icon_png, path, size)
    if png is None:
        png = _placeholder_png()
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ── POST /api/apps/launch ──────────────────────────────────────────────────

class LaunchRequest(BaseModel):
    name: str

@router.post("/launch")
async def launch_app(req: LaunchRequest):
    """Launch an installed application by name."""
    apps = await _get_cached_apps()
    needle = req.name.strip().lower()
    match = None
    for a in apps:
        if a["name"].lower() == needle:
            match = a
            break
    if match is None:
        raise HTTPException(404, f"App not found: {req.name}")

    exe_path = await asyncio.to_thread(_resolve_launch_target, match)
    if exe_path is None:
        raise HTTPException(400, f"Could not find executable for: {req.name}")

    import subprocess
    try:
        subprocess.Popen([exe_path], shell=True)
    except OSError as exc:
        raise HTTPException(500, f"Failed to launch: {exc}")

    return {"ok": True, "name": match["name"], "launched": exe_path}


def _resolve_launch_target(app: dict) -> str | None:
    """Find the best executable to launch for an installed application."""
    import os
    loc = app.get("install_location") or ""
    uninst = app.get("uninstall_string") or ""
    icon_path = app.get("icon_path") or ""

    # Priority 1: icon_path if it points to an exe
    if icon_path and os.path.isfile(icon_path) and icon_path.lower().endswith(".exe"):
        return icon_path

    # Priority 2: scan install_location for an exe
    if loc and os.path.isdir(loc):
        try:
            for entry in os.scandir(loc):
                if entry.is_file() and entry.name.lower().endswith(".exe"):
                    return entry.path
        except OSError:
            pass

    # Priority 3: parse the uninstall_string for a quoted exe path
    import re
    m = re.search(r'"([^"]+\.exe)"', uninst)
    if m and os.path.isfile(m.group(1)):
        return m.group(1)

    # Priority 4: any exe in the same directory as the uninstaller
    m = re.search(r'"([^"]+)"', uninst)
    if m:
        uninst_parent = os.path.dirname(m.group(1))
        if os.path.isdir(uninst_parent):
            try:
                for entry in os.scandir(uninst_parent):
                    if entry.is_file() and entry.name.lower().endswith(".exe"):
                        return entry.path
            except OSError:
                pass

    return None
