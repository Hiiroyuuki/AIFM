"""
PySide6 desktop frontend for the AI file manager.

The module is organized by UI responsibility: resource loading, display models,
Everything search integration, widget collection, layout, browsing, preview,
history, and the top-level window controller.
"""

import ctypes
import html
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDir, QFile, QFileInfo, Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QStandardItem,
    QStandardItemModel,
    QTextDocument,
    QTextOption,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileIconProvider,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = "C:/"
FILE_TYPE_NAMES_PATH = BASE_DIR / "file_type_names.json"
UI_PATH = BASE_DIR / "form.ui"
EVERYTHING_RESULT_LIMIT = 1000
EVERYTHING_SDK_DLL_PATH = BASE_DIR / "Everything-SDK" / "dll" / "Everything64.dll"
EVERYTHING_START_TIMEOUT_SECONDS = 6
FOLDER_COLUMN_WIDTHS = (220, 90, 140, 160)
SEARCH_NAME_COLUMN_WIDTH = 180

SELECTED_ROW_BACKGROUND = "#e8f1ff"
SELECTED_ROW_TEXT = "#111111"
SEARCH_HIGHLIGHT_BACKGROUND = "#fff0a8"

PREVIEW_TABLE_STYLE = """
QTableView {
    gridline-color: transparent;
}
QTableView::item {
    border-bottom: 1px solid #d8d8d8;
    padding: 1px 4px;
}
QTableView::item:selected {
    background-color: #e8f1ff;
    color: #111111;
}
"""


def selected_row_style(widget_name, item_padding="1px 4px", include_tree_branch=False):
    """Build a borderless selected-row stylesheet for tree and table views."""
    style = f"""
{widget_name} {{
    selection-background-color: {SELECTED_ROW_BACKGROUND};
    selection-color: {SELECTED_ROW_TEXT};
    outline: 0;
}}
{widget_name}::item {{
    border: none;
    outline: none;
    padding: {item_padding};
}}
{widget_name}::item:selected,
{widget_name}::item:selected:active,
{widget_name}::item:selected:!active,
{widget_name}::item:focus {{
    background-color: {SELECTED_ROW_BACKGROUND};
    color: {SELECTED_ROW_TEXT};
    border: none;
    outline: none;
}}
"""

    if include_tree_branch:
        style += f"""
{widget_name}::branch:selected,
{widget_name}::branch:selected:active,
{widget_name}::branch:selected:!active {{
    background-color: {SELECTED_ROW_BACKGROUND};
    border: none;
    outline: none;
}}
"""

    return style


def load_ui():
    """Create the top-level window defined in form.ui."""
    ui_file = QFile(str(UI_PATH))
    ui_file.open(QFile.ReadOnly)
    window = QUiLoader().load(ui_file)
    ui_file.close()
    return window


class FileTypeRegistry:
    """
    Resolves file extensions to user-facing type names.

    The registry is intentionally small: it loads a JSON mapping at startup,
    falls back to Python's mimetype database, and finally falls back to the raw
    extension. PreviewPanel uses this class so type naming stays outside the UI
    rendering code.
    """

    def __init__(self, json_path):
        """Load type names from the configured JSON file."""
        self.type_names = self.load(json_path)

    def load(self, json_path):
        """Read extension display names from disk."""
        try:
            with json_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

        return {
            extension.lower(): name
            for extension, name in data.items()
            if extension.startswith(".") and isinstance(name, str)
        }

    def describe(self, path):
        """Return a human-readable file type for a path."""
        suffix = path.suffix.lower()
        if suffix in self.type_names:
            return self.type_names[suffix]

        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type:
            main_type, sub_type = mime_type.split("/", 1)
            return f"{sub_type.replace('-', ' ').title()} {main_type}"

        if suffix:
            return f"{suffix[1:].upper()} file"

        return "File"


