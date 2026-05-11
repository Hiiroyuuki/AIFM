"""Core non-UI functions used by the file manager frontend."""

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from config_loader import Config

config = Config()
CONFIG = config.get_config()

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ANALYSIS_DB = config.get_default_analysis_db()
DEFAULT_AI_FOLDER_DB = CONFIG.get("default_ai_folder_db", "ai_folders.sqlite3")
DEFAULT_AI_FOLDER_ICON = Path(CONFIG.get("ai_folder_icon_path", BASE_DIR / "AIFM.ico"))
EVERYTHING_RESULT_LIMIT = config.get_everything_limit()
EVERYTHING_SDK_DLL_PATH = BASE_DIR / "Everything-SDK" / "dll" / "Everything64.dll"
EVERYTHING_START_TIMEOUT_SECONDS = config.get_everything_timeout()
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


class FileOperationService:
    """
    Performs filesystem mutations for the browser.

    The service keeps UI code away from copy, move, delete, undo, redo, and
    temporary trash details. Methods return simple result dictionaries so the
    frontend can decide how to present success, skip, and error messages.
    """

    def paste(self, source_paths, destination_path, move=False):
        """Copy or move paths into a destination folder."""
        destination = Path(destination_path)
        if not destination.is_dir():
            raise NotADirectoryError(str(destination))

        result = {
            "done": [],
            "skipped": [],
            "errors": [],
            "operations": [],
        }

        for source_text in source_paths:
            source = Path(source_text)
            try:
                target = self.target_path(source, destination, move)
                if target is None:
                    result["skipped"].append(str(source))
                    continue

                if move:
                    shutil.move(str(source), str(target))
                elif source.is_dir():
                    shutil.copytree(source, target, copy_function=shutil.copy2)
                else:
                    shutil.copy2(source, target)

                result["done"].append(str(target))
                result["operations"].append(
                    {
                        "type": "move" if move else "copy",
                        "source": str(source.resolve(strict=False)),
                        "target": str(target.resolve(strict=False)),
                    }
                )
            except (OSError, shutil.Error) as error:
                result["errors"].append(f"{source}: {error}")

        return result

    def delete_for_undo(self, source_paths):
        """Move paths into the app trash and return operations for undo."""
        result = {
            "done": [],
            "skipped": [],
            "errors": [],
            "operations": [],
        }

        for source_text in source_paths:
            source = Path(source_text)
            try:
                if not source.exists():
                    result["skipped"].append(str(source))
                    continue

                trash = self.trash_path(source)
                self.move_existing(source, trash)
                result["done"].append(str(source))
                result["operations"].append(
                    {
                        "type": "delete",
                        "source": str(source.resolve(strict=False)),
                        "trash": str(trash.resolve(strict=False)),
                    }
                )
            except (OSError, shutil.Error) as error:
                result["errors"].append(f"{source}: {error}")

        return result

    def delete(self, source_paths, recycle=True):
        """Delete paths directly or through the Windows recycle bin."""
        result = {
            "done": [],
            "skipped": [],
            "errors": [],
        }

        for source_text in source_paths:
            source = Path(source_text)
            try:
                if not source.exists():
                    result["skipped"].append(str(source))
                    continue

                if recycle and os.name == "nt":
                    self.move_to_recycle_bin(source)
                elif source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()

                result["done"].append(str(source))
            except OSError as error:
                result["errors"].append(f"{source}: {error}")

        return result

    def undo(self, operations):
        """Undo a recorded batch of file operations."""
        result = {
            "done": [],
            "skipped": [],
            "errors": [],
        }

        for operation in reversed(operations):
            try:
                self.undo_one(operation)
                result["done"].append(self.operation_label(operation))
            except (OSError, shutil.Error) as error:
                result["errors"].append(f"{self.operation_label(operation)}: {error}")

        return result

    def redo(self, operations):
        """Redo a recorded batch of file operations."""
        result = {
            "done": [],
            "skipped": [],
            "errors": [],
        }

        for operation in operations:
            try:
                self.redo_one(operation)
                result["done"].append(self.operation_label(operation))
            except (OSError, shutil.Error) as error:
                result["errors"].append(f"{self.operation_label(operation)}: {error}")

        return result

    def undo_one(self, operation):
        """Undo one copy, move, or delete operation."""
        operation_type = operation["type"]
        if operation_type == "copy":
            target = Path(operation["target"])
            trash = self.trash_path(target)
            self.move_existing(target, trash)
            operation["trash"] = str(trash.resolve(strict=False))
            return

        if operation_type == "move":
            self.move_existing(Path(operation["target"]), Path(operation["source"]))
            return

        if operation_type == "delete":
            self.move_existing(Path(operation["trash"]), Path(operation["source"]))

    def redo_one(self, operation):
        """Redo one copy, move, or delete operation."""
        operation_type = operation["type"]
        if operation_type == "copy":
            self.move_existing(Path(operation["trash"]), Path(operation["target"]))
            return

        if operation_type == "move":
            self.move_existing(Path(operation["source"]), Path(operation["target"]))
            return

        if operation_type == "delete":
            source = Path(operation["source"])
            trash = Path(operation.get("trash") or self.trash_path(source))
            self.move_existing(source, trash)
            operation["trash"] = str(trash.resolve(strict=False))

    def move_existing(self, source, target):
        """Move an existing path to a target that does not already exist."""
        if not source.exists():
            raise FileNotFoundError(str(source))
        if target.exists():
            raise FileExistsError(str(target))

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def trash_path(self, source):
        """Return a unique app-trash path for a source item."""
        trash_root = BASE_DIR / ".aifm_trash"
        trash_root.mkdir(parents=True, exist_ok=True)
        return trash_root / f"{uuid.uuid4().hex}_{source.name}"

    def clear_trash(self):
        """Remove the app-trash folder on application exit."""
        trash_root = BASE_DIR / ".aifm_trash"
        if not trash_root.exists():
            return ""

        try:
            self.prepare_for_delete(trash_root)
            shutil.rmtree(trash_root, onerror=self.handle_rmtree_error)
        except OSError as error:
            return str(error)

        return ""

    def prepare_for_delete(self, root):
        """Reset Windows attributes before recursively deleting a tree."""
        if os.name != "nt":
            return

        paths = list(Path(root).rglob("*"))
        paths.append(Path(root))
        for path in paths:
            self.set_windows_attributes(path, FILE_ATTRIBUTE_NORMAL, replace=True)

    def handle_rmtree_error(self, function, path, _exc_info):
        """Retry rmtree operations after clearing Windows file attributes."""
        self.set_windows_attributes(path, FILE_ATTRIBUTE_NORMAL, replace=True)
        function(path)

    @staticmethod
    def set_windows_attributes(path, attributes, replace=False):
        """Set or merge Windows file attributes for a path."""
        if os.name != "nt":
            return

        path_text = str(Path(path))
        kernel32 = ctypes.windll.kernel32
        if replace:
            kernel32.SetFileAttributesW(path_text, attributes)
            return

        current = kernel32.GetFileAttributesW(path_text)
        if current == INVALID_FILE_ATTRIBUTES:
            return

        kernel32.SetFileAttributesW(path_text, current | attributes)

    @staticmethod
    def operation_label(operation):
        """Return a readable path label for an undo/redo operation."""
        return operation.get("target") or operation.get("source") or operation.get("trash", "")

    def move_to_recycle_bin(self, source):
        """Move one path to the Windows recycle bin."""
        class SHFILEOPSTRUCTW(ctypes.Structure):
            """ctypes structure required by SHFileOperationW."""

            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        operation = SHFILEOPSTRUCTW()
        operation.wFunc = 3
        operation.pFrom = str(source) + "\0\0"
        operation.pTo = None
        operation.fFlags = 0x0040 | 0x0010 | 0x0004

        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if result:
            raise OSError(result, "Failed to move item to Recycle Bin")
        if operation.fAnyOperationsAborted:
            raise OSError("Recycle Bin operation was cancelled")

    def target_path(self, source, destination, move):
        """Return the destination path for paste, including copy-name handling."""
        if not source.exists():
            raise FileNotFoundError(str(source))

        if move and self.same_path(source.parent, destination):
            return None

        if source.is_dir() and self.path_is_inside(destination, source):
            raise OSError("Cannot paste a folder into itself.")

        target = destination / source.name
        if not target.exists():
            return target

        return self.unique_copy_path(source, destination)

    def unique_copy_path(self, source, destination):
        """Return a non-existing copy target path in the destination folder."""
        if source.is_dir():
            first_name = f"{source.name} - Copy"
            next_name = lambda index: f"{source.name} - Copy ({index})"
        else:
            first_name = f"{source.stem} - Copy{source.suffix}"
            next_name = lambda index: f"{source.stem} - Copy ({index}){source.suffix}"

        target = destination / first_name
        if not target.exists():
            return target

        index = 2
        while True:
            target = destination / next_name(index)
            if not target.exists():
                return target
            index += 1

    @staticmethod
    def same_path(left, right):
        """Compare two filesystem paths with platform normalization."""
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )

    @staticmethod
    def path_is_inside(child, parent):
        """Return whether child resolves under parent."""
        try:
            Path(child).resolve(strict=False).relative_to(
                Path(parent).resolve(strict=False)
            )
            return True
        except ValueError:
            return False


