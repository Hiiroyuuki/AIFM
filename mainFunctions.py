"""Core non-UI functions used by the file manager frontend."""

import ctypes
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


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ANALYSIS_DB = BASE_DIR / "folder_analysis.sqlite3"
EVERYTHING_RESULT_LIMIT = 1000
EVERYTHING_SDK_DLL_PATH = BASE_DIR / "Everything-SDK" / "dll" / "Everything64.dll"
EVERYTHING_START_TIMEOUT_SECONDS = 6


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
            shutil.rmtree(trash_root)
        except OSError as error:
            return str(error)

        return ""

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