class CharacterElideDelegate(QStyledItemDelegate):
    """
    Draws table cells with safe eliding and optional search-term highlights.

    The central table uses one delegate in both folder mode and search mode.
    Folder mode only needs normal text clipping. Search mode calls
    set_highlight_query(), then paint() draws matching terms with a light
    background while preserving selection styling and clipping to the cell.
    """

    def __init__(self, parent=None):
        """Create a delegate with search highlighting disabled."""
        super().__init__(parent)
        self.highlight_terms = []

    def initStyleOption(self, option, index):
        """Apply right-side eliding to each rendered table cell."""
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideRight

    def set_highlight_query(self, query):
        """Extract literal words from a query for table-cell highlighting."""
        terms = []
        for token in re.findall(r'"[^"]+"|\S+', query or ""):
            term = token.strip('"').strip("*?")
            if term and ":" not in term:
                terms.append(term)

        self.highlight_terms = sorted(set(terms), key=len, reverse=True)
        if self.parent() is not None:
            self.parent().viewport().update()

    def paint(self, painter, option, index):
        """Paint highlighted search terms when any are configured."""
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not self.highlight_terms or not self.has_match(text):
            super().paint(painter, option, index)
            return

        display_option = QStyleOptionViewItem(option)
        self.initStyleOption(display_option, index)
        style = display_option.widget.style() if display_option.widget else QApplication.style()

        text_option = QStyleOptionViewItem(display_option)
        text_option.text = ""
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            text_option,
            painter,
            display_option.widget,
        )

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText,
            display_option,
            display_option.widget,
        )
        self.draw_highlighted_text(painter, text_rect, display_option, text)

    def has_match(self, text):
        """Return whether the text contains any highlighted term."""
        lowered = text.lower()
        return any(term.lower() in lowered for term in self.highlight_terms)

    def draw_highlighted_text(self, painter, text_rect, option, text):
        """Draw highlighted rich text clipped to the cell text rectangle."""
        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(option.font)

        no_wrap = QTextOption()
        no_wrap.setWrapMode(QTextOption.WrapMode.NoWrap)
        document.setDefaultTextOption(no_wrap)
        document.setHtml(self.highlight_html(text, option))

        painter.save()
        painter.setClipRect(text_rect)
        y_offset = max(0, (text_rect.height() - document.size().height()) / 2)
        painter.translate(text_rect.left(), text_rect.top() + y_offset)
        document.drawContents(painter)
        painter.restore()

    def highlight_html(self, text, option):
        """Return HTML with query terms wrapped in highlight spans."""
        text_color = SELECTED_ROW_TEXT
        if not option.state & QStyle.StateFlag.State_Selected:
            text_color = option.palette.text().color().name()

        pattern = re.compile(
            "|".join(re.escape(term) for term in self.highlight_terms),
            re.IGNORECASE,
        )
        parts = []
        last_end = 0
        for match in pattern.finditer(text):
            parts.append(html.escape(text[last_end : match.start()]))
            parts.append(
                "<span style="
                f"'background-color: {SEARCH_HIGHLIGHT_BACKGROUND}; color: {text_color};'"
                f">{html.escape(match.group(0))}</span>"
            )
            last_end = match.end()
        parts.append(html.escape(text[last_end:]))

        return (
            f"<span style='white-space: nowrap; color: {text_color};'>"
            f"{''.join(parts)}</span>"
        )


class FileTableModel(QFileSystemModel):
    """
    QFileSystemModel used by the regular folder browser table.

    Qt's default modified-time string follows system locale conventions, which
    can make the column visually uneven. This subclass keeps all filesystem
    behavior from QFileSystemModel and only normalizes the Date Modified display
    column.
    """

    MODIFIED_COLUMN = 3
    MODIFIED_FORMAT = "yyyy-MM-dd HH:mm:ss"

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Format the modified time column with fixed-width date components."""
        if role == Qt.ItemDataRole.DisplayRole and index.column() == self.MODIFIED_COLUMN:
            modified = self.fileInfo(index).lastModified()
            if modified.isValid():
                return modified.toString(self.MODIFIED_FORMAT)

        return super().data(index, role)


class EverythingSdkSearch:
    """
    Adapter for the Everything SDK DLL.

    The rest of the app treats search as a Python method returning paths plus
    an optional message. This class owns all ctypes binding details, readiness
    checks, optional Everything.exe startup, query execution, and SDK error
    translation.
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
        """Load the SDK and remember the maximum number of shown results."""
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
        """Locate Everything.exe for optional startup support."""
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
        """Start Everything if possible and wait briefly for its database."""
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
        """Return whether the Everything SDK reports a loaded database."""
        try:
            if hasattr(self.dll, "Everything_IsDBLoaded") and self.dll.Everything_IsDBLoaded():
                return True
        except OSError:
            return False

        return False

    def search(self, query):
        """Run a query and return paths plus an optional error message."""
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
        """Load and bind the Everything SDK DLL."""
        if not self.dll_path.exists():
            return None

        try:
            dll = ctypes.WinDLL(str(self.dll_path))
        except OSError:
            return None

        self.bind_functions(dll)
        return dll

    def bind_functions(self, dll):
        """Declare ctypes signatures for the SDK functions used here."""
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
        """Collect full result paths from the last SDK query."""
        paths = []
        count = self.dll.Everything_GetNumResults()

        for index in range(count):
            buffer = ctypes.create_unicode_buffer(32768)
            self.dll.Everything_GetResultFullPathNameW(index, buffer, len(buffer))
            if buffer.value:
                paths.append(buffer.value)

        return paths

    def total_result_count(self):
        """Return the total result count reported by the last SDK query."""
        if not self.dll:
            return 0

        return self.dll.Everything_GetTotResults()

    def last_error_message(self):
        """Return a friendly message for the last SDK error code."""
        error_code = self.last_error_code()
        message = self.ERROR_MESSAGES.get(
            error_code,
            f"Everything SDK query failed with error {error_code}.",
        )
        return message

    def last_error_code(self):
        """Return the SDK's last error code."""
        return self.dll.Everything_GetLastError()