class EverythingSdkSearch:
    """
    Thin wrapper around Everything SDK search.

    The frontend asks this class for search result paths and total result count.
    This class owns DLL loading, optional Everything startup, ctypes signatures,
    and conversion from SDK errors into user-facing messages.
    """

    ERROR_MESSAGES = {
        1: "Memory allocation failed.",
        2: "Everything IPC is unavailable. Please make sure Everything is running.",
        3: "Unable to register Everything SDK window class.",
        4: "Unable to create Everything SDK window.",
        5: "Unable to create Everything SDK thread.",
        6: "Invalid search call.",
        7: "Invalid index.",
        8: "Invalid Everything call.",
    }

    REQUEST_FILE_NAME = 0x00000001
    REQUEST_PATH = 0x00000002
    SORT_PATH_ASCENDING = 3

    def __init__(self, result_limit=EVERYTHING_RESULT_LIMIT):
        """Load the SDK and configure the maximum displayed result count."""
        self.result_limit = result_limit
        self.dll_path = EVERYTHING_SDK_DLL_PATH
        self.core_exe_path = self.find_core_executable()
        self.dll = self.load_dll()

    def start(self):
        """Ensure the SDK can talk to an Everything process."""
        if not self.dll:
            return f"Everything SDK DLL was not found: {self.dll_path}"

        return self.ensure_running()

    def find_core_executable(self):
        """Find an installed or bundled Everything executable."""
        candidates = [
            shutil.which("Everything.exe"),
            BASE_DIR / "Everything.exe",
            BASE_DIR / "Everything" / "Everything.exe",
            Path("C:/Program Files/Everything/Everything.exe"),
            Path("C:/Program Files (x86)/Everything/Everything.exe"),
        ]

        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)

        return None

    def ensure_running(self):
        """Start Everything when needed and wait briefly for its database."""
        if self.is_ready():
            return ""

        if not self.core_exe_path:
            return (
                "Everything is not running and Everything.exe was not found. "
                "Place Everything.exe in the project root or install Everything."
            )

        try:
            subprocess.Popen(
                [self.core_exe_path, "-startup"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            return f"Failed to start Everything: {error}"

        deadline = time.monotonic() + EVERYTHING_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.is_ready():
                return ""
            time.sleep(0.25)

        return "Everything was started, but its database is still loading."

    def is_ready(self):
        """Return whether the SDK reports a loaded Everything database."""
        try:
            if hasattr(self.dll, "Everything_IsDBLoaded") and self.dll.Everything_IsDBLoaded():
                return True
        except OSError:
            return False

        return False

    def search(self, query):
        """Run an Everything query and return result paths plus a message."""
        if not self.dll:
            return [], f"Everything SDK DLL was not found: {self.dll_path}"

        self.dll.Everything_Reset()
        self.dll.Everything_SetSearchW(query)
        if self.result_limit > 0:
            self.dll.Everything_SetMax(self.result_limit)
        self.dll.Everything_SetRequestFlags(self.REQUEST_FILE_NAME | self.REQUEST_PATH)
        self.dll.Everything_SetSort(self.SORT_PATH_ASCENDING)

        if not self.dll.Everything_QueryW(True):
            if self.last_error_code() == 2:
                message = self.ensure_running()
                if not message and self.dll.Everything_QueryW(True):
                    return self.collect_paths(), ""
            return [], self.last_error_message()

        return self.collect_paths(), ""

    def load_dll(self):
        """Load the Everything SDK DLL and bind the used functions."""
        if not self.dll_path.exists():
            return None

        try:
            dll = ctypes.WinDLL(str(self.dll_path))
        except OSError:
            return None

        self.bind_functions(dll)
        return dll

    def bind_functions(self, dll):
        """Declare ctypes signatures for Everything SDK functions."""
        dll.Everything_Reset.argtypes = []
        dll.Everything_Reset.restype = None

        dll.Everything_SetSearchW.argtypes = [wintypes.LPCWSTR]
        dll.Everything_SetSearchW.restype = None

        dll.Everything_SetMax.argtypes = [wintypes.DWORD]
        dll.Everything_SetMax.restype = None

        dll.Everything_SetRequestFlags.argtypes = [wintypes.DWORD]
        dll.Everything_SetRequestFlags.restype = None

        dll.Everything_SetSort.argtypes = [wintypes.DWORD]
        dll.Everything_SetSort.restype = None

        dll.Everything_QueryW.argtypes = [wintypes.BOOL]
        dll.Everything_QueryW.restype = wintypes.BOOL

        dll.Everything_GetLastError.argtypes = []
        dll.Everything_GetLastError.restype = wintypes.DWORD

        dll.Everything_GetNumResults.argtypes = []
        dll.Everything_GetNumResults.restype = wintypes.DWORD

        dll.Everything_GetTotResults.argtypes = []
        dll.Everything_GetTotResults.restype = wintypes.DWORD

        dll.Everything_GetResultFullPathNameW.argtypes = [
            wintypes.DWORD,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        dll.Everything_GetResultFullPathNameW.restype = wintypes.DWORD

        if hasattr(dll, "Everything_IsDBLoaded"):
            dll.Everything_IsDBLoaded.argtypes = []
            dll.Everything_IsDBLoaded.restype = wintypes.BOOL

    def collect_paths(self):
        """Collect full paths from the most recent SDK query."""
        paths = []
        count = self.dll.Everything_GetNumResults()

        for index in range(count):
            buffer = ctypes.create_unicode_buffer(32768)
            self.dll.Everything_GetResultFullPathNameW(index, buffer, len(buffer))
            if buffer.value:
                paths.append(buffer.value)

        return paths

    def total_result_count(self):
        """Return total matches reported by the most recent SDK query."""
        if not self.dll:
            return 0

        return self.dll.Everything_GetTotResults()

    def last_error_message(self):
        """Return a friendly message for the last SDK error."""
        error_code = self.last_error_code()
        message = self.ERROR_MESSAGES.get(
            error_code,
            f"Everything SDK query failed with error {error_code}.",
        )
        return message

    def last_error_code(self):
        """Return the last Everything SDK error code."""
        return self.dll.Everything_GetLastError()


@dataclass(frozen=True)
class FolderSizeRecord:
    """
    One persisted folder-size analysis row.

    Each row represents one folder under the analysed root. size_bytes and
    file_count include all descendants.
    """

    root_path: str
    folder_path: str
    parent_path: str
    name: str
    depth: int
    size_bytes: int
    file_count: int
    folder_count: int
    error_count: int
    analysed_at: str


class FolderAnalysisStore:
    """
    Scans folder sizes and maintains a queryable SQLite table.

    analyse_and_store() scans a selected root folder, calculates recursive size
    totals for the root and every subfolder, then replaces the previous rows for
    that root in folder_size_analysis. Query helpers return rows as dictionaries
    so future UI or AI features can inspect the saved analysis without rescanning.
    """

    TABLE_NAME = "folder_size_analysis"
    REMOVED_COLUMNS = ("direct_size_bytes", "direct_file_count")

    def __init__(self, db_path=DEFAULT_ANALYSIS_DB):
        """Create a store backed by the configured SQLite database path."""
        self.db_path = Path(db_path)
        self.ensure_schema()

    def ensure_schema(self):
        """Create the folder analysis table and indexes when missing."""
        with self.connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root_path TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    parent_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    folder_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    analysed_at TEXT NOT NULL
                )
                """
            )
            self.drop_removed_columns(connection)
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_root
                ON {self.TABLE_NAME} (root_path)
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_folder
                ON {self.TABLE_NAME} (folder_path)
                """
            )

    def drop_removed_columns(self, connection):
        """Remove obsolete analysis columns from existing SQLite databases."""
        columns = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({self.TABLE_NAME})")
        }
        for column in self.REMOVED_COLUMNS:
            if column in columns:
                connection.execute(f"ALTER TABLE {self.TABLE_NAME} DROP COLUMN {column}")

    def connect(self):
        """Open a SQLite connection to the analysis database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def analyse_and_store(self, folder_path):
        """
        Analyse one folder tree, persist rows, and return the root summary row.

        Existing rows for the same root_path are deleted before new rows are
        inserted, so the table always reflects the latest analysis for that root.
        """
        root_path = self.normalized_path(folder_path)
        root = Path(root_path)
        if not root.is_dir():
            raise NotADirectoryError(root_path)

        analysed_at = datetime.now().isoformat(timespec="seconds")
        records = []
        root_record = self.scan_folder(root, root, analysed_at, records)
        self.replace_records(root_path, records)
        return root_record

    def scan_folder(self, folder_path, root_path, analysed_at, records):
        """Recursively scan a folder and append one record per folder."""
        total_size_bytes = 0
        total_file_count = 0
        total_folder_count = 0
        error_count = 0

        try:
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_record = self.scan_folder(
                                Path(entry.path),
                                root_path,
                                analysed_at,
                                records,
                            )
                            total_size_bytes += child_record.size_bytes
                            total_file_count += child_record.file_count
                            total_folder_count += 1 + child_record.folder_count
                            error_count += child_record.error_count
                            continue

                        file_size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        error_count += 1
                        continue

                    total_size_bytes += file_size
                    total_file_count += 1
        except OSError:
            error_count += 1

        record = FolderSizeRecord(
            root_path=str(root_path),
            folder_path=str(folder_path),
            parent_path=self.parent_path(folder_path, root_path),
            name=folder_path.name or str(folder_path),
            depth=self.folder_depth(folder_path, root_path),
            size_bytes=total_size_bytes,
            file_count=total_file_count,
            folder_count=total_folder_count,
            error_count=error_count,
            analysed_at=analysed_at,
        )
        records.append(record)
        return record

    def replace_records(self, root_path, records):
        """Replace all saved rows for root_path with the latest scan records."""
        rows = [asdict(record) for record in records]
        columns = list(FolderSizeRecord.__dataclass_fields__)
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)

        with self.connect() as connection:
            connection.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE root_path = ?",
                (root_path,),
            )
            connection.executemany(
                f"""
                INSERT INTO {self.TABLE_NAME} ({column_sql})
                VALUES ({placeholders})
                """,
                [tuple(row[column] for column in columns) for row in rows],
            )

    def query_records(self, root_path=None, limit=None):
        """Return saved analysis rows ordered by largest folders first."""
        sql = f"""
            SELECT
                root_path,
                folder_path,
                parent_path,
                name,
                depth,
                size_bytes,
                file_count,
                folder_count,
                error_count,
                analysed_at
            FROM {self.TABLE_NAME}
        """
        params = []
        if root_path:
            sql += " WHERE root_path = ?"
            params.append(self.normalized_path(root_path))

        sql += " ORDER BY size_bytes DESC, folder_path ASC"

        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params)]

    def root_summary(self, root_path):
        """Return the root row for an analysed folder, if present."""
        normalized = self.normalized_path(root_path)
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                f"""
                SELECT *
                FROM {self.TABLE_NAME}
                WHERE root_path = ? AND folder_path = ?
                LIMIT 1
                """,
                (normalized, normalized),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def folder_summary(self, folder_path):
        """Return the latest saved row for one folder path."""
        normalized = self.normalized_path(folder_path)
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                f"""
                SELECT *
                FROM {self.TABLE_NAME}
                WHERE folder_path = ?
                ORDER BY analysed_at DESC, root_path ASC
                LIMIT 1
                """,
                (normalized,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def child_folder_size_map(self, parent_path):
        """Return analysed folder sizes for direct children of parent_path."""
        parent = Path(self.normalized_path(parent_path))
        candidates = []

        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                f"""
                SELECT folder_path, parent_path, root_path, size_bytes, analysed_at
                FROM {self.TABLE_NAME}
                WHERE parent_path = ? OR folder_path = root_path
                ORDER BY analysed_at DESC
                """,
                (str(parent),),
            )
            candidates = [dict(row) for row in cursor]

        sizes = {}
        for row in candidates:
            folder_path = Path(row["folder_path"])
            if not self.same_path(folder_path.parent, parent):
                continue

            key = self.normalized_path(folder_path)
            if key not in sizes:
                sizes[key] = row["size_bytes"]

        return sizes

    @staticmethod
    def normalized_path(folder_path):
        """Return a stable absolute path string for storage and lookup."""
        return str(Path(folder_path).resolve(strict=False))

    @staticmethod
    def parent_path(folder_path, root_path):
        """Return an empty parent for the root row, otherwise the real parent."""
        if folder_path == root_path:
            return ""

        return str(folder_path.parent)

    @staticmethod
    def folder_depth(folder_path, root_path):
        """Return depth relative to the analysed root folder."""
        try:
            return len(folder_path.relative_to(root_path).parts)
        except ValueError:
            return 0

    @staticmethod
    def same_path(left, right):
        """Compare two stored paths with platform-aware normalization."""
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
            os.path.normpath(str(right))
        )


@dataclass(frozen=True)
class AIFolderRecord:
    id: int
    folder_path: str
    parent_path: str
    name: str
    size_bytes: int
    file_count: int
    folder_count: int
    error_count: int
    authorization_mode: str
    allow_read: bool
    allow_write: bool
    allow_delete: bool
    allow_execute: bool
    aifm_params: dict
    metadata: dict
    created_at: str
    updated_at: str


class AIFolderStore:
    TABLE_NAME = "ai_folders"
    AUTH_USER_REQUIRED = "user_required"
    AUTH_AI_DECIDES = "ai_decides"
    AUTH_ALWAYS_ALLOWED = "always_allowed"
    AUTH_MODES = {
        AUTH_USER_REQUIRED,
        AUTH_AI_DECIDES,
        AUTH_ALWAYS_ALLOWED,
    }
    DEFAULT_PERMISSIONS = {
        "read": True,
        "write": True,
        "delete": False,
        "execute": False,
    }
    DEFAULT_AIFM_PARAMS = {
        "folder_kind": "ai_folder",
        "managed_by": "AIFM",
        "agent_scope": "folder",
        "version": 1,
    }

    def __init__(self, db_path=DEFAULT_AI_FOLDER_DB):
        self.db_path = Path(db_path)
        self.ensure_schema()
        self.icon_path = Path(DEFAULT_AI_FOLDER_ICON).resolve(strict=False)

    def ensure_schema(self):
        with self.connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INTEGER PRIMARY KEY,
                    folder_path TEXT NOT NULL UNIQUE,
                    parent_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_count INTEGER NOT NULL,
                    folder_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    authorization_mode TEXT NOT NULL,
                    allow_read INTEGER NOT NULL,
                    allow_write INTEGER NOT NULL,
                    allow_delete INTEGER NOT NULL,
                    allow_execute INTEGER NOT NULL,
                    aifm_params_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.migrate_id_schema(connection)
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_parent
                ON {self.TABLE_NAME} (parent_path)
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_authorization
                ON {self.TABLE_NAME} (authorization_mode)
                """
            )

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def migrate_id_schema(self, connection):
        table_sql = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (self.TABLE_NAME,),
        ).fetchone()
        if not table_sql:
            return

        table_definition = table_sql[0] or ""
        stats = connection.execute(
            f"SELECT COUNT(*), MIN(id) FROM {self.TABLE_NAME}"
        ).fetchone()
        row_count = int(stats[0] or 0)
        min_id = stats[1]
        needs_rebuild = "AUTOINCREMENT" in table_definition.upper()
        needs_rebase = row_count > 0 and min_id != 0
        if not needs_rebuild and not needs_rebase:
            return

        cursor = connection.execute(f"SELECT * FROM {self.TABLE_NAME} ORDER BY id ASC")
        column_names = [column[0] for column in cursor.description]
        rows = [dict(zip(column_names, row)) for row in cursor.fetchall()]
        temp_table = f"{self.TABLE_NAME}_new"
        connection.execute(f"DROP TABLE IF EXISTS {temp_table}")
        connection.execute(
            f"""
            CREATE TABLE {temp_table} (
                id INTEGER PRIMARY KEY,
                folder_path TEXT NOT NULL UNIQUE,
                parent_path TEXT NOT NULL,
                name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_count INTEGER NOT NULL,
                folder_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL,
                authorization_mode TEXT NOT NULL,
                allow_read INTEGER NOT NULL,
                allow_write INTEGER NOT NULL,
                allow_delete INTEGER NOT NULL,
                allow_execute INTEGER NOT NULL,
                aifm_params_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = self.table_columns()
        placeholders = ", ".join("?" for _ in columns)
        for new_id, row in enumerate(rows):
            if needs_rebase:
                row["id"] = new_id
            connection.execute(
                f"""
                INSERT INTO {temp_table} ({", ".join(columns)})
                VALUES ({placeholders})
                """,
                [row[column] for column in columns],
            )

        connection.execute(f"DROP TABLE {self.TABLE_NAME}")
        connection.execute(f"ALTER TABLE {temp_table} RENAME TO {self.TABLE_NAME}")

    def apply_folder_icon(self, folder_path):
        folder = Path(folder_path)
        if not folder.is_dir() or not self.icon_path.exists():
            return

        desktop_ini = folder / "desktop.ini"
        desktop_ini.write_text(
            "\n".join(
                [
                    "[.ShellClassInfo]",
                    f"IconResource={self.icon_path},0",
                    f"IconFile={self.icon_path}",
                    "IconIndex=0",
                    "",
                ]
            ),
            encoding="utf-16",
        )
        self.set_windows_attributes(
            desktop_ini,
            FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM,
        )
        self.set_windows_attributes(
            folder,
            FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_SYSTEM,
        )

    def create_ai_folder(
        self,
        parent_path,
        name="New AIFolder",
        authorization_mode=AUTH_USER_REQUIRED,
        permissions=None,
        aifm_params=None,
        metadata=None,
    ):
        parent = Path(parent_path).resolve(strict=False)
        if not parent.is_dir():
            raise NotADirectoryError(str(parent))

        folder_name = self.safe_folder_name(name)
        folder_path = self.unique_child_path(parent, folder_name)
        folder_path.mkdir(parents=False, exist_ok=False)
        self.apply_folder_icon(folder_path)

        record = self.build_record(
            folder_path=folder_path,
            authorization_mode=authorization_mode,
            permissions=permissions,
            aifm_params=aifm_params,
            metadata=metadata,
        )
        self.upsert_record(record)
        return record

    def register_ai_folder(
        self,
        folder_path,
        authorization_mode=AUTH_USER_REQUIRED,
        permissions=None,
        aifm_params=None,
        metadata=None,
        create_if_missing=False,
    ):
        folder = Path(folder_path).resolve(strict=False)
        if create_if_missing:
            folder.mkdir(parents=True, exist_ok=True)
        if not folder.is_dir():
            raise NotADirectoryError(str(folder))
        self.apply_folder_icon(folder)

        record = self.build_record(
            folder_path=folder,
            authorization_mode=authorization_mode,
            permissions=permissions,
            aifm_params=aifm_params,
            metadata=metadata,
        )
        self.upsert_record(record)
        return record

    def refresh_ai_folder(self, folder_path):
        current = self.get_ai_folder(folder_path)
        if not current:
            raise KeyError(f"AI folder is not registered: {folder_path}")

        return self.register_ai_folder(
            folder_path=folder_path,
            authorization_mode=current["authorization_mode"],
            permissions={
                "read": current["allow_read"],
                "write": current["allow_write"],
                "delete": current["allow_delete"],
                "execute": current["allow_execute"],
            },
            aifm_params=current["aifm_params"],
            metadata=current["metadata"],
        )

    def build_record(
        self,
        folder_path,
        authorization_mode,
        permissions=None,
        aifm_params=None,
        metadata=None,
    ):
        folder = Path(folder_path).resolve(strict=False)
        size_bytes, file_count, folder_count, error_count = self.folder_statistics(folder)
        permission_values = self.normalized_permissions(permissions)
        params = dict(self.DEFAULT_AIFM_PARAMS)
        if aifm_params:
            params.update(dict(aifm_params))

        now = datetime.now().isoformat(timespec="seconds")
        existing = self.get_ai_folder(folder)
        created_at = existing["created_at"] if existing else now

        return AIFolderRecord(
            id=existing["id"] if existing else self.next_id(),
            folder_path=str(folder),
            parent_path=str(folder.parent),
            name=folder.name or str(folder),
            size_bytes=size_bytes,
            file_count=file_count,
            folder_count=folder_count,
            error_count=error_count,
            authorization_mode=self.normalized_authorization_mode(authorization_mode),
            allow_read=permission_values["read"],
            allow_write=permission_values["write"],
            allow_delete=permission_values["delete"],
            allow_execute=permission_values["execute"],
            aifm_params=params,
            metadata=dict(metadata or {}),
            created_at=created_at,
            updated_at=now,
        )

    def upsert_record(self, record):
        row = self.record_to_row(record)
        with self.connect() as connection:
            existing = connection.execute(
                f"SELECT id FROM {self.TABLE_NAME} WHERE folder_path = ?",
                (row["folder_path"],),
            ).fetchone()
            if existing:
                row["id"] = int(existing[0])
                self.update_row(connection, row)
            else:
                self.insert_row(connection, row)

    def insert_row(self, connection, row):
        columns = self.table_columns()
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"""
            INSERT INTO {self.TABLE_NAME} ({", ".join(columns)})
            VALUES ({placeholders})
            """,
            [row[column] for column in columns],
        )

    def update_row(self, connection, row):
        update_columns = [
            column
            for column in self.table_columns()
            if column not in {"id", "folder_path"}
        ]
        assignments = ", ".join(f"{column} = ?" for column in update_columns)
        connection.execute(
            f"""
            UPDATE {self.TABLE_NAME}
            SET {assignments}
            WHERE folder_path = ?
            """,
            [row[column] for column in update_columns] + [row["folder_path"]],
        )

    def next_id(self):
        with self.connect() as connection:
            return self.available_id(self.used_ids(connection))

    @staticmethod
    def available_id(used_ids, preferred=None):
        if preferred is not None:
            preferred = int(preferred)
            if preferred >= 0 and preferred not in used_ids:
                return preferred

        candidate = 0
        while candidate in used_ids:
            candidate += 1
        return candidate

    def used_ids(self, connection):
        return {
            int(row[0])
            for row in connection.execute(f"SELECT id FROM {self.TABLE_NAME}")
        }

    @staticmethod
    def table_columns():
        return [
            "id",
            "folder_path",
            "parent_path",
            "name",
            "size_bytes",
            "file_count",
            "folder_count",
            "error_count",
            "authorization_mode",
            "allow_read",
            "allow_write",
            "allow_delete",
            "allow_execute",
            "aifm_params_json",
            "metadata_json",
            "created_at",
            "updated_at",
        ]

    def get_ai_folder(self, folder_path):
        normalized = self.normalized_path(folder_path)
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                f"""
                SELECT *
                FROM {self.TABLE_NAME}
                WHERE folder_path = ?
                LIMIT 1
                """,
                (normalized,),
            )
            row = cursor.fetchone()
            return self.row_to_dict(row) if row else None

    def query_ai_folders(self, parent_path=None, authorization_mode=None, limit=None):
        sql = f"""
            SELECT *
            FROM {self.TABLE_NAME}
        """
        clauses = []
        params = []
        if parent_path:
            clauses.append("parent_path = ?")
            params.append(self.normalized_path(parent_path))
        if authorization_mode:
            clauses.append("authorization_mode = ?")
            params.append(self.normalized_authorization_mode(authorization_mode))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        sql += " ORDER BY updated_at DESC, folder_path ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            return [self.row_to_dict(row) for row in connection.execute(sql, params)]

    def delete_records_for_paths(self, folder_paths):
        roots = [self.normalized_path(path) for path in folder_paths if path]
        if not roots:
            return []

        records = self.query_ai_folders()
        deleted_records = [
            record
            for record in records
            if any(self.is_same_or_child_path(record["folder_path"], root) for root in roots)
        ]
        if not deleted_records:
            return []

        with self.connect() as connection:
            connection.executemany(
                f"DELETE FROM {self.TABLE_NAME} WHERE folder_path = ?",
                [(record["folder_path"],) for record in deleted_records],
            )

        return deleted_records

    def restore_records(self, records):
        if not records:
            return 0

        with self.connect() as connection:
            used_ids = self.used_ids(connection)
            rows = [self.record_dict_to_row(record) for record in records]
            for row in rows:
                existing = connection.execute(
                    f"SELECT id FROM {self.TABLE_NAME} WHERE folder_path = ?",
                    (row["folder_path"],),
                ).fetchone()
                if existing:
                    old_id = row["id"]
                    row["id"] = int(existing[0])
                    used_ids.discard(old_id)
                    used_ids.add(row["id"])
                    self.update_row(connection, row)
                    continue

                row["id"] = self.available_id(used_ids, preferred=row["id"])
                used_ids.add(row["id"])
                self.insert_row(connection, row)

        return len(rows)

    def cleanup_missing_records(self):
        records = self.query_ai_folders()
        missing_paths = [
            record["folder_path"]
            for record in records
            if not Path(record["folder_path"]).exists()
        ]
        if not missing_paths:
            return 0

        self.delete_records_for_paths(missing_paths)
        return len(missing_paths)

    def set_authorization_mode(self, folder_path, authorization_mode):
        normalized = self.normalized_path(folder_path)
        mode = self.normalized_authorization_mode(authorization_mode)
        updated_at = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE {self.TABLE_NAME}
                SET authorization_mode = ?, updated_at = ?
                WHERE folder_path = ?
                """,
                (mode, updated_at, normalized),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"AI folder is not registered: {folder_path}")

    def set_permissions(self, folder_path, permissions):
        normalized = self.normalized_path(folder_path)
        values = self.normalized_permissions(permissions)
        updated_at = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE {self.TABLE_NAME}
                SET
                    allow_read = ?,
                    allow_write = ?,
                    allow_delete = ?,
                    allow_execute = ?,
                    updated_at = ?
                WHERE folder_path = ?
                """,
                (
                    int(values["read"]),
                    int(values["write"]),
                    int(values["delete"]),
                    int(values["execute"]),
                    updated_at,
                    normalized,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"AI folder is not registered: {folder_path}")

    def can_ai_operate(self, folder_path, operation):
        record = self.get_ai_folder(folder_path)
        if not record:
            return False

        operation_key = self.permission_key_for_operation(operation)
        return bool(record[f"allow_{operation_key}"])

    def needs_user_authorization(self, folder_path, ai_requires_authorization=None):
        record = self.get_ai_folder(folder_path)
        if not record:
            return True

        mode = record["authorization_mode"]
        if mode == self.AUTH_ALWAYS_ALLOWED:
            return False
        if mode == self.AUTH_AI_DECIDES:
            return True if ai_requires_authorization is None else bool(ai_requires_authorization)

        return True

    @staticmethod
    def permission_key_for_operation(operation):
        operation_text = str(operation or "").strip().lower().replace("-", "_")
        write_operations = {
            "write",
            "create",
            "copy",
            "cut",
            "move",
            "rename",
            "paste",
            "update",
            "modify",
        }
        delete_operations = {"delete", "remove", "trash"}
        execute_operations = {"execute", "run", "open"}

        if operation_text in delete_operations:
            return "delete"
        if operation_text in execute_operations:
            return "execute"
        if operation_text in write_operations:
            return "write"

        return "read"

    def record_to_row(self, record):
        return {
            "id": int(record.id),
            "folder_path": record.folder_path,
            "parent_path": record.parent_path,
            "name": record.name,
            "size_bytes": record.size_bytes,
            "file_count": record.file_count,
            "folder_count": record.folder_count,
            "error_count": record.error_count,
            "authorization_mode": record.authorization_mode,
            "allow_read": int(record.allow_read),
            "allow_write": int(record.allow_write),
            "allow_delete": int(record.allow_delete),
            "allow_execute": int(record.allow_execute),
            "aifm_params_json": self.dumps_json(record.aifm_params),
            "metadata_json": self.dumps_json(record.metadata),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def row_to_dict(self, row):
        data = dict(row)
        data["allow_read"] = bool(data["allow_read"])
        data["allow_write"] = bool(data["allow_write"])
        data["allow_delete"] = bool(data["allow_delete"])
        data["allow_execute"] = bool(data["allow_execute"])
        data["aifm_params"] = self.loads_json(data.pop("aifm_params_json"))
        data["metadata"] = self.loads_json(data.pop("metadata_json"))
        data["size"] = format_bytes(data["size_bytes"])
        return data

    def record_dict_to_row(self, record):
        return {
            "id": int(record.get("id") if record.get("id") is not None else -1),
            "folder_path": self.normalized_path(record["folder_path"]),
            "parent_path": self.normalized_path(record["parent_path"]),
            "name": str(record["name"]),
            "size_bytes": int(record.get("size_bytes") or 0),
            "file_count": int(record.get("file_count") or 0),
            "folder_count": int(record.get("folder_count") or 0),
            "error_count": int(record.get("error_count") or 0),
            "authorization_mode": self.normalized_authorization_mode(
                record.get("authorization_mode")
            ),
            "allow_read": int(bool(record.get("allow_read"))),
            "allow_write": int(bool(record.get("allow_write"))),
            "allow_delete": int(bool(record.get("allow_delete"))),
            "allow_execute": int(bool(record.get("allow_execute"))),
            "aifm_params_json": self.dumps_json(record.get("aifm_params")),
            "metadata_json": self.dumps_json(record.get("metadata")),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
        }

    def folder_statistics(self, folder_path):
        size_bytes = 0
        file_count = 0
        folder_count = 0
        error_count = 0

        try:
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child = self.folder_statistics(Path(entry.path))
                            size_bytes += child[0]
                            file_count += child[1]
                            folder_count += 1 + child[2]
                            error_count += child[3]
                            continue

                        size_bytes += entry.stat(follow_symlinks=False).st_size
                        file_count += 1
                    except OSError:
                        error_count += 1
        except OSError:
            error_count += 1

        return size_bytes, file_count, folder_count, error_count

    def normalized_permissions(self, permissions):
        values = dict(self.DEFAULT_PERMISSIONS)
        for key, value in dict(permissions or {}).items():
            if key in values:
                values[key] = bool(value)
        return values

    def normalized_authorization_mode(self, authorization_mode):
        mode = str(authorization_mode or self.AUTH_USER_REQUIRED).strip().lower()
        mode = mode.replace("-", "_")
        aliases = {
            "required": self.AUTH_USER_REQUIRED,
            "require_user": self.AUTH_USER_REQUIRED,
            "user": self.AUTH_USER_REQUIRED,
            "ask_user": self.AUTH_USER_REQUIRED,
            "ai": self.AUTH_AI_DECIDES,
            "ai_decide": self.AUTH_AI_DECIDES,
            "ai_infer": self.AUTH_AI_DECIDES,
            "always": self.AUTH_ALWAYS_ALLOWED,
            "allow": self.AUTH_ALWAYS_ALLOWED,
            "trusted": self.AUTH_ALWAYS_ALLOWED,
        }
        mode = aliases.get(mode, mode)
        if mode not in self.AUTH_MODES:
            raise ValueError(f"Unknown AI folder authorization mode: {authorization_mode}")
        return mode

    @staticmethod
    def safe_folder_name(name):
        cleaned = "".join("_" if character in '<>:"/\\|?*' else character for character in str(name))
        cleaned = cleaned.strip().strip(".")
        return cleaned or "New AIFolder"

    @staticmethod
    def unique_child_path(parent, folder_name):
        target = parent / folder_name
        if not target.exists():
            return target

        index = 1
        while True:
            candidate = parent / f"{folder_name} ({index})"
            if not candidate.exists():
                return candidate
            index += 1

    @staticmethod
    def normalized_path(folder_path):
        return str(Path(folder_path).resolve(strict=False))

    @staticmethod
    def is_same_or_child_path(path_text, root_text):
        path = os.path.normcase(os.path.normpath(str(path_text)))
        root = os.path.normcase(os.path.normpath(str(root_text)))
        if path == root:
            return True

        root_prefix = root.rstrip(os.sep) + os.sep
        return path.startswith(root_prefix)

    @staticmethod
    def set_windows_attributes(path, attributes):
        if os.name != "nt":
            return

        path_text = str(Path(path))
        kernel32 = ctypes.windll.kernel32
        current = kernel32.GetFileAttributesW(path_text)
        if current == INVALID_FILE_ATTRIBUTES:
            return

        kernel32.SetFileAttributesW(path_text, current | attributes)

    @staticmethod
    def dumps_json(value):
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def loads_json(value):
        try:
            data = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


def format_bytes(size_bytes):
    """Format a byte count for compact UI messages."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(size_bytes)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size_bytes} B"
            return f"{size:.2f} {unit}"
        size /= 1024

    return f"{size_bytes} B"
