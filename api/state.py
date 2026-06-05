"""Shared runtime state for the AIFM FastAPI server.

A single AppState instance is created at import time and shared by all
routers. It holds every service object and the in-memory undo/redo stacks
that replace the Qt-side undo_stack / redo_stack in the old frontend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config_loader import Config
from mainFunctions import (
    AIFolderStore,
    EverythingSdkSearch,
    FileOperationService,
    FolderAnalysisStore,
)


@dataclass
class UndoEntry:
    """One undoable batch of file operations kept on the server-side stack."""

    action: str                                    # "copy" | "move" | "delete"
    operations: list[dict[str, Any]]               # serialisable op records
    ai_folder_records: list[dict[str, Any]] = field(default_factory=list)


class AppState:
    """Singleton that holds all service instances and mutable server state."""

    def __init__(self) -> None:
        self.config = Config()
        self.file_operations = FileOperationService()
        self.analysis_store = FolderAnalysisStore()
        self.ai_folder_store = AIFolderStore()
        self.search_engine = EverythingSdkSearch()
        self._everything_started = False
        self.undo_stack: list[UndoEntry] = []
        self.redo_stack: list[UndoEntry] = []

    # ------------------------------------------------------------------
    # Everything startup
    # ------------------------------------------------------------------

    def ensure_everything_started(self) -> str:
        """Start the Everything process if it is not running yet."""
        if not self._everything_started:
            msg = self.search_engine.start()
            self._everything_started = True
            return msg
        return ""

    # ------------------------------------------------------------------
    # Config reload (called after writing config.json)
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        self.config = Config(self.config.path)

    # ------------------------------------------------------------------
    # Undo / redo helpers
    # ------------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def push_undo(self, entry: UndoEntry) -> None:
        self.undo_stack.append(entry)
        self.redo_stack.clear()


# Module-level singleton — imported by every router.
state = AppState()