@dataclass
class UiElements:
    """
    Strongly named references to widgets loaded from form.ui.

    The Qt Designer file owns object creation. This dataclass gives the Python
    code a typed, centralized place to resolve the object names used by the
    controllers, which keeps findChild() calls out of the feature logic.
    """

    window: QWidget
    central_widget: QWidget
    tree_view: QTreeView
    table_view: QTableView
    status_list: QListWidget
    back_button: QPushButton
    forward_button: QPushButton
    navigate_bar: QLineEdit
    search_button: QPushButton
    preview_view: QTableView
    ai_view: QTableView
    side_buttons: list[QPushButton]

    @classmethod
    def collect(cls, window):
        """Find all widgets used by the Python controller."""
        return cls(
            window=window,
            central_widget=window.findChild(QWidget, "centralwidget") or window,
            tree_view=window.findChild(QTreeView, "treeView"),
            table_view=window.findChild(QTableView, "tableView"),
            status_list=window.findChild(QListWidget, "listWidget"),
            back_button=window.findChild(QPushButton, "back"),
            forward_button=window.findChild(QPushButton, "forward"),
            navigate_bar=window.findChild(QLineEdit, "navigateBar"),
            search_button=window.findChild(QPushButton, "search"),
            preview_view=window.findChild(QTableView, "preview"),
            ai_view=window.findChild(QTableView, "AIview"),
            side_buttons=[
                window.findChild(QPushButton, f"pushButton_{index}")
                for index in range(1, 8)
            ],
        )


