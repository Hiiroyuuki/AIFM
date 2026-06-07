"""
ProjFS (Projected File System) read-only virtual directory provider for AIFM.

Projects the app→file index (app_file_index.py) as a virtual filesystem:

    <virtual_root>/
        <app_name>/         ← directory (from apps_with_files)
            <file_name>     ← file backed by the real file_path on disk

Requirements:
    * Windows 10 version 1809 (build 17763) or later.
    * The "Projected File System" Windows feature must be enabled.
      Check with:  dism /online /get-features | findstr ProjectedFileSystem
      Enable with: dism /online /Enable-Feature /FeatureName:ProjectedFileSystem
    * projectedfslib.dll is in System32 (loaded automatically).

Usage:
    python projfs_provider.py [virtual_root_path]

    Default virtual_root_path is ./aifm_projection (relative to the project root).

This script does NOT depend on FastAPI — it only reads from the SQLite database
written by AppFileIndexStore.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import signal
import sys
import time
from pathlib import Path
from uuid import UUID as _PyUUID


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Windows constants
# ═══════════════════════════════════════════════════════════════════════════════

FILE_ATTRIBUTE_DIRECTORY        = 0x00000010
FILE_ATTRIBUTE_NORMAL           = 0x00000080
FILE_ATTRIBUTE_READONLY         = 0x00000001

S_OK                            = 0x00000000
E_FAIL                          = 0x80004005
E_OUTOFMEMORY                   = 0x8007000E
E_INVALIDARG                    = 0x80070057

PRJ_PLACEHOLDER_INFO_FLAG_DIRECTORY = 0x00000001

# ProjFS notification types (not used for read-only, listed for completeness)
PRJ_NOTIFICATION_NONE           = 0x00000000
PRJ_NOTIFICATION_ALL            = 0xFFFFFFFF


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ctypes structure definitions (Windows ProjFS API, x64 layout)
# ═══════════════════════════════════════════════════════════════════════════════

class GUID(ctypes.Structure):
    """Windows GUID: {Data1-Data2-Data3-Data4[0:2]-Data4[2:8]}."""
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def zero(cls) -> GUID:
        return cls(0, 0, 0, (ctypes.c_ubyte * 8)(*[0] * 8))


class PRJ_FILE_BASIC_INFO(ctypes.Structure):
    """
    Per-file/directory basic metadata passed to PrjFillDirEntryBuffer and
    embedded in PRJ_PLACEHOLDER_INFO.

    sizeof = 56 on x64 (BOOLEAN + 7 pad + 5×INT64 + UINT32 + 4 pad).
    """
    _fields_ = [
        ("IsDirectory",    ctypes.c_bool),     #  0  BOOLEAN (1 byte)
        # ctypes inserts 7 bytes padding      #  1-7
        ("FileSize",       ctypes.c_int64),    #  8
        ("CreationTime",   ctypes.c_int64),    # 16  FILETIME as INT64
        ("LastAccessTime", ctypes.c_int64),    # 24
        ("LastWriteTime",  ctypes.c_int64),    # 32
        ("ChangeTime",     ctypes.c_int64),    # 40
        ("FileAttributes", ctypes.c_uint32),   # 48
        # ctypes inserts 4 bytes padding      # 52-55
    ]                                           # → 56


class PRJ_CALLBACK_DATA(ctypes.Structure):
    """
    Context blob passed to every callback.  Only the fields that this read-only
    provider reads are declared; later Windows versions may append more fields
    (reflected in the Size member), but FilePathName stays at offset 48 on x64
    across all versions released so far (1809, 2004, 21H2, 24H2).
    """
    _fields_ = [
        # --- header ---
        ("Size",                           ctypes.c_uint32),     #  0
        ("Flags",                          ctypes.c_uint32),     #  4
        ("NamespaceVirtualizationContext", ctypes.c_void_p),     #  8  (pointer)
        ("CommandId",                      ctypes.c_int32),      # 16
        ("InstanceId",                     GUID),                # 20  (align 4)
        ("ComponentId",                    ctypes.c_uint32),     # 36
        ("VersionInfo_ProviderId",         ctypes.c_ubyte * 3),  # 40  PRJ_PLACEHOLDER_VERSION_INFO
        # ctypes inserts 5 bytes padding                         # 43-47
        ("FilePathName",                   ctypes.c_wchar_p),    # 48
        ("TriggeringProcessId",            ctypes.c_uint32),     # 56
        # ctypes inserts 4 bytes padding                         # 60-63
        ("TriggeringProcessImageFileName", ctypes.c_wchar_p),    # 64
    ]
    # NOTE: DataStreamId (GUID) starts at offset 72 on Win10 1809; on 2004+
    # it shifts because extra fields were inserted after
    # TriggeringProcessImageFileName.  This provider always passes a
    # zero-filled GUID as the stream id (main data stream), so we never
    # read DataStreamId from this structure.


class PRJ_PLACEHOLDER_INFO(ctypes.Structure):
    """
    Fixed header of the placeholder-info blob written by
    PrjWritePlaceholderInfo.  No variable-length EA / security / streams data
    is appended (all size fields are set to 0).

    sizeof = 96 on x64.
    """
    _fields_ = [
        ("FileBasicInfo",               PRJ_FILE_BASIC_INFO),   #  0  (56 bytes)
        ("EaBufferSize",                ctypes.c_uint32),       # 56
        ("OffsetToFirstEa",             ctypes.c_uint32),       # 60
        ("SecurityBufferSize",          ctypes.c_uint32),       # 64
        ("OffsetToSecurityDescriptor",  ctypes.c_uint32),       # 68
        ("StreamsInfoBufferSize",       ctypes.c_uint32),       # 72
        ("OffsetToFirstStreamInfo",     ctypes.c_uint32),       # 76
        ("Flags",                       ctypes.c_uint32),       # 80  PRJ_PLACEHOLDER_INFO_FLAGS
        # ctypes inserts 4 bytes padding                         # 84-87
        # (next field is INT64, aligned to 8)
        ("FileSize",                    ctypes.c_int64),        # 88  (file size; 0 for dirs)
    ]                                                            # → 96


# ═══════════════════════════════════════════════════════════════════════════════
# 3. projectedfslib.dll — lazy-loaded function signatures
# ═══════════════════════════════════════════════════════════════════════════════

# Module-level handle — set to None until _load_projfs() succeeds.  All
# ProjFS callbacks access this via the module global, so they only work
# inside an active run() session.
_projfs: ctypes.WinDLL | None = None


def _load_projfs() -> ctypes.WinDLL:
    """Load projectedfslib.dll and wire up every function signature.

    Returns the loaded DLL handle, or raises FileNotFoundError with a
    helpful message when the library is missing (ProjFS feature not enabled).
    """
    global _projfs
    if _projfs is not None:
        return _projfs

    try:
        _projfs = ctypes.WinDLL("projectedfslib.dll")
    except FileNotFoundError:
        # Try the full System32 path as a fallback.
        sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "projectedfslib.dll"
        try:
            _projfs = ctypes.WinDLL(str(sys32))
        except FileNotFoundError:
            raise FileNotFoundError(
                "projectedfslib.dll not found.\n"
                "This library is part of Windows 10 1809+.\n"
                "Make sure the 'Projected File System' Windows feature is enabled:\n"
                "  dism /online /get-features | findstr Projected\n"
                "If missing, enable it with:\n"
                "  dism /online /Enable-Feature /FeatureName:ProjectedFileSystem\n"
            ) from None

    # -- DLL exports -----------------------------------------------------------

    _projfs.PrjMarkDirectoryAsPlaceholder.restype = w.HRESULT
    _projfs.PrjMarkDirectoryAsPlaceholder.argtypes = [
        w.LPCWSTR,                              # rootPathName
        w.LPCWSTR,                              # targetPathName (NULL = root itself)
        w.LPCWSTR,                              # versionInfo (NULL)
        ctypes.POINTER(GUID),                   # virtualizationInstanceID
    ]

    _projfs.PrjStartVirtualizing.restype = w.HRESULT
    _projfs.PrjStartVirtualizing.argtypes = [
        w.LPCWSTR,                              # virtualizationRootPath
        ctypes.c_void_p,                        # callbacks (PRJ_CALLBACKS*)
        ctypes.c_void_p,                        # instanceContext (user pointer)
        w.LPCVOID,                              # options (NULL)
        ctypes.POINTER(ctypes.c_void_p),        # namespaceVirtualizationContext (out)
    ]

    _projfs.PrjStopVirtualizing.restype = None
    _projfs.PrjStopVirtualizing.argtypes = [ctypes.c_void_p]

    _projfs.PrjFillDirEntryBuffer.restype = w.HRESULT
    _projfs.PrjFillDirEntryBuffer.argtypes = [
        w.LPCWSTR,                              # fileName
        ctypes.POINTER(PRJ_FILE_BASIC_INFO),    # fileBasicInfo
        ctypes.c_void_p,                        # dirEntryBufferHandle
    ]

    _projfs.PrjWritePlaceholderInfo.restype = w.HRESULT
    _projfs.PrjWritePlaceholderInfo.argtypes = [
        ctypes.c_void_p,                        # namespaceVirtualizationContext
        w.LPCWSTR,                              # destinationFileName
        ctypes.POINTER(PRJ_PLACEHOLDER_INFO),   # placeholderInfo
        ctypes.c_uint32,                        # placeholderInfoSize
    ]

    _projfs.PrjWriteFileData.restype = w.HRESULT
    _projfs.PrjWriteFileData.argtypes = [
        ctypes.c_void_p,                        # namespaceVirtualizationContext
        ctypes.POINTER(GUID),                   # dataStreamId
        ctypes.c_void_p,                        # buffer
        ctypes.c_uint64,                        # byteOffset
        ctypes.c_uint32,                        # length
    ]

    _projfs.PrjFileNameMatch.restype = w.HRESULT
    _projfs.PrjFileNameMatch.argtypes = [
        w.LPCWSTR,                              # fileNameToCheck
        w.LPCWSTR,                              # searchExpression
    ]

    _projfs.PrjAllocateAlignedBuffer.restype = w.HRESULT
    _projfs.PrjAllocateAlignedBuffer.argtypes = [
        ctypes.c_void_p,                        # namespaceVirtualizationContext
        ctypes.c_uint64,                        # size
        ctypes.POINTER(ctypes.c_void_p),        # buffer (out)
    ]

    _projfs.PrjFreeAlignedBuffer.restype = None
    _projfs.PrjFreeAlignedBuffer.argtypes = [
        ctypes.c_void_p,                        # buffer
    ]

    return _projfs


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Callback type definitions  (WINFUNCTYPE / stdcall)
# ═══════════════════════════════════════════════════════════════════════════════

_CB_START_DIR_ENUM = ctypes.WINFUNCTYPE(
    w.HRESULT,
    ctypes.POINTER(PRJ_CALLBACK_DATA),   # callbackData
    ctypes.POINTER(GUID),                # enumerationId
)

_CB_END_DIR_ENUM = ctypes.WINFUNCTYPE(
    None,                                # void
    ctypes.POINTER(PRJ_CALLBACK_DATA),   # callbackData
    ctypes.POINTER(GUID),                # enumerationId
)

_CB_GET_DIR_ENUM = ctypes.WINFUNCTYPE(
    w.HRESULT,
    ctypes.POINTER(PRJ_CALLBACK_DATA),   # callbackData
    ctypes.POINTER(GUID),                # enumerationId
    w.LPCWSTR,                           # searchExpression
    ctypes.c_void_p,                     # dirEntryBufferHandle
)

_CB_GET_PLACEHOLDER_INFO = ctypes.WINFUNCTYPE(
    w.HRESULT,
    ctypes.POINTER(PRJ_CALLBACK_DATA),   # callbackData
)

_CB_GET_FILE_DATA = ctypes.WINFUNCTYPE(
    w.HRESULT,
    ctypes.POINTER(PRJ_CALLBACK_DATA),   # callbackData
    ctypes.c_uint64,                     # byteOffset
    ctypes.c_uint32,                     # length
)


class PRJ_CALLBACKS(ctypes.Structure):
    """
    Passed to PrjStartVirtualizing.  Unused callback slots MUST be NULL.

    We implement the five callbacks required by a minimal read-only provider.
    Notification callbacks are not needed because we never register for them.
    """
    _fields_ = [
        ("StartDirectoryEnumerationCallback",  _CB_START_DIR_ENUM),
        ("EndDirectoryEnumerationCallback",    _CB_END_DIR_ENUM),
        ("CancelCommandCallback",              ctypes.c_void_p),   # NULL (unused)
        ("GetDirectoryEnumerationCallback",    _CB_GET_DIR_ENUM),
        ("GetFileDataCallback",                _CB_GET_FILE_DATA),
        ("GetPlaceholderInfoCallback",         _CB_GET_PLACEHOLDER_INFO),
        ("QueryFileNameCallback",              ctypes.c_void_p),   # NULL (unused)
        ("NotificationCallback",               ctypes.c_void_p),   # NULL (unused)
        # NOTE: Newer SDKs may define more callback slots.  Passing a smaller
        # structure is fine — PrjStartVirtualizing infers the version from
        # the structure size.
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  In-memory cache loaded from AppFileIndexStore
# ═══════════════════════════════════════════════════════════════════════════════

# Schema:
#   _cache: dict[str, dict[str, dict[str, object]]]
#       _cache[app_name] = {
#           file_name: {"file_path": str, "size": int},
#           ...
#       }
#   _app_names: list[str]  — sorted list of app names (for root enumeration)

_cache: dict[str, dict[str, dict[str, object]]] = {}
_app_names: list[str] = []


def load_cache() -> None:
    """Pre-load the full app→file index into memory.

    Called once at startup.  All ProjFS callbacks read exclusively from
    _cache / _app_names — never from the database or disk.
    """
    global _cache, _app_names

    from app_file_index import AppFileIndexStore

    store = AppFileIndexStore()
    apps = store.apps_with_files()       # [{"app_name", "file_count"}, …]

    for entry in apps:
        app_name = entry["app_name"]
        files = store.files_for_app(app_name)  # [{"file_path","file_name","suffix"}, …]
        app_cache: dict[str, dict[str, object]] = {}
        for f in files:
            file_name = f["file_name"]
            file_path = f["file_path"]
            try:
                st_size = os.path.getsize(file_path)
            except OSError:
                st_size = 0
            app_cache[file_name] = {"file_path": file_path, "size": st_size}
        _cache[app_name] = app_cache

    _app_names = sorted(_cache.keys())

    total_files = sum(len(files) for files in _cache.values())
    print(f"[projfs] Cache loaded: {len(_app_names)} apps, {total_files} files")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Helper — resolve a relative path to (app, file) or just (app,)
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_relative(relative_path: str) -> tuple[str | None, str | None]:
    """Parse *relative_path* into (app_name, file_name) or (app_name, None).

    The relative path uses a single backslash as separator, e.g.::

        ""          →  (None, None)          root directory
        "Qt"        →  ("Qt", None)          app directory
        "Qt\\main.cpp" → ("Qt", "main.cpp")  file

    Returns (None, None) if the path does not correspond to a known app / file.
    """
    if not relative_path or relative_path == "\\" or relative_path == ".":
        return (None, None)   # root

    # Strip leading separator just in case.
    stripped = relative_path.lstrip("\\")
    parts = stripped.split("\\", 1)

    app_name = parts[0]
    if app_name not in _cache:
        return (None, None)

    if len(parts) == 1:
        return (app_name, None)   # app directory

    file_name = parts[1]
    if file_name in _cache[app_name]:
        return (app_name, file_name)

    return (None, None)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Callback implementations
# ═══════════════════════════════════════════════════════════════════════════════

# -- 7a  StartDirectoryEnumeration / EndDirectoryEnumeration --------------------

# Session store:  GUID-as-string → set() of already-enumerated names
# Used so that a subsequent GetDirectoryEnumeration call for the same session
# knows where to resume.
_enum_sessions: dict[str, set[str]] = {}


def _guid_str(g: GUID) -> str:
    """Convert a GUID struct to a stable string key."""
    return str(_PyUUID(bytes=bytes((ctypes.c_ubyte * 16).from_address(ctypes.addressof(g)))))


def _start_directory_enumeration(
    callback_data: ctypes.POINTER(PRJ_CALLBACK_DATA),
    enumeration_id: ctypes.POINTER(GUID),
) -> int:
    """Called by ProjFS when it begins enumerating a directory.

    We create a fresh "already-served" set so the next
    GetDirectoryEnumeration callback knows where to resume from.
    """
    try:
        key = _guid_str(enumeration_id.contents)
        _enum_sessions[key] = set()
        return S_OK
    except Exception as exc:
        print(f"[projfs] StartDirectoryEnumeration error: {exc}", file=sys.stderr)
        return E_FAIL


def _end_directory_enumeration(
    callback_data: ctypes.POINTER(PRJ_CALLBACK_DATA),
    enumeration_id: ctypes.POINTER(GUID),
) -> None:
    """Called by ProjFS when enumeration is complete (or cancelled).

    We remove the session state to free memory.
    """
    try:
        key = _guid_str(enumeration_id.contents)
        _enum_sessions.pop(key, None)
    except Exception:
        pass


# -- 7b  GetDirectoryEnumeration -----------------------------------------------

def _get_directory_enumeration(
    callback_data: ctypes.POINTER(PRJ_CALLBACK_DATA),
    enumeration_id: ctypes.POINTER(GUID),
    search_expression: ctypes.c_wchar_p,
    dir_entry_buffer_handle: ctypes.c_void_p,
) -> int:
    """Called by ProjFS to fill one or more directory entries.

    ProjFS may call this repeatedly for the same enumeration session until
    the provider stops filling entries.

    Behaviour depends on the relative path in callback_data->FilePathName:
      - root (empty):     return app names as directory entries
      - <app_name>:       return file names as file entries
      - <app_name>\\file:  not a directory — return immediately (no entries)
    """
    try:
        cd = callback_data.contents
        rel = cd.FilePathName or ""

        # Strip leading backslash for consistent comparison.
        if rel and rel[0] == "\\":
            rel = rel[1:]

        app_name, _file_name = _resolve_relative(rel)

        # Build the list of candidate entry names for this directory.
        candidates: list[str] = []
        is_dir_entry = False

        if app_name is None and _file_name is None:
            # Root directory — list app names (directories)
            candidates = list(_app_names)
            is_dir_entry = True
        elif app_name is not None and _file_name is None and app_name in _cache:
            # App directory — list file names (files)
            candidates = list(_cache[app_name].keys())
            is_dir_entry = False
        else:
            # Not a directory we know, or it's a file — no entries.
            return S_OK

        # Filter by search expression if provided and not wildcard-only.
        search_str = search_expression.value if search_expression.value else None
        if search_str and search_str not in ("*", "*.*"):
            filtered: list[str] = []
            for name in candidates:
                hr = _projfs.PrjFileNameMatch(name, search_str)
                if hr == S_OK:
                    filtered.append(name)
            candidates = filtered

        # Only fill names we haven't already served in this session.
        key = _guid_str(enumeration_id.contents)
        served = _enum_sessions.get(key)
        if served is None:
            # Session was already ended; nothing to do.
            return S_OK

        # Find the first not-yet-served entry and fill the buffer with it.
        for name in candidates:
            if name in served:
                continue
            served.add(name)

            basic_info = PRJ_FILE_BASIC_INFO()
            if is_dir_entry:
                # Directory entry
                basic_info.IsDirectory = True
                basic_info.FileSize = 0
                basic_info.FileAttributes = FILE_ATTRIBUTE_DIRECTORY
            else:
                # File entry — use cached size
                basic_info.IsDirectory = False
                basic_info.FileSize = _cache[app_name][name]["size"]
                basic_info.FileAttributes = FILE_ATTRIBUTE_NORMAL

            hr = _projfs.PrjFillDirEntryBuffer(
                name, ctypes.byref(basic_info), dir_entry_buffer_handle,
            )
            if hr != S_OK:
                # Buffer full — ProjFS will call us again.
                # Remove from served so we retry next call.
                served.discard(name)
                break

        return S_OK
    except Exception as exc:
        print(f"[projfs] GetDirectoryEnumeration error: {exc}", file=sys.stderr)
        return E_FAIL


# -- 7c  GetPlaceholderInfo ----------------------------------------------------

def _get_placeholder_info(
    callback_data: ctypes.POINTER(PRJ_CALLBACK_DATA),
) -> int:
    """Called by ProjFS when it needs metadata for a placeholder (file or dir).

    We write a PRJ_PLACEHOLDER_INFO blob via PrjWritePlaceholderInfo.
    For directories we set the directory flag; for files we report the
    real file size so the shell can show correct sizes.
    """
    try:
        cd = callback_data.contents
        rel = cd.FilePathName or ""
        if rel and rel[0] == "\\":
            rel = rel[1:]

        app_name, file_name = _resolve_relative(rel)
        nvc = cd.NamespaceVirtualizationContext

        info = PRJ_PLACEHOLDER_INFO()

        if app_name is not None and file_name is None and app_name in _cache:
            # → directory
            info.Flags = PRJ_PLACEHOLDER_INFO_FLAG_DIRECTORY
            info.FileBasicInfo.IsDirectory = True
            info.FileBasicInfo.FileSize = 0
            info.FileBasicInfo.FileAttributes = FILE_ATTRIBUTE_DIRECTORY
            info.FileSize = 0
        elif app_name is not None and file_name is not None and file_name in _cache.get(app_name, {}):
            # → file
            cached = _cache[app_name][file_name]
            info.Flags = 0   # file (not directory)
            info.FileBasicInfo.IsDirectory = False
            info.FileBasicInfo.FileSize = cached["size"]
            info.FileBasicInfo.FileAttributes = FILE_ATTRIBUTE_NORMAL
            info.FileSize = cached["size"]
        elif app_name is None and file_name is None:
            # → root directory
            info.Flags = PRJ_PLACEHOLDER_INFO_FLAG_DIRECTORY
            info.FileBasicInfo.IsDirectory = True
            info.FileBasicInfo.FileSize = 0
            info.FileBasicInfo.FileAttributes = FILE_ATTRIBUTE_DIRECTORY
            info.FileSize = 0
        else:
            # Unknown path — cannot provide info.
            return E_FAIL

        hr = _projfs.PrjWritePlaceholderInfo(
            nvc, rel, ctypes.byref(info), ctypes.sizeof(PRJ_PLACEHOLDER_INFO),
        )
        return hr
    except Exception as exc:
        print(f"[projfs] GetPlaceholderInfo error: {exc}", file=sys.stderr)
        return E_FAIL


# -- 7d  GetFileData -----------------------------------------------------------

def _get_file_data(
    callback_data: ctypes.POINTER(PRJ_CALLBACK_DATA),
    byte_offset: int,
    length: int,
) -> int:
    """Called by ProjFS when the system wants file content for [offset, length].

    We map the relative path to a real file, read the requested byte range,
    allocate an aligned buffer, fill it, and hand it to PrjWriteFileData.
    """
    try:
        cd = callback_data.contents
        rel = cd.FilePathName or ""
        if rel and rel[0] == "\\":
            rel = rel[1:]

        app_name, file_name = _resolve_relative(rel)
        if app_name is None or file_name is None:
            return E_INVALIDARG

        file_info = _cache.get(app_name, {}).get(file_name)
        if file_info is None:
            return E_INVALIDARG

        real_path = file_info["file_path"]
        nvc = cd.NamespaceVirtualizationContext

        # Read the requested byte range from the real file.
        try:
            with open(real_path, "rb") as fh:
                fh.seek(byte_offset)
                data = fh.read(length)
        except OSError as exc:
            print(f"[projfs] Read error for {real_path!r}: {exc}", file=sys.stderr)
            return E_FAIL

        if not data:
            # EOF — nothing to write, success.
            return S_OK

        data_len = len(data)

        # Allocate an aligned buffer and copy data into it.
        buf = ctypes.c_void_p()
        hr = _projfs.PrjAllocateAlignedBuffer(nvc, data_len, ctypes.byref(buf))
        if hr != S_OK or not buf.value:
            print(f"[projfs] PrjAllocateAlignedBuffer failed: 0x{hr:08X}", file=sys.stderr)
            return hr

        try:
            # memmove takes an integer address, not a c_void_p object.
            ctypes.memmove(buf.value, data, data_len)

            # Always pass a zero-filled GUID for the main data stream.
            stream_id = GUID.zero()
            hr = _projfs.PrjWriteFileData(
                nvc,
                ctypes.byref(stream_id),
                buf,
                byte_offset,
                data_len,
            )
            return hr
        finally:
            _projfs.PrjFreeAlignedBuffer(buf)

    except Exception as exc:
        print(f"[projfs] GetFileData error: {exc}", file=sys.stderr)
        return E_FAIL


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Global references — prevent GC of WINFUNCTYPE callbacks
# ═══════════════════════════════════════════════════════════════════════════════

_g_start_enum:    _CB_START_DIR_ENUM | None = None
_g_end_enum:      _CB_END_DIR_ENUM | None = None
_g_get_dir_enum:  _CB_GET_DIR_ENUM | None = None
_g_get_ph_info:   _CB_GET_PLACEHOLDER_INFO | None = None
_g_get_file_data: _CB_GET_FILE_DATA | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Startup / shutdown
# ═══════════════════════════════════════════════════════════════════════════════

def _create_callbacks() -> PRJ_CALLBACKS:
    """Create and pin the PRJ_CALLBACKS structure + all five callbacks."""
    global _g_start_enum, _g_end_enum, _g_get_dir_enum
    global _g_get_ph_info, _g_get_file_data

    _g_start_enum    = _CB_START_DIR_ENUM(_start_directory_enumeration)
    _g_end_enum      = _CB_END_DIR_ENUM(_end_directory_enumeration)
    _g_get_dir_enum  = _CB_GET_DIR_ENUM(_get_directory_enumeration)
    _g_get_ph_info   = _CB_GET_PLACEHOLDER_INFO(_get_placeholder_info)
    _g_get_file_data = _CB_GET_FILE_DATA(_get_file_data)

    cb = PRJ_CALLBACKS()
    cb.StartDirectoryEnumerationCallback  = _g_start_enum    # type: ignore[assignment]
    cb.EndDirectoryEnumerationCallback    = _g_end_enum      # type: ignore[assignment]
    cb.CancelCommandCallback              = 0                 # NULL
    cb.GetDirectoryEnumerationCallback    = _g_get_dir_enum   # type: ignore[assignment]
    cb.GetFileDataCallback                = _g_get_file_data  # type: ignore[assignment]
    cb.GetPlaceholderInfoCallback         = _g_get_ph_info    # type: ignore[assignment]
    cb.QueryFileNameCallback              = 0                 # NULL
    cb.NotificationCallback               = 0                 # NULL
    return cb


def run(virtual_root: Path) -> None:
    """Start the ProjFS provider and block until Ctrl+C."""

    # --- load projectedfslib.dll (lazy — fails with clear message if missing) ---
    try:
        _load_projfs()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- load cache ---
    load_cache()
    if not _app_names:
        print("[projfs] WARNING: Cache is empty.  The virtual directory will be empty.\n"
              "         Run app_file_index.py to build the index first.")

    # --- ensure virtual root exists ---
    virtual_root.mkdir(parents=True, exist_ok=True)

    # --- mark as ProjFS placeholder ---
    # This is idempotent; if already marked the call returns an error we ignore.
    hr = _projfs.PrjMarkDirectoryAsPlaceholder(
        str(virtual_root),
        None,                 # targetPathName — NULL means the root itself
        None,                 # versionInfo — NULL
        None,                 # virtualizationInstanceID — NULL (not needed)
    )
    if hr != S_OK:
        print(f"[projfs] PrjMarkDirectoryAsPlaceholder returned 0x{hr:08X} "
              f"(may already be marked — continuing)")

    # --- start virtualizing ---
    cbs = _create_callbacks()
    nvc = ctypes.c_void_p()
    hr = _projfs.PrjStartVirtualizing(
        str(virtual_root),
        ctypes.byref(cbs),
        None,                 # instanceContext — no extra context needed
        None,                 # options — NULL = defaults
        ctypes.byref(nvc),    # ← receives the virtualization context handle
    )
    if hr != S_OK:
        print(f"ERROR: PrjStartVirtualizing failed: 0x{hr:08X}", file=sys.stderr)
        sys.exit(1)

    print(f"[projfs] Virtualization active at: {virtual_root}")
    print("[projfs] Press Ctrl+C to stop …")

    # --- block until Ctrl+C, then shut down ---
    _running = True

    def _on_signal(sig, frame):
        nonlocal _running
        _running = False

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGBREAK, _on_signal)   # Ctrl+Break on Windows

    try:
        while _running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[projfs] Stopping virtualisation …")
        _projfs.PrjStopVirtualizing(nvc)
        print("[projfs] Stopped.")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    default_root = Path(__file__).resolve().parent / "aifm_projection"
    root_arg = sys.argv[1] if len(sys.argv) > 1 else str(default_root)
    run(Path(root_arg))
