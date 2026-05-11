"""
PySide6 desktop frontend for the AI file manager.

The module is organized by UI responsibility: resource loading, display models,
widget collection, layout, browsing, preview, search display, history, and the
top-level window controller.
"""

import atexit
import html
import json
import mimetypes
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QDir,
    QFile,
    QFileInfo,
    QItemSelectionModel,
    QMimeData,
    Qt,
    QSortFilterProxyModel,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QDesktopServices,
    QKeySequence,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
    QTextDocument,
    QTextOption,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileIconProvider,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
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

from config_loader import Config
from mainFunctions import (
    AIFolderStore,
    EverythingSdkSearch,
    FileOperationService,
    FolderAnalysisStore,
    format_bytes,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = "C:/"
FILE_TYPE_NAMES_PATH = BASE_DIR / "file_type_names.json"
UI_PATH = BASE_DIR / "form.ui"
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


def style_preview_table(table_view):
    """Apply the shared compact preview-table style."""
    table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table_view.horizontalHeader().setVisible(False)
    table_view.horizontalHeader().setStretchLastSection(True)
    table_view.verticalHeader().setVisible(False)
    table_view.setShowGrid(False)
    table_view.setWordWrap(False)
    table_view.verticalHeader().setDefaultSectionSize(22)
    table_view.verticalHeader().setMinimumSectionSize(18)
    table_view.setStyleSheet(PREVIEW_TABLE_STYLE)


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

    This subclass keeps the normal filesystem data source while adding three UI
    details: multi-select check states, a stable modified-time display, and
    analysed folder sizes in the Size column.
    """

    MODIFIED_COLUMN = 3
    SIZE_COLUMN = 1
    TYPE_COLUMN = 2
    MODIFIED_FORMAT = "yyyy-MM-dd HH:mm:ss"

    def __init__(self, parent=None):
        """Create the file model and local UI state caches."""
        super().__init__(parent)
        self.checked_paths = set()
        self.folder_size_map = {}

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        """Return check states, formatted dates, analysed sizes, or Qt defaults."""
        if (
            index.isValid()
            and role == Qt.ItemDataRole.CheckStateRole
            and index.column() == 0
        ):
            path_text = self.filePath(index)
            if path_text in self.checked_paths:
                return Qt.CheckState.Checked

            return Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.DisplayRole and index.column() == self.MODIFIED_COLUMN:
            modified = self.fileInfo(index).lastModified()
            if modified.isValid():
                return modified.toString(self.MODIFIED_FORMAT)

        if role == Qt.ItemDataRole.DisplayRole and index.column() == self.SIZE_COLUMN:
            size_bytes = self.folder_size_bytes(index)
            if size_bytes is not None:
                return format_bytes(size_bytes)

        return super().data(index, role)

    def flags(self, index):
        """Make the Name column checkable for multi-select display."""
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable

        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        """Update the local checked-path cache when a checkbox changes."""
        if (
            index.isValid()
            and role == Qt.ItemDataRole.CheckStateRole
            and index.column() == 0
        ):
            path_text = self.filePath(index)
            if value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked:
                self.checked_paths.add(path_text)
            else:
                self.checked_paths.discard(path_text)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            return True

        return super().setData(index, value, role)

    def set_checked_paths(self, paths):
        """Replace the checked-path cache from the current table selection."""
        self.checked_paths = set(paths)

    def set_folder_sizes(self, folder_sizes):
        """Store analysed folder sizes keyed by normalized path."""
        self.folder_size_map = {
            self.normalized_path(path): size
            for path, size in folder_sizes.items()
        }

    def folder_size_bytes(self, index):
        """Return an analysed folder size for index when one is available."""
        name_index = index.sibling(index.row(), 0)
        if not name_index.isValid() or not self.isDir(name_index):
            return None

        return self.folder_size_map.get(self.normalized_path(self.filePath(name_index)))

    def size_sort_value(self, index):
        """Return a numeric byte value for sorting the Size column."""
        size_bytes = self.folder_size_bytes(index)
        if size_bytes is not None:
            return int(size_bytes)

        file_info = self.fileInfo(index.sibling(index.row(), 0))
        if file_info.isDir():
            return -1

        return file_info.size()

    def type_sort_value(self, index):
        """Return a stable type key for sorting the Type column."""
        name_index = index.sibling(index.row(), 0)
        file_info = self.fileInfo(name_index)
        if file_info.isDir():
            return "folder"

        suffix = file_info.completeSuffix() or file_info.suffix()
        if suffix:
            return suffix.casefold()

        display_type = self.data(index, Qt.ItemDataRole.DisplayRole) or ""
        return str(display_type).casefold()

    @staticmethod
    def normalized_path(path_text):
        """Normalize a path for case-insensitive table cache lookups."""
        return QDir.cleanPath(str(path_text)).casefold()


class FileSortProxyModel(QSortFilterProxyModel):
    """
    Sorts the file browser table using semantic values.

    QFileSystemModel does not know analysed recursive folder sizes, so this
    proxy lets the Size column sort by raw bytes while the table still displays
    formatted text such as MB or GB.
    """

    def __init__(self, parent=None):
        """Create a proxy that keeps folders grouped before files."""
        super().__init__(parent)
        self.current_sort_order = Qt.SortOrder.AscendingOrder

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """Remember sort order so folder grouping stays stable."""
        self.current_sort_order = order
        super().sort(column, order)

    def lessThan(self, left, right):
        """Compare two source indexes for table sorting."""
        source_model = self.sourceModel()
        left_name_index = left.sibling(left.row(), 0)
        right_name_index = right.sibling(right.row(), 0)
        left_is_dir = source_model.isDir(left_name_index)
        right_is_dir = source_model.isDir(right_name_index)

        if left_is_dir != right_is_dir:
            return self.folder_group_less_than(left_is_dir, right_is_dir)

        if left.column() == FileTableModel.SIZE_COLUMN:
            return self.compare_with_name_tiebreaker(
                source_model.size_sort_value(left),
                source_model.size_sort_value(right),
                left_name_index,
                right_name_index,
            )

        if left.column() == FileTableModel.TYPE_COLUMN:
            return self.compare_with_name_tiebreaker(
                source_model.type_sort_value(left),
                source_model.type_sort_value(right),
                left_name_index,
                right_name_index,
            )

        if left.column() == FileTableModel.MODIFIED_COLUMN:
            left_time = source_model.fileInfo(left_name_index).lastModified()
            right_time = source_model.fileInfo(right_name_index).lastModified()
            return self.compare_with_name_tiebreaker(
                left_time,
                right_time,
                left_name_index,
                right_name_index,
            )

        left_text = source_model.data(left, Qt.ItemDataRole.DisplayRole) or ""
        right_text = source_model.data(right, Qt.ItemDataRole.DisplayRole) or ""
        return self.compare_with_name_tiebreaker(
            str(left_text).casefold(),
            str(right_text).casefold(),
            left_name_index,
            right_name_index,
        )

    def folder_group_less_than(self, left_is_dir, right_is_dir):
        """Keep folders before files in both ascending and descending sorts."""
        if self.current_sort_order == Qt.SortOrder.DescendingOrder:
            return not left_is_dir and right_is_dir

        return left_is_dir and not right_is_dir

    def compare_with_name_tiebreaker(
        self,
        left_value,
        right_value,
        left_name_index,
        right_name_index,
    ):
        """Compare semantic values, falling back to names when values match."""
        if left_value == right_value:
            return self.compare_text(
                left_name_index.data(Qt.ItemDataRole.DisplayRole) or "",
                right_name_index.data(Qt.ItemDataRole.DisplayRole) or "",
            )

        return left_value < right_value

    @staticmethod
    def compare_text(left_text, right_text):
        """Compare display text case-insensitively."""
        return str(left_text).casefold() < str(right_text).casefold()


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
    analyse_button: QPushButton
    new_ai_folder_button: QPushButton
    back_button: QPushButton
    forward_button: QPushButton
    undo_button: QPushButton
    redo_button: QPushButton
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
            analyse_button=window.findChild(QPushButton, "analyse"),
            new_ai_folder_button=window.findChild(QPushButton, "newaifolder"),
            back_button=window.findChild(QPushButton, "back"),
            forward_button=window.findChild(QPushButton, "forward"),
            undo_button=window.findChild(QPushButton, "undo"),
            redo_button=window.findChild(QPushButton, "redo"),
            navigate_bar=window.findChild(QLineEdit, "navigateBar"),
            search_button=window.findChild(QPushButton, "search"),
            preview_view=window.findChild(QTableView, "preview"),
            ai_view=window.findChild(QTableView, "AIview"),
            side_buttons=[
                window.findChild(QPushButton, object_name)
                for object_name in (
                    "analyse",
                    "newaifolder",
                    "pushButton_3",
                    "pushButton_4",
                    "pushButton_5",
                    "pushButton_6",
                    "pushButton_7",
                )
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

    SIDE_PANEL_WIDTH = 130
    PANEL_SPACING = 6
    STATUS_ROW_HEIGHT = 24
    STATUS_BUTTON_WIDTH = 70
    TOOLBAR_BUTTON_MIN_WIDTH = 82

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
        status_row = self.create_status_row()

        file_splitter = QSplitter(Qt.Orientation.Vertical, self.ui.central_widget)
        file_splitter.addWidget(self.ui.table_view)
        file_splitter.addWidget(status_row)
        file_splitter.setStretchFactor(0, 1)
        file_splitter.setStretchFactor(1, 0)
        file_splitter.setSizes([500, self.STATUS_ROW_HEIGHT])
        file_splitter.setHandleWidth(1)
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

    def create_status_row(self):
        """Create the compact undo, redo, and folder-info row."""
        status_row = QWidget(self.ui.central_widget)
        status_row.setFixedHeight(self.STATUS_ROW_HEIGHT)
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(2)
        status_layout.addWidget(self.ui.undo_button)
        status_layout.addWidget(self.ui.redo_button)
        status_layout.addWidget(self.ui.status_list, 1)
        return status_row

    def apply_sizes(self, side_panel):
        """Apply minimum and fixed sizes after widgets enter splitters."""
        side_panel.setFixedWidth(self.SIDE_PANEL_WIDTH)
        for button in self.ui.side_buttons:
            if button is not None:
                button.setMinimumWidth(self.SIDE_PANEL_WIDTH - 12)

        for button in (
            self.ui.back_button,
            self.ui.forward_button,
            self.ui.search_button,
        ):
            button.setMinimumWidth(self.TOOLBAR_BUTTON_MIN_WIDTH)

        self.ui.tree_view.setMinimumWidth(180)
        self.ui.table_view.setMinimumWidth(260)
        self.ui.status_list.setFixedHeight(self.STATUS_ROW_HEIGHT)
        self.ui.undo_button.setFixedHeight(self.STATUS_ROW_HEIGHT)
        self.ui.redo_button.setFixedHeight(self.STATUS_ROW_HEIGHT)
        self.ui.undo_button.setFixedWidth(self.STATUS_BUTTON_WIDTH)
        self.ui.redo_button.setFixedWidth(self.STATUS_BUTTON_WIDTH)
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

    ARRANGE_COLUMNS = {
        "name": 0,
        "filename": 0,
        "file_name": 0,
        "size": 1,
        "type": 2,
        "kind": 2,
        "date": 3,
        "modified": 3,
        "date_modified": 3,
        "time": 3,
    }

    def __init__(self, ui, default_arrange="name_asc"):
        """Create filesystem/search models used by the browser area."""
        self.ui = ui
        self.default_arrange = default_arrange
        self.search_mode = False
        self.syncing_tree = False
        self.dir_model = QFileSystemModel(ui.window)
        self.file_model = FileTableModel(ui.window)
        self.file_proxy_model = FileSortProxyModel(ui.window)
        self.icon_provider = QFileIconProvider()
        self.search_model = QStandardItemModel(ui.window)
        self.table_delegate = CharacterElideDelegate(ui.table_view)
        self.syncing_selection_checks = False
        self.connected_table_selection_model = None
        self.file_proxy_model.setSourceModel(self.file_model)
        self.file_proxy_model.setDynamicSortFilter(True)
        self.file_model.dataChanged.connect(self.apply_file_check_selection)
        self.search_model.itemChanged.connect(self.apply_search_check_selection)

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

        self.ui.table_view.setModel(self.file_proxy_model)
        self.ui.table_view.setRootIndex(
            self.file_proxy_model.mapFromSource(self.file_model.index(DEFAULT_PATH))
        )
        self.connect_table_selection()
        self.apply_default_arrange()
        self.update_folder_info(DEFAULT_PATH)

    def apply_default_arrange(self):
        """Apply the startup sort mode from config.json."""
        column, order = self.parse_arrange(self.default_arrange)
        self.ui.table_view.sortByColumn(column, order)
        self.file_proxy_model.sort(column, order)

    @classmethod
    def parse_arrange(cls, arrange_text):
        """Parse a sort mode string into a table column and Qt sort order."""
        text = str(arrange_text or "").strip().lower().replace("-", "_")
        if not text:
            text = "name_asc"

        order = Qt.SortOrder.AscendingOrder
        for suffix in ("_desc", "_descending"):
            if text.endswith(suffix):
                order = Qt.SortOrder.DescendingOrder
                text = text[: -len(suffix)]
                break

        for suffix in ("_asc", "_ascending"):
            if text.endswith(suffix):
                order = Qt.SortOrder.AscendingOrder
                text = text[: -len(suffix)]
                break

        if text in ("latest", "newest", "recent"):
            return FileTableModel.MODIFIED_COLUMN, Qt.SortOrder.DescendingOrder
        if text in ("oldest",):
            return FileTableModel.MODIFIED_COLUMN, Qt.SortOrder.AscendingOrder
        if text in ("largest", "biggest"):
            return FileTableModel.SIZE_COLUMN, Qt.SortOrder.DescendingOrder
        if text in ("smallest",):
            return FileTableModel.SIZE_COLUMN, Qt.SortOrder.AscendingOrder

        return cls.ARRANGE_COLUMNS.get(text, 0), order

    def setup_table_view(self):
        """Configure shared behavior for folder and search tables."""
        self.ui.table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ui.table_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.ui.table_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
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

    def connect_table_selection(self):
        """Connect the active table selection model to checkbox syncing."""
        selection_model = self.ui.table_view.selectionModel()
        if selection_model is None:
            return

        if selection_model is self.connected_table_selection_model:
            return

        if self.connected_table_selection_model is not None:
            try:
                self.connected_table_selection_model.selectionChanged.disconnect(
                    self.sync_checks_to_selection
                )
            except (RuntimeError, TypeError):
                pass

        selection_model.selectionChanged.connect(self.sync_checks_to_selection)
        self.connected_table_selection_model = selection_model

    def sync_checks_to_selection(self, *_args):
        """Mirror selected table rows into checkboxes for both table modes."""
        if self.syncing_selection_checks:
            return

        self.syncing_selection_checks = True
        try:
            selected_paths = self.selected_table_paths()
            if self.search_mode:
                self.sync_search_checks(selected_paths)
            else:
                self.file_model.set_checked_paths(selected_paths)
                self.refresh_file_check_column()
        finally:
            self.syncing_selection_checks = False

    def selected_table_paths(self):
        """Return unique filesystem paths from selected table rows."""
        selection_model = self.ui.table_view.selectionModel()
        if selection_model is None:
            return []

        paths = []
        for index in selection_model.selectedRows(0):
            path_text = self.table_path(index)
            if path_text:
                paths.append(path_text)

        return paths

    def refresh_file_check_column(self):
        """Repaint the checkbox column for the current folder root."""
        root_index = self.current_file_root_index()
        row_count = self.file_model.rowCount(root_index)
        if row_count <= 0:
            return

        top_left = self.file_model.index(0, 0, root_index)
        bottom_right = self.file_model.index(row_count - 1, 0, root_index)
        self.file_model.dataChanged.emit(
            top_left,
            bottom_right,
            [Qt.ItemDataRole.CheckStateRole],
        )

    def set_folder_size_map(self, folder_sizes):
        """Apply analysed child-folder sizes to the regular file table."""
        self.file_model.set_folder_sizes(folder_sizes)
        self.refresh_folder_size_column()
        self.resort_folder_table()

    def refresh_folder_size_column(self):
        """Repaint the Size column after analysed folder sizes change."""
        root_index = self.current_file_root_index()
        row_count = self.file_model.rowCount(root_index)
        if row_count <= 0:
            return

        top_left = self.file_model.index(0, FileTableModel.SIZE_COLUMN, root_index)
        bottom_right = self.file_model.index(
            row_count - 1,
            FileTableModel.SIZE_COLUMN,
            root_index,
        )
        self.file_model.dataChanged.emit(
            top_left,
            bottom_right,
            [Qt.ItemDataRole.DisplayRole],
        )

    def current_file_root_index(self):
        """Return the source-model root index for the current folder table."""
        if self.ui.table_view.model() is self.file_proxy_model:
            return self.file_proxy_model.mapToSource(self.ui.table_view.rootIndex())

        return self.ui.table_view.rootIndex()

    def resort_folder_table(self):
        """Re-run folder table sorting after size data changes."""
        if self.search_mode or self.ui.table_view.model() is not self.file_proxy_model:
            return

        header = self.ui.table_view.horizontalHeader()
        self.file_proxy_model.sort(
            header.sortIndicatorSection(),
            header.sortIndicatorOrder(),
        )

    def sync_search_checks(self, selected_paths):
        """Mirror selected search-result rows into search result checkboxes."""
        selected_path_set = {QDir.cleanPath(path).casefold() for path in selected_paths}
        for row in range(self.search_model.rowCount()):
            item = self.search_model.item(row, 0)
            if item is None or not item.isCheckable():
                continue
            path_text = item.data(Qt.ItemDataRole.UserRole) or ""
            check_state = Qt.CheckState.Checked
            if QDir.cleanPath(path_text).casefold() not in selected_path_set:
                check_state = Qt.CheckState.Unchecked
            item.setCheckState(check_state)

    def apply_file_check_selection(self, top_left, _bottom_right, roles=None):
        """Select or deselect one folder-mode row when its checkbox changes."""
        if self.syncing_selection_checks:
            return

        if roles:
            role_values = {
                role.value if hasattr(role, "value") else role
                for role in roles
            }
            if Qt.ItemDataRole.CheckStateRole.value not in role_values:
                return

        if top_left.column() != 0:
            return

        checked = self.file_model.data(top_left, Qt.ItemDataRole.CheckStateRole)
        self.set_table_row_selected(
            self.file_proxy_model.mapFromSource(top_left),
            checked == Qt.CheckState.Checked,
        )

    def apply_search_check_selection(self, item):
        """Select or deselect one search-mode row when its checkbox changes."""
        if self.syncing_selection_checks:
            return

        if item.column() != 0:
            return

        path_text = item.data(Qt.ItemDataRole.UserRole) or ""
        if not path_text:
            return

        index = self.search_model.indexFromItem(item)
        self.set_table_row_selected(index, item.checkState() == Qt.CheckState.Checked)

    def set_table_row_selected(self, index, selected):
        """Apply row selection state to the table selection model."""
        selection_model = self.ui.table_view.selectionModel()
        if selection_model is None or not index.isValid():
            return

        flags = QItemSelectionModel.SelectionFlag.Rows
        if selected:
            flags |= QItemSelectionModel.SelectionFlag.Select
            self.ui.table_view.setCurrentIndex(index)
        else:
            flags |= QItemSelectionModel.SelectionFlag.Deselect

        selection_model.select(index, flags)

    def is_row_selected(self, index):
        """Return whether the row containing index is currently selected."""
        selection_model = self.ui.table_view.selectionModel()
        if selection_model is None or not index.isValid():
            return False

        return selection_model.isRowSelected(index.row(), index.parent())

    def select_single_row(self, index):
        """Clear selection and select only the row containing index."""
        selection_model = self.ui.table_view.selectionModel()
        if selection_model is None or not index.isValid():
            return

        row_index = index.sibling(index.row(), 0)
        self.ui.table_view.setCurrentIndex(row_index)
        selection_model.select(
            row_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.sync_checks_to_selection()

    def show_folder(self, folder_path):
        """Display a folder with the QFileSystemModel table."""
        self.clear_table_selection()
        self.search_mode = False
        self.file_model.set_folder_sizes({})
        self.table_delegate.set_highlight_query("")
        self.ui.table_view.setModel(self.file_proxy_model)
        self.configure_folder_columns()
        root_index = self.file_model.setRootPath(folder_path)
        self.ui.table_view.setRootIndex(self.file_proxy_model.mapFromSource(root_index))
        self.connect_table_selection()
        self.sync_checks_to_selection()
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
        self.clear_table_selection()
        self.search_mode = True
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
        self.connect_table_selection()
        self.configure_search_columns()
        self.sync_checks_to_selection()

    def current_table_path(self):
        """Return the current table row path, if any."""
        index = self.ui.table_view.currentIndex()
        if not index.isValid():
            return ""

        return self.table_path(index)

    def current_folder_path(self):
        """Return the folder currently shown in regular browser mode."""
        if self.search_mode:
            return ""

        return self.file_model.filePath(self.current_file_root_index())

    def select_all_rows(self):
        """Select every visible table row and sync checkboxes."""
        self.ui.table_view.selectAll()
        self.sync_checks_to_selection()

    def restore_table_position(self, path_text):
        """Restore selection and scroll position for a path."""
        self.select_table_path(path_text, attempts=6)

    def select_table_path(self, path_text, attempts=0):
        """Select a path in the active table, retrying while Qt loads rows."""
        if not path_text:
            return False

        if self.search_mode:
            index = self.search_table_index(path_text)
        else:
            index = self.folder_table_index(path_text)

        if index is not None and index.isValid():
            self.ui.table_view.setCurrentIndex(index)
            self.ui.table_view.selectRow(index.row())
            self.ui.table_view.scrollTo(
                index,
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
            return True

        if attempts > 0:
            QTimer.singleShot(
                50,
                lambda: self.select_table_path(path_text, attempts - 1),
            )

        return False

    def folder_table_index(self, path_text):
        """Return the proxy-model table index for a child path in folder mode."""
        root_path = QDir.cleanPath(self.file_model.filePath(self.current_file_root_index()))
        parent_path = QDir.cleanPath(str(Path(path_text).parent))
        if not self.same_path(root_path, parent_path):
            return None

        index = self.file_model.index(path_text)
        if not index.isValid():
            return None

        return self.file_proxy_model.mapFromSource(index.sibling(index.row(), 0))

    def search_table_index(self, path_text):
        """Return the search-model index for a result path."""
        for row in range(self.search_model.rowCount()):
            index = self.search_model.index(row, 0)
            if self.same_path(index.data(Qt.ItemDataRole.UserRole) or "", path_text):
                return index

        return None

    @staticmethod
    def same_path(left, right):
        """Compare paths using Qt's clean path format and case folding."""
        return QDir.cleanPath(str(left)).casefold() == QDir.cleanPath(str(right)).casefold()

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
        name_item = self.create_search_item(name, path_text, checkable=True)
        path_item = self.create_search_item(parent, path_text)
        name_item.setIcon(self.icon_provider.icon(QFileInfo(path_text)))
        return [name_item, path_item]

    @staticmethod
    def create_search_row(name, path_text):
        """Create a generic two-column search row."""
        return [
            FileBrowser.create_search_item(name, path_text, checkable=bool(path_text)),
            FileBrowser.create_search_item(path_text, path_text),
        ]

    @staticmethod
    def create_search_item(text, path_text, checkable=False):
        """Create a non-editable search table cell."""
        item = QStandardItem(text)
        item.setEditable(False)
        item.setToolTip(path_text or text)
        item.setData(path_text, Qt.ItemDataRole.UserRole)
        if checkable and path_text:
            item.setCheckable(True)
            item.setCheckState(Qt.CheckState.Unchecked)
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

        return self.file_model.filePath(self.folder_source_index(index))

    def is_table_dir(self, index):
        """Return whether a table index points to a directory."""
        if self.search_mode:
            path_text = self.table_path(index)
            return bool(path_text) and Path(path_text).is_dir()

        return self.file_model.isDir(self.folder_source_index(index))

    def folder_source_index(self, index):
        """Map a visible folder-mode table index back to the source model."""
        return self.file_proxy_model.mapToSource(index.sibling(index.row(), 0))


class PreviewPanel:
    """
    Owns the right-side file preview table.

    The preview table shows metadata for the current file, folder, or search.
    """

    def __init__(self, ui, file_types):
        """Create the preview model and keep file type lookup available."""
        self.ui = ui
        self.file_types = file_types
        self.preview_model = QStandardItemModel(ui.window)

    def setup(self):
        """Attach the model and style the preview table."""
        self.ui.preview_view.setModel(self.preview_model)
        style_preview_table(self.ui.preview_view)

    def set_table_rows(self, model, headers, rows, value_tooltips=False):
        """Replace all rows in a preview-style table model."""
        model.clear()
        model.setHorizontalHeaderLabels(headers)
        self.append_table_rows(model, rows, value_tooltips)

    def append_table_rows(self, model, rows, value_tooltips=False):
        """Append rows to a preview-style table model."""
        for row in rows:
            items = [QStandardItem(str(value)) for value in row]
            if value_tooltips and len(items) > 1:
                items[1].setToolTip(str(row[1]))
            model.appendRow(items)

    def append_preview_rows(self, rows, value_tooltips=False):
        """Append rows to the main preview table."""
        self.append_table_rows(self.preview_model, rows, value_tooltips)

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


class AIPreviewPanel:
    """
    Owns the AI preview area.

    This class is intentionally only a placeholder for now. Future AI preview
    behavior can be added here without mixing it into the normal file preview.
    """

    def __init__(self, ui):
        """Create the placeholder model for the AI preview view."""
        self.ui = ui
        self.model = QStandardItemModel(ui.window)

    def setup(self):
        """Attach the placeholder model and basic read-only table behavior."""
        self.ui.ai_view.setModel(self.model)
        style_preview_table(self.ui.ai_view)
        self.set_placeholder()

    def set_placeholder(self):
        """Show static placeholder text until AI preview is implemented."""
        self.model.clear()
        self.model.setHorizontalHeaderLabels(["AI Preview"])
        for text in ("AI preview placeholder", "No AI feature is connected yet."):
            self.model.appendRow([QStandardItem(text)])


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
    def folder_entry(folder_path, selected_path=""):
        """Create a history entry for a folder."""
        return {
            "type": "folder",
            "path": folder_path,
            "selected_path": selected_path,
        }

    @staticmethod
    def search_entry(query, paths, message, total_results, result_limit, selected_path=""):
        """Create a history entry for an Everything search."""
        return {
            "type": "search",
            "query": query,
            "paths": list(paths),
            "message": message,
            "total_results": total_results,
            "result_limit": result_limit,
            "selected_path": selected_path,
        }

    def push_folder(self, folder_path, selected_path=""):
        """Push a folder navigation state."""
        self.push(self.folder_entry(folder_path, selected_path))

    def push_search(
        self,
        query,
        paths,
        message,
        total_results,
        result_limit,
        selected_path="",
    ):
        """Push a search result navigation state."""
        self.push(
            self.search_entry(
                query,
                paths,
                message,
                total_results,
                result_limit,
                selected_path,
            )
        )

    def push(self, entry):
        """Add a new state and discard forward history."""
        current_entry = self.entries[self.index]
        if self.same_target(current_entry, entry):
            if entry.get("selected_path"):
                current_entry["selected_path"] = entry["selected_path"]
            return

        del self.entries[self.index + 1 :]
        self.entries.append(entry)
        self.index = len(self.entries) - 1

    def update_current_selected_path(self, selected_path):
        """Store the latest selected row path on the current history entry."""
        self.entries[self.index]["selected_path"] = selected_path or ""

    @staticmethod
    def same_target(left, right):
        """Return whether two history entries point to the same state."""
        if left["type"] != right["type"]:
            return False

        if left["type"] == "folder":
            return left["path"] == right["path"]

        return left["query"] == right["query"] and left["paths"] == right["paths"]

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
        self.config = Config()
        self.file_types = FileTypeRegistry(FILE_TYPE_NAMES_PATH)
        self.everything = EverythingSdkSearch()
        self.layout = LayoutManager(self.ui)
        self.browser = FileBrowser(self.ui, self.config.get_default_arrange())
        self.preview = PreviewPanel(self.ui, self.file_types)
        self.ai_preview = AIPreviewPanel(self.ui)
        self.history = NavigationHistory(DEFAULT_PATH)
        self.analysis_store = FolderAnalysisStore()
        self.ai_folder_store = AIFolderStore()
        self.ai_folder_store.cleanup_missing_records()
        self.file_operations = FileOperationService()
        self.clipboard_paths = []
        self.clipboard_move = False
        self.shortcuts = []
        self.undo_stack = []
        self.redo_stack = []
        self.status_restore_token = 0

        self.layout.setup()
        self.browser.setup()
        self.preview.setup()
        self.ai_preview.setup()
        self.everything_startup_message = self.everything.start()
        self.connect_signals()
        self.navigate_to(DEFAULT_PATH, add_history=False)
        self.update_operation_buttons()
        self.show_startup_status()

    def connect_signals(self):
        """Connect Qt signals to controller methods."""
        self.ui.tree_view.selectionModel().currentChanged.connect(self.show_files)
        self.ui.table_view.doubleClicked.connect(self.open_item)
        self.ui.table_view.clicked.connect(self.preview_selected)
        self.ui.analyse_button.clicked.connect(self.analyse_selected_folder)
        if self.ui.new_ai_folder_button is not None:
            self.ui.new_ai_folder_button.clicked.connect(self.create_new_ai_folder)
        self.ui.back_button.clicked.connect(self.go_back)
        self.ui.forward_button.clicked.connect(self.go_forward)
        self.ui.undo_button.clicked.connect(self.undo_file_operation)
        self.ui.redo_button.clicked.connect(self.redo_file_operation)
        self.ui.search_button.clicked.connect(self.go_to_typed_path)
        self.ui.navigate_bar.returnPressed.connect(self.go_to_typed_path)
        self.ui.navigate_bar.editingFinished.connect(self.go_to_typed_path)
        self.setup_shortcuts()
        self.setup_context_menu()

    def setup_shortcuts(self):
        """Install keyboard shortcuts for common file-browser actions."""
        self.add_table_shortcut("Ctrl+C", self.copy_selected_files)
        self.add_table_shortcut("Ctrl+X", self.cut_selected_files)
        self.add_table_shortcut("Ctrl+V", self.paste_files)
        self.add_table_shortcut("Ctrl+A", self.select_all_files)
        self.add_table_shortcut("Delete", self.delete_selected_files)
        self.add_table_shortcut("Ctrl+Z", self.undo_file_operation)
        self.add_table_shortcut("Ctrl+Y", self.redo_file_operation)
        self.add_table_shortcut("Ctrl+Shift+Z", self.redo_file_operation)

    def add_table_shortcut(self, key_sequence, handler):
        """Register one shortcut on the central table view."""
        shortcut = QShortcut(QKeySequence(key_sequence), self.ui.table_view)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(handler)
        self.shortcuts.append(shortcut)

    def setup_context_menu(self):
        """Enable right-click menus for the central table and folder tree."""
        self.ui.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ui.table_view.customContextMenuRequested.connect(
            self.show_file_context_menu
        )
        self.ui.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ui.tree_view.customContextMenuRequested.connect(
            self.show_tree_context_menu
        )

    def show_startup_status(self):
        """Display Everything startup errors in the status bar when available."""
        if self.everything_startup_message and hasattr(self.window, "statusBar"):
            self.window.statusBar().showMessage(self.everything_startup_message)

    def navigate_to(
        self,
        folder_path,
        add_history=True,
        sync_tree=True,
        selected_path="",
    ):
        """Navigate the browser to a folder path."""
        folder_path = QDir.cleanPath(QDir(folder_path).absolutePath())
        if not QDir(folder_path).exists():
            return

        if add_history:
            self.save_current_browser_position()

        self.browser.show_folder(folder_path)
        self.update_folder_size_cache(folder_path)
        self.ui.navigate_bar.setText(folder_path)
        self.preview_path_with_analysis(folder_path)

        if add_history:
            self.history.push_folder(folder_path, selected_path)

        if sync_tree:
            self.browser.sync_tree_to_path(folder_path)

        self.restore_saved_table_position({"selected_path": selected_path})
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
            self.save_current_browser_position()
            QDesktopServices.openUrl(QUrl.fromLocalFile(item_path))

    def preview_selected(self, index, _previous=None):
        """Refresh the preview panel for a clicked table row."""
        path_text = self.browser.table_path(index) if index.isValid() else ""
        if path_text:
            self.history.update_current_selected_path(path_text)
            self.preview_path_with_analysis(path_text)

    def update_folder_size_cache(self, folder_path):
        """Load analysed child-folder sizes into the browser table."""
        self.browser.set_folder_size_map(
            self.analysis_store.child_folder_size_map(folder_path)
        )

    def show_file_context_menu(self, position):
        """Build and execute the context menu for the central table."""
        index = self.ui.table_view.indexAt(position)
        paths = []
        if index.isValid():
            if not self.browser.is_row_selected(index):
                self.browser.select_single_row(index)
            self.preview_selected(index)
            paths = self.selected_paths_for_shortcut()

        menu = QMenu(self.window)
        actions = {}

        if paths:
            if len(paths) == 1:
                actions[menu.addAction("Open")] = self.open_selected_item
                menu.addSeparator()

            if self.can_undo_file_operation():
                actions[menu.addAction("Undo")] = self.undo_file_operation
            if self.can_redo_file_operation():
                actions[menu.addAction("Redo")] = self.redo_file_operation
            if self.can_undo_file_operation() or self.can_redo_file_operation():
                menu.addSeparator()

            actions[menu.addAction("Copy")] = self.copy_selected_files
            actions[menu.addAction("Cut")] = self.cut_selected_files
            actions[menu.addAction("Delete")] = self.delete_selected_files
            menu.addSeparator()
        else:
            if self.can_undo_file_operation():
                actions[menu.addAction("Undo")] = self.undo_file_operation
            if self.can_redo_file_operation():
                actions[menu.addAction("Redo")] = self.redo_file_operation
            if self.can_undo_file_operation() or self.can_redo_file_operation():
                menu.addSeparator()

        if self.can_paste_files():
            actions[menu.addAction("Paste")] = self.paste_files

        actions[menu.addAction("Refresh")] = self.refresh_browser
        actions[menu.addAction("Select All")] = self.select_all_files

        action = menu.exec(self.ui.table_view.viewport().mapToGlobal(position))
        handler = actions.get(action)
        if handler is not None:
            handler()

    def show_tree_context_menu(self, position):
        """Build and execute the context menu for one folder-tree item."""
        index = self.ui.tree_view.indexAt(position)
        if not index.isValid():
            return

        folder_path = self.browser.tree_path(index)
        if not folder_path:
            return

        self.ui.tree_view.setCurrentIndex(index)
        self.preview_path_with_analysis(folder_path)

        is_drive_root = self.is_drive_root_path(folder_path)
        menu = QMenu(self.window)
        actions = {}

        if not is_drive_root:
            actions[menu.addAction("Open")] = lambda: self.navigate_to(folder_path)
            menu.addSeparator()

        if self.can_undo_file_operation():
            actions[menu.addAction("Undo")] = self.undo_file_operation
        if self.can_redo_file_operation():
            actions[menu.addAction("Redo")] = self.redo_file_operation
        if self.can_undo_file_operation() or self.can_redo_file_operation():
            menu.addSeparator()

        if not is_drive_root:
            actions[menu.addAction("Copy")] = lambda: self.copy_selected_files([folder_path])
            actions[menu.addAction("Cut")] = lambda: self.cut_selected_files([folder_path])
            actions[menu.addAction("Delete")] = lambda: self.delete_selected_files([folder_path])
            menu.addSeparator()

            if self.can_paste_files_to(folder_path):
                actions[menu.addAction("Paste")] = lambda: self.paste_files(folder_path)

        actions[menu.addAction("Refresh")] = self.refresh_browser

        action = menu.exec(self.ui.tree_view.viewport().mapToGlobal(position))
        handler = actions.get(action)
        if handler is not None:
            handler()

    def open_selected_item(self):
        """Open the current table row from the context menu."""
        index = self.ui.table_view.currentIndex()
        if index.isValid():
            self.open_item(index)

    def selected_paths_for_shortcut(self):
        """Return selected paths, falling back to the current row."""
        paths = self.browser.selected_table_paths()
        if not paths:
            current_path = self.browser.current_table_path()
            if current_path:
                paths = [current_path]

        return list(dict.fromkeys(paths))

    def copy_selected_files(self, paths=None):
        """Copy selected paths into the app and system clipboard."""
        paths = list(paths) if paths is not None else self.selected_paths_for_shortcut()
        if not paths:
            self.show_temporary_status(["No files selected to copy."])
            return

        self.set_file_clipboard(paths, move=False)
        self.show_temporary_status([f"Copied to clipboard: {len(paths)}"])

    def cut_selected_files(self, paths=None):
        """Mark selected paths for move paste."""
        paths = list(paths) if paths is not None else self.selected_paths_for_shortcut()
        if not paths:
            self.show_temporary_status(["No files selected to cut."])
            return

        self.set_file_clipboard(paths, move=True)
        self.show_temporary_status([f"Cut to clipboard: {len(paths)}"])

    def paste_files(self, destination=None):
        """Paste copied or cut paths into the current folder."""
        destination = destination or self.current_paste_destination()
        if not destination:
            self.show_temporary_status(["Paste is only available in a folder."])
            return

        paths, move = self.file_clipboard_contents()
        if not paths:
            self.show_temporary_status(["Clipboard has no files to paste."])
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = self.file_operations.paste(paths, destination, move=move)
        finally:
            QApplication.restoreOverrideCursor()

        if move and not result["errors"]:
            self.clipboard_paths = []
            self.clipboard_move = False

        self.record_file_operation("move" if move else "copy", result)
        self.browser.update_folder_info(destination)
        self.show_paste_result(result, move)

    def select_all_files(self):
        """Select all rows in the active table."""
        self.browser.select_all_rows()

    def delete_selected_files(self, paths=None):
        """Move selected paths to the app trash so the action can be undone."""
        paths = list(paths) if paths is not None else self.selected_paths_for_shortcut()
        if not paths:
            self.show_temporary_status(["No files selected to delete."])
            return

        paths = [path for path in paths if not self.is_drive_root_path(path)]
        if not paths:
            self.show_temporary_status(["Drive roots cannot be deleted."])
            return

        if not self.confirm_delete(paths):
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = self.file_operations.delete_for_undo(paths)
        finally:
            QApplication.restoreOverrideCursor()

        result["ai_folder_records"] = self.ai_folder_store.delete_records_for_paths(
            result["done"]
        )
        self.remove_deleted_clipboard_paths(result["done"])
        self.record_file_operation("delete", result)
        self.refresh_browser()
        self.show_delete_result(result)

    def confirm_delete(self, paths):
        """Ask for confirmation before deleting selected paths."""
        item_text = "item" if len(paths) == 1 else "items"
        message = f"Delete {len(paths)} selected {item_text}?"
        reply = QMessageBox.question(
            self.window,
            "Delete",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def record_file_operation(self, action, result):
        """Push a successful file operation onto the undo stack."""
        operations = result.get("operations", [])
        if not operations:
            self.update_operation_buttons()
            return

        self.undo_stack.append(
            {
                "action": action,
                "operations": operations,
                "ai_folder_records": result.get("ai_folder_records", []),
            }
        )
        self.redo_stack.clear()
        self.update_operation_buttons()

    def undo_file_operation(self):
        """Undo the most recent file operation batch."""
        if not self.undo_stack:
            self.show_temporary_status(["Nothing to undo."])
            return

        entry = self.undo_stack.pop()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = self.file_operations.undo(entry["operations"])
        finally:
            QApplication.restoreOverrideCursor()

        if result["errors"]:
            self.undo_stack.append(entry)
        else:
            self.restore_ai_folder_records_for_undo(entry)
            self.redo_stack.append(entry)

        self.refresh_browser()
        self.update_operation_buttons()
        self.show_operation_result("Undo", result)

    def redo_file_operation(self):
        """Redo the most recently undone file operation batch."""
        if not self.redo_stack:
            self.show_temporary_status(["Nothing to redo."])
            return

        entry = self.redo_stack.pop()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            result = self.file_operations.redo(entry["operations"])
        finally:
            QApplication.restoreOverrideCursor()

        if result["errors"]:
            self.redo_stack.append(entry)
        else:
            self.remove_ai_folder_records_for_redo(entry)
            self.undo_stack.append(entry)

        self.refresh_browser()
        self.update_operation_buttons()
        self.show_operation_result("Redo", result)

    def restore_ai_folder_records_for_undo(self, entry):
        """Restore AI-folder database rows after undoing a delete."""
        if entry.get("action") != "delete":
            return

        self.ai_folder_store.restore_records(entry.get("ai_folder_records", []))

    def remove_ai_folder_records_for_redo(self, entry):
        """Remove AI-folder database rows after redoing a delete."""
        if entry.get("action") != "delete":
            return

        records = entry.get("ai_folder_records", [])
        self.ai_folder_store.delete_records_for_paths(
            record["folder_path"]
            for record in records
        )

    def can_undo_file_operation(self):
        """Return whether undo is currently available."""
        return bool(self.undo_stack)

    def can_redo_file_operation(self):
        """Return whether redo is currently available."""
        return bool(self.redo_stack)

    def update_operation_buttons(self):
        """Enable or disable undo and redo buttons."""
        self.ui.undo_button.setEnabled(self.can_undo_file_operation())
        self.ui.redo_button.setEnabled(self.can_redo_file_operation())

    def set_file_clipboard(self, paths, move=False):
        """Store file paths in the app clipboard and the system clipboard."""
        self.clipboard_paths = list(paths)
        self.clipboard_move = move

        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(path) for path in paths])
        QApplication.clipboard().setMimeData(mime_data)

    def file_clipboard_contents(self):
        """Return app clipboard paths or local file URLs from the system clipboard."""
        if self.clipboard_paths:
            return self.clipboard_paths, self.clipboard_move

        mime_data = QApplication.clipboard().mimeData()
        paths = [
            url.toLocalFile()
            for url in mime_data.urls()
            if url.isLocalFile() and url.toLocalFile()
        ]
        return paths, False

    def can_paste_files(self):
        """Return whether paste can run in the current browser state."""
        return self.can_paste_files_to(
            self.current_paste_destination(),
            allow_drive_root=True,
        )

    def can_paste_files_to(self, destination, allow_drive_root=False):
        """Return whether files can be pasted into the given folder."""
        paths, _move = self.file_clipboard_contents()
        return (
            bool(paths)
            and bool(destination)
            and Path(destination).is_dir()
            and (allow_drive_root or not self.is_drive_root_path(destination))
        )

    def current_paste_destination(self):
        """Return the folder that should receive pasted files."""
        folder_path = self.browser.current_folder_path()
        if folder_path and Path(folder_path).is_dir():
            return folder_path

        typed_path = self.ui.navigate_bar.text().strip()
        if typed_path and Path(typed_path).is_dir():
            return str(Path(typed_path))

        return ""

    @staticmethod
    def is_drive_root_path(path_text):
        """Return whether a path is a Windows drive root such as C:/ or D:/."""
        if not path_text:
            return False

        path = Path(path_text)
        anchor = path.anchor
        if not anchor:
            return False

        clean_path = QDir.cleanPath(str(path_text)).rstrip("/\\").casefold()
        clean_anchor = QDir.cleanPath(anchor).rstrip("/\\").casefold()
        return clean_path == clean_anchor

    def show_paste_result(self, result, move):
        """Show a temporary status message for paste results."""
        verb = "Moved" if move else "Copied"
        fields = [f"{verb}: {len(result['done'])}"]

        if result["skipped"]:
            fields.append(f"Skipped: {len(result['skipped'])}")

        if result["errors"]:
            fields.append(f"Errors: {len(result['errors'])}")
            fields.append(result["errors"][0])

        self.show_temporary_status(fields)

    def show_delete_result(self, result):
        """Show a temporary status message for delete results."""
        fields = [f"Deleted: {len(result['done'])}"]

        if result["skipped"]:
            fields.append(f"Skipped: {len(result['skipped'])}")

        if result["errors"]:
            fields.append(f"Errors: {len(result['errors'])}")
            fields.append(result["errors"][0])

        self.show_temporary_status(fields)

    def show_operation_result(self, label, result):
        """Show a temporary status message for undo or redo results."""
        fields = [f"{label}: {len(result['done'])}"]

        if result["skipped"]:
            fields.append(f"Skipped: {len(result['skipped'])}")

        if result["errors"]:
            fields.append(f"Errors: {len(result['errors'])}")
            fields.append(result["errors"][0])

        self.show_temporary_status(fields)

    def show_temporary_status(self, fields, duration_ms=3000):
        """Show a status message briefly, then restore the normal status."""
        self.status_restore_token += 1
        token = self.status_restore_token
        self.browser.set_status_fields(fields)
        QTimer.singleShot(duration_ms, lambda: self.restore_status_after_delay(token))

    def restore_status_after_delay(self, token):
        """Restore normal status when the latest temporary message expires."""
        if token != self.status_restore_token:
            return

        self.restore_current_status()

    def restore_current_status(self):
        """Restore folder or search summary status for the active state."""
        folder_path = self.browser.current_folder_path()
        if folder_path and Path(folder_path).is_dir():
            self.browser.update_folder_info(folder_path)
            return

        entry = self.history.current()
        if entry["type"] == "folder" and Path(entry["path"]).is_dir():
            self.browser.update_folder_info(entry["path"])
            return

        if entry["type"] == "search":
            self.browser.set_status_fields(
                [
                    f"Shown results: {len(entry['paths'])}",
                    f"Total results: {entry['total_results']}",
                    f"Limit: {entry['result_limit']}",
                    f"Status: {entry['message'] or 'Ready'}",
                ]
            )

    def remove_deleted_clipboard_paths(self, deleted_paths):
        """Drop deleted paths from the pending cut/copy clipboard."""
        deleted_path_set = {QDir.cleanPath(path).casefold() for path in deleted_paths}
        self.clipboard_paths = [
            path
            for path in self.clipboard_paths
            if QDir.cleanPath(path).casefold() not in deleted_path_set
        ]
        if not self.clipboard_paths:
            self.clipboard_move = False

    def refresh_browser(self):
        """Reload the active folder or search history entry."""
        entry = self.history.current()
        if entry["type"] == "folder":
            self.navigate_to(entry["path"], add_history=False)
        elif entry["type"] == "search":
            self.search_everything(entry["query"], add_history=False)

    def save_current_browser_position(self):
        """Store the current selected table path in history."""
        self.history.update_current_selected_path(self.browser.current_table_path())

    def restore_saved_table_position(self, entry):
        """Restore selected table path saved on a history entry."""
        selected_path = entry.get("selected_path") or ""
        if not selected_path:
            return

        self.browser.restore_table_position(selected_path)
        if Path(selected_path).exists():
            self.preview_path_with_analysis(selected_path)

    def preview_path_with_analysis(self, path_text):
        """Refresh preview metadata and append analysis data when present."""
        self.preview.preview_path(path_text)
        self.append_analysis_to_preview(path_text)

    def append_analysis_to_preview(self, path_text):
        """Append saved folder-analysis rows to the preview table."""
        if not Path(path_text).is_dir():
            return

        record = self.analysis_store.folder_summary(path_text)
        if not record:
            return

        self.preview.append_preview_rows(
            [
                ("", ""),
                ("Analysis", ""),
                ("Analysed at", record["analysed_at"]),
                ("Analysed root", record["root_path"]),
                ("Total size", format_bytes(record["size_bytes"])),
                ("Files", record["file_count"]),
                ("Subfolders", record["folder_count"]),
                ("Errors", record["error_count"]),
            ],
            value_tooltips=True,
        )

    def selected_folder_for_analysis(self):
        """Return the selected folder, falling back to the current folder."""
        table_index = self.ui.table_view.currentIndex()
        if table_index.isValid():
            table_path = self.browser.table_path(table_index)
            if table_path and Path(table_path).is_dir():
                return table_path

        tree_index = self.ui.tree_view.currentIndex()
        if tree_index.isValid():
            tree_path = self.browser.tree_path(tree_index)
            if tree_path and Path(tree_path).is_dir():
                return tree_path

        current_entry = self.history.current()
        if current_entry["type"] == "folder":
            return current_entry["path"]

        typed_path = self.ui.navigate_bar.text().strip()
        if typed_path and Path(typed_path).is_dir():
            return typed_path

        return ""

    def create_new_ai_folder(self):
        """Create an AI-managed folder in the current browser folder."""
        default_parent = self.current_ai_folder_parent()
        if not default_parent:
            self.show_temporary_status(["No folder selected for New AIFolder."])
            return

        options = self.ask_new_ai_folder_options(default_parent)
        if not options:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            record = self.ai_folder_store.create_ai_folder(
                parent_path=options["parent_path"],
                name=options["folder_name"],
                authorization_mode=options["authorization_mode"],
                aifm_params={
                    "created_by": "frontend",
                    "visible_in_browser": True,
                },
            )
        except OSError as error:
            self.show_temporary_status([f"New AIFolder failed: {error}"])
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.navigate_to(
            options["parent_path"],
            add_history=False,
            selected_path=record.folder_path,
        )
        self.preview_path_with_analysis(record.folder_path)
        self.show_temporary_status(
            [
                f"New AIFolder: {record.name}",
                f"Auth: {record.authorization_mode}",
            ]
        )

    def current_ai_folder_parent(self):
        """Return the folder where New AIFolder should be created."""
        folder_path = self.browser.current_folder_path()
        if folder_path and Path(folder_path).is_dir():
            return folder_path

        tree_index = self.ui.tree_view.currentIndex()
        if tree_index.isValid():
            tree_path = self.browser.tree_path(tree_index)
            if tree_path and Path(tree_path).is_dir():
                return tree_path

        typed_path = self.ui.navigate_bar.text().strip()
        if typed_path and Path(typed_path).is_dir():
            return typed_path

        return ""

    def ask_new_ai_folder_options(self, default_parent):
        dialog = QDialog(self.window)
        dialog.setWindowTitle("New AIFolder")

        parent_edit = QLineEdit(default_parent)
        name_edit = QLineEdit("New AIFolder")
        mode_combo = QComboBox(dialog)
        mode_combo.addItem("User Required", AIFolderStore.AUTH_USER_REQUIRED)
        mode_combo.addItem("AI Decides", AIFolderStore.AUTH_AI_DECIDES)
        mode_combo.addItem("Always Allowed", AIFolderStore.AUTH_ALWAYS_ALLOWED)

        browse_button = QPushButton("Browse", dialog)
        path_row = QWidget(dialog)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(parent_edit, 1)
        path_layout.addWidget(browse_button)

        form_layout = QFormLayout()
        form_layout.addRow("Parent folder", path_row)
        form_layout.addRow("Folder name", name_edit)
        form_layout.addRow("Authorization", mode_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )

        layout = QVBoxLayout(dialog)
        layout.addLayout(form_layout)
        layout.addWidget(buttons)

        def choose_parent():
            folder = QFileDialog.getExistingDirectory(
                self.window,
                "Choose Parent Folder",
                parent_edit.text().strip() or default_parent,
            )
            if folder:
                parent_edit.setText(QDir.cleanPath(folder))

        def accept_if_valid():
            parent_path = QDir.cleanPath(parent_edit.text().strip())
            folder_name = name_edit.text().strip()
            if not parent_path or not Path(parent_path).is_dir():
                QMessageBox.warning(
                    dialog,
                    "New AIFolder",
                    "Parent folder does not exist.",
                )
                return
            if not folder_name:
                QMessageBox.warning(
                    dialog,
                    "New AIFolder",
                    "Folder name cannot be empty.",
                )
                return

            dialog.options = {
                "parent_path": parent_path,
                "folder_name": folder_name,
                "authorization_mode": mode_combo.currentData(),
            }
            dialog.accept()

        browse_button.clicked.connect(choose_parent)
        buttons.accepted.connect(accept_if_valid)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.options

        return None

    def analyse_selected_folder(self):
        """Analyse selected folder sizes and persist the result table."""
        folder_path = self.selected_folder_for_analysis()
        if not folder_path:
            self.browser.set_status_fields(["No folder selected for analysis."])
            return

        self.browser.set_status_fields([f"Analysing: {folder_path}"])
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        try:
            summary = self.analysis_store.analyse_and_store(folder_path)
        except OSError as error:
            self.browser.set_status_fields([f"Analysis failed: {error}"])
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.browser.set_status_fields(
            [
                f"Analysed folders: {summary.folder_count + 1}",
                f"Files: {summary.file_count}",
                f"Size: {format_bytes(summary.size_bytes)}",
                f"Errors: {summary.error_count}",
            ]
        )
        current_folder = self.browser.current_folder_path()
        if current_folder:
            self.update_folder_size_cache(current_folder)
        self.preview_path_with_analysis(summary.folder_path)

    def update_history_buttons(self):
        """Enable or disable back/forward buttons."""
        self.ui.back_button.setEnabled(self.history.can_go_back())
        self.ui.forward_button.setEnabled(self.history.can_go_forward())

    def go_back(self):
        """Restore the previous history state."""
        if self.history.can_go_back():
            self.save_current_browser_position()
            self.restore_history_entry(self.history.go_back())

    def go_forward(self):
        """Restore the next history state."""
        if self.history.can_go_forward():
            self.save_current_browser_position()
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
            self.navigate_to(str(path.parent), selected_path=str(path))
        else:
            self.search_everything(typed_path)

    def search_everything(self, query, add_history=True):
        """Run an Everything search and optionally add it to history."""
        if add_history:
            self.save_current_browser_position()

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
            self.navigate_to(
                entry["path"],
                add_history=False,
                selected_path=entry.get("selected_path", ""),
            )
        elif entry["type"] == "search":
            self.show_search_state(
                entry["query"],
                entry["paths"],
                entry["message"],
                entry["total_results"],
                entry["result_limit"],
            )
            self.restore_saved_table_position(entry)
            self.update_history_buttons()

    def show(self):
        """Show the loaded Qt window."""
        self.window.show()

    def cleanup_on_exit(self):
        """Clear the app trash when the Qt application exits."""
        self.file_operations.clear_trash()


def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    file_manager = FileManagerWindow()
    app.aboutToQuit.connect(file_manager.cleanup_on_exit)
    atexit.register(file_manager.cleanup_on_exit)
    file_manager.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