class LayoutManager:
    """
    Builds the runtime layout around widgets from form.ui.

    The .ui file provides the widgets, but this class creates the final dynamic
    splitter layout: toolbar, fixed side button column, folder tree, file table,
    status row, preview table, and AI preview placeholder. It owns sizing and
    stretch behavior only, not browsing or search logic.
    """

    SIDE_PANEL_WIDTH = 110
    PANEL_SPACING = 6

    def __init__(self, ui):
        """Store resolved UI widgets for layout composition."""
        self.ui = ui

    def setup(self):
        """Compose toolbar, side buttons, file table, and preview panes."""
        toolbar_layout = self.create_toolbar()
        side_panel = self.create_side_panel()
        content_splitter = self.create_content_splitter()

        body_layout = QHBoxLayout()
        body_layout.setSpacing(self.PANEL_SPACING)
        body_layout.addWidget(side_panel)
        body_layout.addWidget(content_splitter, 1)

        main_layout = QVBoxLayout(self.ui.central_widget)
        main_layout.addLayout(toolbar_layout)
        main_layout.addLayout(body_layout, 1)

        self.apply_sizes(side_panel)

    def create_toolbar(self):
        """Create the top navigation row."""
        toolbar_layout = QHBoxLayout()
        toolbar_layout.addSpacing(self.SIDE_PANEL_WIDTH + self.PANEL_SPACING)
        toolbar_layout.addWidget(self.ui.back_button)
        toolbar_layout.addWidget(self.ui.forward_button)
        toolbar_layout.addWidget(self.ui.navigate_bar, 1)
        toolbar_layout.addWidget(self.ui.search_button)
        return toolbar_layout

    def create_side_panel(self):
        """Create the fixed-width left action button column."""
        side_panel = QWidget(self.ui.central_widget)
        side_layout = QVBoxLayout(side_panel)
        for button in self.ui.side_buttons:
            if button is not None:
                side_layout.addWidget(button)
        side_layout.addStretch()
        return side_panel

    def create_content_splitter(self):
        """Create the resizable tree, file, preview, and AI preview panes."""
        file_splitter = QSplitter(Qt.Orientation.Vertical, self.ui.central_widget)
        file_splitter.addWidget(self.ui.table_view)
        file_splitter.addWidget(self.ui.status_list)
        file_splitter.setStretchFactor(0, 1)
        file_splitter.setStretchFactor(1, 0)
        file_splitter.setSizes([500, 28])
        file_splitter.setChildrenCollapsible(False)

        preview_splitter = QSplitter(Qt.Orientation.Vertical, self.ui.central_widget)
        preview_splitter.addWidget(self.ui.preview_view)
        preview_splitter.addWidget(self.ui.ai_view)
        preview_splitter.setStretchFactor(0, 1)
        preview_splitter.setStretchFactor(1, 1)
        preview_splitter.setSizes([260, 260])
        preview_splitter.setChildrenCollapsible(False)

        content_splitter = QSplitter(Qt.Orientation.Horizontal, self.ui.central_widget)
        content_splitter.addWidget(self.ui.tree_view)
        content_splitter.addWidget(file_splitter)
        content_splitter.addWidget(preview_splitter)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 2)
        content_splitter.setStretchFactor(2, 1)
        content_splitter.setSizes([200, 600, 240])
        content_splitter.setChildrenCollapsible(False)
        return content_splitter

    def apply_sizes(self, side_panel):
        """Apply minimum and fixed sizes after widgets enter splitters."""
        side_panel.setFixedWidth(self.SIDE_PANEL_WIDTH)
        self.ui.tree_view.setMinimumWidth(180)
        self.ui.table_view.setMinimumWidth(260)
        self.ui.status_list.setFixedHeight(28)
        self.ui.preview_view.setMinimumWidth(220)
        self.ui.ai_view.setMinimumWidth(220)
        self.ui.tree_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.ui.table_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)


