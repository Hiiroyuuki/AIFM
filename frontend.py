import sys
from datetime import datetime
import json
import mimetypes
from pathlib import Path

from PySide6.QtCore import QDir, QFile, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileSystemModel,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

DEFAULT_PATH = "C:/"
BASE_DIR = Path(__file__).resolve().parent
FILE_TYPE_NAMES_PATH = BASE_DIR / "file_type_names.json"


def load_file_type_names():
    try:
        with FILE_TYPE_NAMES_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}

    return {
        extension.lower(): name
        for extension, name in data.items()
        if extension.startswith(".") and isinstance(name, str)
    }


def main():
    app = QApplication(sys.argv)
    file_type_names = load_file_type_names()

    ui_path = Path(__file__).with_name("form.ui")
    ui_file = QFile(str(ui_path))
    ui_file.open(QFile.ReadOnly)

    window = QUiLoader().load(ui_file)
    ui_file.close()

    central_widget = window.findChild(QWidget, "centralwidget") or window
    tree_view = window.findChild(QTreeView, "treeView")
    list_view = window.findChild(QListView, "listView")
    back_button = window.findChild(QPushButton, "back")
    forward_button = window.findChild(QPushButton, "forward")
    navigate_bar = window.findChild(QLineEdit, "navigateBar")
    preview_view = window.findChild(QTableView, "preview")
    ai_view = window.findChild(QTableView, "AIview")
    side_buttons = [
        window.findChild(QPushButton, f"pushButton_{index}")
        for index in range(1, 8)
    ]

    toolbar_layout = QHBoxLayout()
    toolbar_layout.addWidget(back_button)
    toolbar_layout.addWidget(forward_button)
    toolbar_layout.addWidget(navigate_bar, 1)

    side_panel = QWidget(central_widget)
    side_layout = QVBoxLayout(side_panel)
    for button in side_buttons:
        if button is not None:
            side_layout.addWidget(button)
    side_layout.addStretch()

    preview_splitter = QSplitter(Qt.Orientation.Vertical, central_widget)
    preview_splitter.addWidget(preview_view)
    preview_splitter.addWidget(ai_view)
    preview_splitter.setStretchFactor(0, 1)
    preview_splitter.setStretchFactor(1, 1)
    preview_splitter.setSizes([260, 260])
    preview_splitter.setChildrenCollapsible(False)

    content_splitter = QSplitter(Qt.Orientation.Horizontal, central_widget)
    content_splitter.addWidget(tree_view)
    content_splitter.addWidget(list_view)
    content_splitter.addWidget(preview_splitter)
    content_splitter.setStretchFactor(0, 1)
    content_splitter.setStretchFactor(1, 2)
    content_splitter.setStretchFactor(2, 1)
    content_splitter.setSizes([260, 460, 320])
    content_splitter.setChildrenCollapsible(False)

    body_layout = QHBoxLayout()
    body_layout.addWidget(side_panel)
    body_layout.addWidget(content_splitter, 1)

    main_layout = QVBoxLayout(central_widget)
    main_layout.addLayout(toolbar_layout)
    main_layout.addLayout(body_layout, 1)

    side_panel.setFixedWidth(110)
    tree_view.setMinimumWidth(180)
    list_view.setMinimumWidth(260)
    list_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    tree_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    preview_view.setMinimumWidth(260)
    ai_view.setMinimumWidth(260)

    dir_model = QFileSystemModel(window)
    dir_model.setFilter(QDir.Drives | QDir.AllDirs | QDir.NoDotAndDotDot)
    dir_model.setRootPath("")

    tree_view.setModel(dir_model)
    tree_view.setRootIndex(dir_model.index(""))
    tree_view.setHeaderHidden(False)
    tree_view.setIndentation(10)
    tree_view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    for column in range(1, dir_model.columnCount()):
        tree_view.hideColumn(column)

    file_model = QFileSystemModel(window)
    file_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)
    file_model.setRootPath(DEFAULT_PATH)

    list_view.setModel(file_model)
    list_view.setRootIndex(file_model.index(DEFAULT_PATH))

    preview_model = QStandardItemModel(window)
    ai_model = QStandardItemModel(window)
    preview_view.setModel(preview_model)
    ai_view.setModel(ai_model)

    for table_view in (preview_view, ai_view):
        table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table_view.horizontalHeader().setStretchLastSection(True)

    preview_view.horizontalHeader().setVisible(False)
    preview_view.verticalHeader().setVisible(False)
    preview_view.setShowGrid(False)
    preview_view.setWordWrap(False)
    preview_view.verticalHeader().setDefaultSectionSize(22)
    preview_view.verticalHeader().setMinimumSectionSize(18)
    preview_view.setStyleSheet(
        """
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
    )

    def set_table_rows(model, headers, rows, value_tooltips=False):
        model.clear()
        model.setHorizontalHeaderLabels(headers)
        for row in rows:
            items = [QStandardItem(str(value)) for value in row]
            if value_tooltips and len(items) > 1:
                items[1].setToolTip(str(row[1]))
            model.appendRow(items)

    def format_time(timestamp):
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def describe_file_type(path):
        suffix = path.suffix.lower()
        if suffix in file_type_names:
            return file_type_names[suffix]

        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type:
            main_type, sub_type = mime_type.split("/", 1)
            return f"{sub_type.replace('-', ' ').title()} {main_type}"

        if suffix:
            return f"{suffix[1:].upper()} file"

        return "File"

    def set_ai_placeholder():
        set_table_rows(
            ai_model,
            ["AI Preview"],
            [["AI preview placeholder"], ["No AI feature is connected yet."]],
        )

    def preview_path(path_text):
        path = Path(path_text).resolve(strict=False)
        rows = [
            ("Name", path.name or str(path)),
            ("Absolute path", str(path)),
        ]

        try:
            stat = path.stat()
        except OSError as error:
            rows.append(("Error", error))
            set_table_rows(preview_model, ["Field", "Value"], rows, value_tooltips=True)
            return

        if path.is_dir():
            rows.extend(
                [
                    ("Type", "Folder"),
                    ("Modified", format_time(stat.st_mtime)),
                ]
            )
        else:
            rows.extend(
                [
                    ("Type", describe_file_type(path)),
                    ("Size", f"{stat.st_size} bytes"),
                    ("Modified", format_time(stat.st_mtime)),
                ]
            )

        set_table_rows(preview_model, ["Field", "Value"], rows, value_tooltips=True)

    def preview_selected(index):
        if index.isValid():
            preview_path(file_model.filePath(index))

    history = [DEFAULT_PATH]
    history_index = 0
    syncing_tree = False

    def update_history_buttons():
        back_button.setEnabled(history_index > 0)
        forward_button.setEnabled(history_index < len(history) - 1)

    def navigate_to(folder_path, add_history=True, sync_tree=True):
        nonlocal history_index, syncing_tree

        folder_path = QDir.cleanPath(QDir(folder_path).absolutePath())
        if not QDir(folder_path).exists():
            return

        list_view.setRootIndex(file_model.setRootPath(folder_path))
        navigate_bar.setText(folder_path)
        preview_path(folder_path)

        if add_history and history[history_index] != folder_path:
            del history[history_index + 1 :]
            history.append(folder_path)
            history_index = len(history) - 1

        if sync_tree:
            tree_index = dir_model.index(folder_path)
            if tree_index.isValid():
                syncing_tree = True
                tree_view.expand(tree_index)
                tree_view.setCurrentIndex(tree_index)
                tree_view.scrollTo(
                    tree_index,
                    QAbstractItemView.ScrollHint.EnsureVisible,
                )
                syncing_tree = False

        update_history_buttons()

    def show_files(index):
        if syncing_tree:
            return

        folder_path = dir_model.filePath(index)
        navigate_to(folder_path, sync_tree=False)

    def open_item(index):
        item_path = file_model.filePath(index)
        if file_model.isDir(index):
            navigate_to(item_path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item_path))

    def go_back():
        nonlocal history_index

        if history_index == 0:
            return

        history_index -= 1
        navigate_to(history[history_index], add_history=False)

    def go_forward():
        nonlocal history_index

        if history_index >= len(history) - 1:
            return

        history_index += 1
        navigate_to(history[history_index], add_history=False)

    def go_to_typed_path():
        typed_path = navigate_bar.text().strip()
        if not typed_path:
            navigate_bar.setText(history[history_index])
            return

        folder_path = QDir.cleanPath(QDir(typed_path).absolutePath())
        if QDir(folder_path).exists():
            navigate_to(folder_path)
        else:
            navigate_bar.setText(history[history_index])

    tree_view.selectionModel().currentChanged.connect(show_files)
    list_view.doubleClicked.connect(open_item)
    list_view.selectionModel().currentChanged.connect(preview_selected)
    back_button.clicked.connect(go_back)
    forward_button.clicked.connect(go_forward)
    navigate_bar.returnPressed.connect(go_to_typed_path)
    navigate_bar.editingFinished.connect(go_to_typed_path)

    set_ai_placeholder()
    navigate_to(DEFAULT_PATH, add_history=False)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