class FileBrowser:
    """
    Controls the left folder tree, central table, and one-line status area.

    The browser has two table modes: regular folder browsing backed by
    FileTableModel, and Everything search results backed by QStandardItemModel.
    It also handles column sizing, search result icons/tooltips, selected-path
    resolution, tree synchronization, and compact item-count/search status text.
    """

    def __init__(self, ui):
        """Create filesystem/search models used by the browser area."""
        self.ui = ui
        self.search_mode = False
        self.syncing_tree = False
        self.dir_model = QFileSystemModel(ui.window)
        self.file_model = FileTableModel(ui.window)
        self.icon_provider = QFileIconProvider()
        self.search_model = QStandardItemModel(ui.window)
        self.table_delegate = CharacterElideDelegate(ui.table_view)

    def setup(self):
        """Initialize tree, table, status line, and default folder."""
        self.setup_tree_model()
        self.setup_table_view()
        self.setup_status_list()
        self.setup_file_table_model()

    def setup_tree_model(self):
        """Configure the left folder tree."""
        self.dir_model.setFilter(QDir.Drives | QDir.AllDirs | QDir.NoDotAndDotDot)
        self.dir_model.setRootPath("")

        self.ui.tree_view.setModel(self.dir_model)
        self.ui.tree_view.setRootIndex(self.dir_model.index(""))
        self.ui.tree_view.setHeaderHidden(True)
        self.ui.tree_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.ui.tree_view.setAllColumnsShowFocus(True)
        self.ui.tree_view.setIndentation(10)
        self.ui.tree_view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.ui.tree_view.setStyleSheet(
            selected_row_style(
                "QTreeView",
                item_padding="1px 2px",
                include_tree_branch=True,
            )
        )
        for column in range(1, self.dir_model.columnCount()):
            self.ui.tree_view.hideColumn(column)

    def setup_file_table_model(self):
        """Show the default folder in the central file table."""
        self.file_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)
        self.file_model.setRootPath(DEFAULT_PATH)

        self.ui.table_view.setModel(self.file_model)
        self.ui.table_view.setRootIndex(self.file_model.index(DEFAULT_PATH))
        self.update_folder_info(DEFAULT_PATH)

    def setup_table_view(self):
        """Configure shared behavior for folder and search tables."""
        self.ui.table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ui.table_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.ui.table_view.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.ui.table_view.setAlternatingRowColors(True)
        self.ui.table_view.setSortingEnabled(True)
        self.ui.table_view.setWordWrap(False)
        self.ui.table_view.setShowGrid(False)
        self.ui.table_view.setItemDelegate(self.table_delegate)
        self.ui.table_view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.ui.table_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.ui.table_view.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.ui.table_view.verticalHeader().setVisible(False)
        self.ui.table_view.horizontalHeader().setStretchLastSection(True)
        self.ui.table_view.setStyleSheet(selected_row_style("QTableView"))

    def setup_status_list(self):
        """Make the listWidget behave like a one-line status bar."""
        self.ui.status_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ui.status_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.ui.status_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ui.status_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.ui.status_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.ui.status_list.setWordWrap(False)

    def clear_table_selection(self):
        """Clear table selection before switching models to avoid stale indexes."""
        selection_model = self.ui.table_view.selectionModel()
        if selection_model is not None:
            selection_model.clear()

        self.ui.table_view.clearSelection()

    def show_folder(self, folder_path):
        """Display a folder with the QFileSystemModel table."""
        self.search_mode = False
        self.clear_table_selection()
        self.table_delegate.set_highlight_query("")
        self.ui.table_view.setModel(self.file_model)
        self.configure_folder_columns()
        self.ui.table_view.setRootIndex(self.file_model.setRootPath(folder_path))
        self.update_folder_info(folder_path)

    def configure_folder_columns(self):
        """Restore normal file-browser column widths after search mode."""
        header = self.ui.table_view.horizontalHeader()
        header.setStretchLastSection(True)
        for column in range(self.file_model.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

        for column, width in enumerate(FOLDER_COLUMN_WIDTHS):
            self.ui.table_view.setColumnWidth(column, width)

    def show_search_results(self, paths, message="", query=""):
        """Display Everything search results in the central table."""
        self.search_mode = True
        self.clear_table_selection()
        self.table_delegate.set_highlight_query(query)
        self.search_model.clear()
        self.search_model.setHorizontalHeaderLabels(["Name", "Path"])
        self.search_model.setHeaderData(
            1,
            Qt.Orientation.Horizontal,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            Qt.ItemDataRole.TextAlignmentRole,
        )

        if message:
            self.search_model.appendRow(self.create_search_row(message, ""))
        elif not paths:
            self.search_model.appendRow(self.create_search_row("No search results.", ""))
        else:
            for path_text in paths:
                self.search_model.appendRow(self.create_search_row_for_path(path_text))

        self.ui.table_view.setModel(self.search_model)
        self.ui.table_view.setRootIndex(self.search_model.index(0, 0).parent())
        self.configure_search_columns()

    def configure_search_columns(self):
        """Keep Name fixed and let only Path expand for search results."""
        header = self.ui.table_view.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.ui.table_view.setColumnWidth(0, SEARCH_NAME_COLUMN_WIDTH)
        self.ui.table_view.resizeColumnToContents(1)

    def create_search_row_for_path(self, path_text):
        """Create a search result row with icon, name, path, and tooltips."""
        path = Path(path_text)
        name = path.name or path_text
        parent = str(path.parent) if path.parent != path else ""
        name_item = self.create_search_item(name, path_text)
        path_item = self.create_search_item(parent, path_text)
        name_item.setIcon(self.icon_provider.icon(QFileInfo(path_text)))
        return [name_item, path_item]

    @staticmethod
    def create_search_row(name, path_text):
        """Create a generic two-column search row."""
        return [
            FileBrowser.create_search_item(name, path_text),
            FileBrowser.create_search_item(path_text, path_text),
        ]

    @staticmethod
    def create_search_item(text, path_text):
        """Create a non-editable search table cell."""
        item = QStandardItem(text)
        item.setEditable(False)
        item.setToolTip(path_text or text)
        item.setData(path_text, Qt.ItemDataRole.UserRole)
        return item

    def set_status_fields(self, fields):
        """Show compact status fields in one listWidget row."""
        status_text = "    |    ".join(str(field) for field in fields)
        self.ui.status_list.clear()
        self.ui.status_list.addItem(status_text)
        self.ui.status_list.item(0).setToolTip(status_text)

    def update_folder_info(self, folder_path):
        """Update the status row with current folder item counts."""
        files, folders, total = self.count_folder_entries(folder_path)
        self.set_status_fields(
            [
                f"Items: {total}",
                f"Files: {files}",
                f"Folders: {folders}",
            ]
        )

    @staticmethod
    def count_folder_entries(folder_path):
        """Count visible entries in a folder without mutating the Qt model."""
        files = 0
        folders = 0

        try:
            entries = list(Path(folder_path).iterdir())
        except OSError:
            return 0, 0, 0

        for entry in entries:
            if entry.is_dir():
                folders += 1
            else:
                files += 1

        return files, folders, len(entries)

    def sync_tree_to_path(self, folder_path):
        """Reveal and select a folder in the left tree."""
        tree_index = self.dir_model.index(folder_path)
        if not tree_index.isValid():
            return

        self.syncing_tree = True
        self.ui.tree_view.expand(tree_index)
        self.ui.tree_view.setCurrentIndex(tree_index)
        self.ui.tree_view.scrollTo(tree_index, QAbstractItemView.ScrollHint.EnsureVisible)
        self.syncing_tree = False

    def tree_path(self, index):
        """Return the folder path represented by a tree index."""
        return self.dir_model.filePath(index)

    def table_path(self, index):
        """Return the filesystem path represented by a table index."""
        if self.search_mode:
            return index.data(Qt.ItemDataRole.UserRole) or ""

        return self.file_model.filePath(index.sibling(index.row(), 0))

    def is_table_dir(self, index):
        """Return whether a table index points to a directory."""
        if self.search_mode:
            path_text = self.table_path(index)
            return bool(path_text) and Path(path_text).is_dir()

        return self.file_model.isDir(index.sibling(index.row(), 0))


class PreviewPanel:
    """
    Owns the right-side preview tables.

    The preview table shows metadata for the current file, folder, or search.
    The AI preview table is intentionally a placeholder for now, so the main
    window can reserve the UI area without mixing future AI behavior into the
    file-browser code.
    """

    def __init__(self, ui, file_types):
        """Create preview models and keep file type lookup available."""
        self.ui = ui
        self.file_types = file_types
        self.preview_model = QStandardItemModel(ui.window)
        self.ai_model = QStandardItemModel(ui.window)

    def setup(self):
        """Attach models and style preview tables."""
        self.ui.preview_view.setModel(self.preview_model)
        self.ui.ai_view.setModel(self.ai_model)

        for table_view in (self.ui.preview_view, self.ui.ai_view):
            table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table_view.horizontalHeader().setStretchLastSection(True)

        self.setup_preview_style()
        self.set_ai_placeholder()

    def setup_preview_style(self):
        """Make the preview table look like a compact property list."""
        self.ui.preview_view.horizontalHeader().setVisible(False)
        self.ui.preview_view.verticalHeader().setVisible(False)
        self.ui.preview_view.setShowGrid(False)
        self.ui.preview_view.setWordWrap(False)
        self.ui.preview_view.verticalHeader().setDefaultSectionSize(22)
        self.ui.preview_view.verticalHeader().setMinimumSectionSize(18)
        self.ui.preview_view.setStyleSheet(PREVIEW_TABLE_STYLE)

    def set_table_rows(self, model, headers, rows, value_tooltips=False):
        """Replace a table model with simple string rows."""
        model.clear()
        model.setHorizontalHeaderLabels(headers)
        for row in rows:
            items = [QStandardItem(str(value)) for value in row]
            if value_tooltips and len(items) > 1:
                items[1].setToolTip(str(row[1]))
            model.appendRow(items)

    def set_ai_placeholder(self):
        """Show a placeholder until AI preview is connected."""
        self.set_table_rows(
            self.ai_model,
            ["AI Preview"],
            [["AI preview placeholder"], ["No AI feature is connected yet."]],
        )

    def preview_path(self, path_text):
        """Show metadata for a selected file or folder."""
        path = Path(path_text).resolve(strict=False)
        rows = [
            ("Name", path.name or str(path)),
            ("Absolute path", str(path)),
        ]

        try:
            stat = path.stat()
        except OSError as error:
            rows.append(("Error", error))
            self.set_table_rows(
                self.preview_model,
                ["Field", "Value"],
                rows,
                value_tooltips=True,
            )
            return

        if path.is_dir():
            rows.extend(
                [
                    ("Type", "Folder"),
                    ("Modified", self.format_time(stat.st_mtime)),
                ]
            )
        else:
            rows.extend(
                [
                    ("Type", self.file_types.describe(path)),
                    ("Size", self.format_size(stat.st_size)),
                    ("Modified", self.format_time(stat.st_mtime)),
                ]
            )

        self.set_table_rows(
            self.preview_model,
            ["Field", "Value"],
            rows,
            value_tooltips=True,
        )

    @staticmethod
    def format_time(timestamp):
        """Format a filesystem timestamp for display."""
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def format_size(size_bytes):
        """Format byte counts using binary units."""
        units = ("Byte", "KB", "MB", "GB", "TB", "PB")
        size = float(size_bytes)

        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "Byte":
                    return f"{size_bytes} Byte"
                return f"{size:.2f} {unit}"
            size /= 1024

        return f"{size_bytes} Byte"


class NavigationHistory:
    """
    Stores back/forward navigation states.

    History entries can be folders or search result snapshots. Keeping search
    rows in history avoids rerunning Everything when the user navigates back to
    a previous search page, and keeps folder/search transitions consistent.
    """

    def __init__(self, default_path):
        """Start history at the default folder."""
        self.entries = [self.folder_entry(default_path)]
        self.index = 0

    @staticmethod
    def folder_entry(folder_path):
        """Create a history entry for a folder."""
        return {
            "type": "folder",
            "path": folder_path,
        }

    @staticmethod
    def search_entry(query, paths, message, total_results, result_limit):
        """Create a history entry for an Everything search."""
        return {
            "type": "search",
            "query": query,
            "paths": list(paths),
            "message": message,
            "total_results": total_results,
            "result_limit": result_limit,
        }

    def push_folder(self, folder_path):
        """Push a folder navigation state."""
        self.push(self.folder_entry(folder_path))

    def push_search(self, query, paths, message, total_results, result_limit):
        """Push a search result navigation state."""
        self.push(
            self.search_entry(query, paths, message, total_results, result_limit)
        )

    def push(self, entry):
        """Add a new state and discard forward history."""
        if self.entries[self.index] == entry:
            return

        del self.entries[self.index + 1 :]
        self.entries.append(entry)
        self.index = len(self.entries) - 1

    def can_go_back(self):
        """Return whether a previous history state exists."""
        return self.index > 0

    def can_go_forward(self):
        """Return whether a forward history state exists."""
        return self.index < len(self.entries) - 1

    def go_back(self):
        """Move back one history state."""
        if not self.can_go_back():
            return self.current()

        self.index -= 1
        return self.current()

    def go_forward(self):
        """Move forward one history state."""
        if not self.can_go_forward():
            return self.current()

        self.index += 1
        return self.current()

    def current(self):
        """Return the current history entry."""
        return self.entries[self.index]

    def current_text(self):
        """Return the text that should appear in the navigation bar."""
        entry = self.current()
        if entry["type"] == "folder":
            return entry["path"]

        return entry["query"]


class FileManagerWindow:
    """
    Top-level application controller.

    This class wires together UI loading, layout, browsing, preview, Everything
    search, and navigation history. It is the only class that connects Qt
    signals to user-facing actions; lower-level classes expose focused methods
    and do not know about the whole application.
    """

    def __init__(self):
        """Load UI, create feature controllers, and show the default folder."""
        self.window = load_ui()
        self.ui = UiElements.collect(self.window)
        self.file_types = FileTypeRegistry(FILE_TYPE_NAMES_PATH)
        self.everything = EverythingSdkSearch()
        self.layout = LayoutManager(self.ui)
        self.browser = FileBrowser(self.ui)
        self.preview = PreviewPanel(self.ui, self.file_types)
        self.history = NavigationHistory(DEFAULT_PATH)

        self.layout.setup()
        self.browser.setup()
        self.preview.setup()
        self.everything_startup_message = self.everything.start()
        self.connect_signals()
        self.navigate_to(DEFAULT_PATH, add_history=False)
        self.show_startup_status()

    def connect_signals(self):
        """Connect Qt signals to controller methods."""
        self.ui.tree_view.selectionModel().currentChanged.connect(self.show_files)
        self.ui.table_view.doubleClicked.connect(self.open_item)
        self.ui.table_view.clicked.connect(self.preview_selected)
        self.ui.back_button.clicked.connect(self.go_back)
        self.ui.forward_button.clicked.connect(self.go_forward)
        self.ui.search_button.clicked.connect(self.go_to_typed_path)
        self.ui.navigate_bar.returnPressed.connect(self.go_to_typed_path)
        self.ui.navigate_bar.editingFinished.connect(self.go_to_typed_path)

    def show_startup_status(self):
        """Display Everything startup errors in the status bar when available."""
        if self.everything_startup_message and hasattr(self.window, "statusBar"):
            self.window.statusBar().showMessage(self.everything_startup_message)

    def navigate_to(self, folder_path, add_history=True, sync_tree=True):
        """Navigate the browser to a folder path."""
        folder_path = QDir.cleanPath(QDir(folder_path).absolutePath())
        if not QDir(folder_path).exists():
            return

        self.browser.show_folder(folder_path)
        self.ui.navigate_bar.setText(folder_path)
        self.preview.preview_path(folder_path)

        if add_history:
            self.history.push_folder(folder_path)

        if sync_tree:
            self.browser.sync_tree_to_path(folder_path)

        self.update_history_buttons()

    def show_files(self, index, _previous=None):
        """Respond to folder selection changes in the tree."""
        if self.browser.syncing_tree:
            return

        self.navigate_to(self.browser.tree_path(index), sync_tree=False)

    def open_item(self, index):
        """Open a table item, entering folders or launching files."""
        item_path = self.browser.table_path(index)
        if not item_path:
            return

        if self.browser.is_table_dir(index):
            self.navigate_to(item_path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item_path))

    def preview_selected(self, index, _previous=None):
        """Refresh the preview panel for a clicked table row."""
        path_text = self.browser.table_path(index) if index.isValid() else ""
        if path_text:
            self.preview.preview_path(path_text)

    def update_history_buttons(self):
        """Enable or disable back/forward buttons."""
        self.ui.back_button.setEnabled(self.history.can_go_back())
        self.ui.forward_button.setEnabled(self.history.can_go_forward())

    def go_back(self):
        """Restore the previous history state."""
        if self.history.can_go_back():
            self.restore_history_entry(self.history.go_back())

    def go_forward(self):
        """Restore the next history state."""
        if self.history.can_go_forward():
            self.restore_history_entry(self.history.go_forward())

    def go_to_typed_path(self):
        """Navigate to a typed path or run an Everything search."""
        typed_path = self.ui.navigate_bar.text().strip()
        if not typed_path:
            self.ui.navigate_bar.setText(self.history.current_text())
            return

        path_text = QDir.cleanPath(QDir(typed_path).absolutePath())
        path = Path(path_text)

        if path.is_dir():
            self.navigate_to(path_text)
        elif path.is_file():
            self.navigate_to(str(path.parent))
            self.preview.preview_path(str(path))
        else:
            self.search_everything(typed_path)

    def search_everything(self, query, add_history=True):
        """Run an Everything search and optionally add it to history."""
        paths, message = self.everything.search(query)
        total_results = self.everything.total_result_count()
        self.show_search_state(query, paths, message, total_results)

        if add_history:
            self.history.push_search(
                query,
                paths,
                message,
                total_results,
                self.everything.result_limit,
            )
            self.update_history_buttons()

    def show_search_state(self, query, paths, message, total_results, result_limit=None):
        """Display search rows, status summary, and preview metadata."""
        result_limit = result_limit or self.everything.result_limit
        self.browser.show_search_results(paths, message, query)
        self.browser.set_status_fields(
            [
                f"Shown results: {len(paths)}",
                f"Total results: {total_results}",
                f"Limit: {result_limit}",
                f"Status: {message or 'Ready'}",
            ]
        )
        self.ui.navigate_bar.setText(query)
        self.preview.set_table_rows(
            self.preview.preview_model,
            ["Field", "Value"],
            [
                ("Engine", "Everything SDK"),
                ("Shown results", len(paths)),
                ("Total results", total_results),
                ("Limit", result_limit),
                ("Status", message or "Ready"),
            ],
            value_tooltips=True,
        )

    def restore_history_entry(self, entry):
        """Restore a folder or search history entry without pushing a duplicate."""
        if entry["type"] == "folder":
            self.navigate_to(entry["path"], add_history=False)
        elif entry["type"] == "search":
            self.show_search_state(
                entry["query"],
                entry["paths"],
                entry["message"],
                entry["total_results"],
                entry["result_limit"],
            )
            self.update_history_buttons()

    def show(self):
        """Show the loaded Qt window."""
        self.window.show()


def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    file_manager = FileManagerWindow()
    file_manager.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
