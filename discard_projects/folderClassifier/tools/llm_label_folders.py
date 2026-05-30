from __future__ import annotations

import argparse
import json
import os
import stat as stat_module
import sys
from collections import Counter, deque
from dataclasses import asdict, dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import OpenAI


LABELS = (
    "app",
    "db",
    "cache",
    "user_data",
    "media",
    "document",
)
LABEL_SET = set(LABELS)

SKIP_DIR_NAMES = {
    "$RECYCLE.BIN",
    "System Volume Information",
    ".git",
    "__pycache__",
    "node_modules",
}

SYSTEM_PROMPT = """你是一个文件夹语义分类器。你只能根据文件夹路径、文件夹名、子目录名、文件名样本、扩展名统计等 metadata 判断标签。不要假设你读取了文件内容。

你的目标不是进行非常细粒度的文件系统分类，而是帮助用户快速区分：
- 哪些目录可能包含用户关心的数据
- 哪些目录主要是程序、缓存、依赖或低价值内容

你应该优先识别：
- 媒体文件目录
- 文档目录
- 用户下载、接收或创建的数据目录
- 聊天软件数据目录
- 项目与工作目录
- 程序目录

你不需要把目录细分成过多的小语义类别。对于已经足够明确的大目录，可以直接使用较粗粒度的标签。

你会收到每个文件夹的：
- path
- depth
- folder_name
- child_dirs
- sampled_files
- extension_counts
- total_files
- total_dirs
- parent_path
- parent_labels

parent_path 和 parent_labels 只用于理解上下文，不能作为硬约束。你应该根据当前目录自己的 path、folder_name、child_dirs、sampled_files、extension_counts、total_files、total_dirs 判断当前目录 labels 和 should_descend。

你必须从以下标签中选择一个或多个：

app:
软件、程序、可执行文件、安装目录、插件、运行环境、IDE、游戏程序、工具程序。

db:
数据库、索引、持久化存储、数据集，例如 sqlite、db、indexeddb、leveldb、database、storage、datasets。

user_data:
用户生成或用户接收的数据，例如聊天文件、浏览器用户数据、微信/QQ文件、个人资料、项目文件、桌面/文档类目录。

media:
媒体类型的文件：图片、视频、音频、相机文件、素材，例如 jpg、png、mp4、mov、mp3、wav。

document:
文档、论文、报告、会议资料、表格、PPT、PDF、笔记、txt、docx、xlsx、pptx。

other:
不确定的文件夹，或者无法归类到上述标签的文件夹。低价值或不适合作为用户关注文件入口的目录。包括程序内部实现细节、依赖库、运行时文件、构建产物、临时目录、缓存目录、日志目录、碎片化资源目录、无法体现用户主动保存/接收内容的目录。

标签规则：
1. 可以选择多个标签。
2. labels 不能为空。
3. 如果 labels 包含 app，则 labels 必须为 ["app"]，should_descend 必须为 false。
4. 如果 labels 包含 other，则 labels 必须为 ["other"]。

should_descend 判断原则：
1. 如果 labels 是 ["app"]，should_descend=false。
2. 如果 labels 是 ["other"]，should_descend=false。
3. 如果 total_dirs == 0，should_descend=false。
4. 如果当前目录是容器型目录，例如 Downloads、Documents、Pictures、Videos、WeChat Files、Projects、datasets 等，并且子目录可能有不同语义，则 should_descend=true。
5. 如果子目录只是实现细节或同质结构，例如 bin、lib、include、runtime、resources、assets、hash objects、logs shards 等，则 should_descend=false。
6. db/media/document/user_data 是否继续下分，由你根据当前目录 metadata 自己判断。
7. 如果不确定，优先选择更保守的标签和 should_descend=false。

输出必须是 JSON 数组，每个元素包含：
- path: 原样返回
- labels: list[str]
- should_descend: bool
- reason: str

不要输出 Markdown。
不要输出解释。
不要输出 ```json 代码块。
不要输出 <think>、</think> 或任何推理过程。
第一个字符必须是 [。
最后一个字符必须是 ]。
"""


@dataclass
class FolderSummary:
    path: str
    depth: int
    folder_name: str
    child_dirs: list[str]
    sampled_files: list[str]
    extension_counts: dict[str, int]
    total_files: int
    total_dirs: int


@dataclass
class ScanNode:
    path: Path
    depth: int
    parent_path: str | None = None
    parent_labels: list[str] | None = None


@dataclass
class PendingSummary:
    summary: FolderSummary
    child_paths: list[Path]
    parent_path: str | None = None
    parent_labels: list[str] | None = None


def normalize_path(path: Path) -> str:
    try:
        normalized = path.expanduser().resolve(strict=False)
    except OSError:
        normalized = path.expanduser().absolute()
    return str(normalized).replace("\\", "/")


def normalize_path_text(path_text: str) -> str:
    normalized = str(path_text).strip().replace("\\", "/")
    if len(normalized) > 3:
        normalized = normalized.rstrip("/")
    return normalized


def path_key(path_text: str) -> str:
    return normalize_path_text(path_text).casefold()


def folder_display_name(path: Path) -> str:
    return path.name or path.anchor.rstrip("\\/") or str(path)


def is_hidden_or_system_dir(path: Path) -> bool:
    if path.is_symlink():
        return True

    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return False
    except (PermissionError, FileNotFoundError):
        return True
    except OSError:
        return False

    hidden = getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 0)
    system = getattr(stat_module, "FILE_ATTRIBUTE_SYSTEM", 0)
    reparse_point = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & (hidden | system | reparse_point))


def should_skip_dir(path: Path, skip_dir_names: set[str] = SKIP_DIR_NAMES) -> bool:
    skip_names = {name.casefold() for name in skip_dir_names}
    return path.name.casefold() in skip_names or is_hidden_or_system_dir(path)


def summarize_folder(
    folder: Path,
    depth: int,
    max_files_per_folder: int,
) -> tuple[FolderSummary | None, list[Path]]:
    child_dirs: list[str] = []
    child_paths: list[Path] = []
    sampled_files: list[str] = []
    extension_counts: Counter[str] = Counter()
    total_files = 0

    try:
        entries = folder.iterdir()
        for entry in entries:
            try:
                if entry.is_dir():
                    if should_skip_dir(entry):
                        continue
                    child_dirs.append(entry.name)
                    child_paths.append(entry)
                    continue

                if entry.is_file():
                    total_files += 1
                    if len(sampled_files) < max_files_per_folder:
                        sampled_files.append(entry.name)
                    extension = entry.suffix.lower() or "<no_ext>"
                    extension_counts[extension] += 1
            except (PermissionError, FileNotFoundError) as error:
                print(
                    f"[scan] skip entry={normalize_path_text(str(entry))} "
                    f"error={type(error).__name__}: {error}",
                    flush=True,
                )
            except OSError as error:
                print(
                    f"[scan] skip entry={normalize_path_text(str(entry))} "
                    f"error={type(error).__name__}: {error}",
                    flush=True,
                )
    except PermissionError as error:
        print(
            f"[scan] skip folder={normalize_path(folder)} "
            f"error={type(error).__name__}: {error}",
            flush=True,
        )
        return None, []
    except FileNotFoundError as error:
        print(
            f"[scan] skip folder={normalize_path_text(str(folder))} "
            f"error={type(error).__name__}: {error}",
            flush=True,
        )
        return None, []
    except OSError as error:
        print(
            f"[scan] skip folder={normalize_path_text(str(folder))} "
            f"error={type(error).__name__}: {error}",
            flush=True,
        )
        return None, []

    child_dirs.sort(key=str.casefold)
    sampled_files.sort(key=str.casefold)
    extension_counts_dict = {
        extension: count
        for extension, count in sorted(
            extension_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    }

    summary = FolderSummary(
        path=normalize_path(folder),
        depth=depth,
        folder_name=folder_display_name(folder),
        child_dirs=child_dirs,
        sampled_files=sampled_files,
        extension_counts=extension_counts_dict,
        total_files=total_files,
        total_dirs=len(child_dirs),
    )
    return summary, child_paths


def scan_folders(
    root: Path,
    max_depth: int,
    max_files_per_folder: int,
) -> list[FolderSummary]:
    root = root.expanduser().resolve(strict=False)
    try:
        if not root.exists():
            print(f"[scan] root not found: {normalize_path_text(str(root))}", flush=True)
            return []
        if not root.is_dir():
            print(f"[scan] root is not a directory: {normalize_path(root)}", flush=True)
            return []
    except (PermissionError, FileNotFoundError, OSError) as error:
        print(
            f"[scan] cannot access root={normalize_path_text(str(root))} "
            f"error={type(error).__name__}: {error}",
            flush=True,
        )
        return []

    summaries: list[FolderSummary] = []
    queue: deque[tuple[Path, int]] = deque([(root, 0)])

    while queue:
        folder, depth = queue.popleft()
        if depth > max_depth:
            continue
        if depth > 0 and should_skip_dir(folder):
            continue

        summary, child_paths = summarize_folder(
            folder=folder,
            depth=depth,
            max_files_per_folder=max_files_per_folder,
        )
        if summary is None:
            continue
        summaries.append(summary)

        if depth >= max_depth:
            continue
        for child_path in sorted(child_paths, key=lambda path: path.name.casefold()):
            queue.append((child_path, depth + 1))

    return summaries


def summary_to_prompt_dict(summary: FolderSummary) -> dict[str, Any]:
    return {
        "path": summary.path,
        "depth": summary.depth,
        "folder_name": summary.folder_name,
        "child_dirs": summary.child_dirs,
        "sampled_files": summary.sampled_files,
        "extension_counts": summary.extension_counts,
        "total_files": summary.total_files,
        "total_dirs": summary.total_dirs,
    }


def pending_summary_to_prompt_dict(pending: PendingSummary) -> dict[str, Any]:
    payload = summary_to_prompt_dict(pending.summary)
    payload.update(
        {
            "parent_path": pending.parent_path,
            "parent_labels": pending.parent_labels,
        }
    )
    return payload


def normalize_label_list(raw_labels: Any) -> list[str]:
    if not isinstance(raw_labels, list):
        return []

    labels: list[str] = []
    seen_labels: set[str] = set()
    for raw_label in raw_labels:
        if not isinstance(raw_label, str):
            continue
        label = raw_label.strip().lower()
        if label not in LABEL_SET or label in seen_labels:
            continue
        labels.append(label)
        seen_labels.add(label)
    return labels


def fallback_label(allowed: set[str], summary: FolderSummary) -> str:
    name = summary.folder_name.casefold()
    extensions = set(summary.extension_counts)
    extension_label_hints = [
        ("db", {".db", ".sqlite", ".sqlite3", ".mdb", ".ldb"}),
        ("cache", {".cache", ".tmp", ".temp", ".log"}),
        ("media", {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".mp3", ".wav"}),
        ("document", {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md"}),
    ]
    name_label_hints = [
        ("cache", {"cache", "tmp", "temp", "logs", "log"}),
        ("db", {"db", "database", "storage", "indexeddb", "leveldb"}),
        ("media", {"media", "picture", "pictures", "photo", "photos", "video", "videos", "music"}),
        ("document", {"doc", "docs", "document", "documents", "paper", "notes", "meeting"}),
    ]

    for label, hints in extension_label_hints:
        if label in allowed and extensions & hints:
            return label
    for label, hints in name_label_hints:
        if label in allowed and any(hint in name for hint in hints):
            return label
    if "user_data" in allowed:
        return "user_data"
    ordered = [label for label in LABELS if label in allowed]
    return ordered[0] if ordered else "user_data"


def infer_should_descend(labels: list[str], summary: FolderSummary) -> bool:
    if "app" in labels:
        return False
    if summary.total_dirs == 0:
        return False

    folder_name = summary.folder_name.casefold()
    container_names = {
        "download",
        "downloads",
        "document",
        "documents",
        "picture",
        "pictures",
        "photo",
        "photos",
        "video",
        "videos",
        "music",
        "wechat files",
        "projects",
        "project",
        "datasets",
        "dataset",
    }
    implementation_names = {
        "bin",
        "lib",
        "include",
        "runtime",
        "resources",
        "assets",
        "logs",
        "log",
        "tmp",
        "temp",
        "cache",
    }
    if folder_name in implementation_names:
        return False
    if folder_name in container_names:
        return True

    return summary.total_dirs > 0


def normalize_decision_by_rules(
    decision: dict,
    summary: FolderSummary,
    parent_labels: list[str] | None,
) -> dict:
    labels = normalize_label_list(decision.get("labels"))

    if not labels:
        labels = [fallback_label(set(LABELS), summary)]

    should_descend = decision.get("should_descend")
    if not isinstance(should_descend, bool):
        should_descend = infer_should_descend(labels, summary)

    if "app" in labels:
        labels = ["app"]
        should_descend = False

    if summary.total_dirs == 0:
        should_descend = False

    reason = decision.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "normalized by hierarchy rules"

    return {
        "path": summary.path,
        "labels": labels,
        "should_descend": should_descend,
        "reason": reason.strip(),
    }


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_array(text: str) -> str:
    stripped = strip_json_fence(text)

    # Remove common thinking tags if present.
    while True:
        start_tag = stripped.find("<think>")
        end_tag = stripped.find("</think>")
        if start_tag != -1 and end_tag != -1 and end_tag > start_tag:
            stripped = stripped[:start_tag] + stripped[end_tag + len("</think>") :]
            stripped = stripped.strip()
            continue
        break

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON array found in LLM response.")

    return stripped[start : end + 1]


def validate_labels(result: list[dict]) -> list[dict]:
    if not isinstance(result, list):
        print(f"[validate] expected list, got {type(result).__name__}", flush=True)
        return []

    validated: list[dict] = []
    for index, item in enumerate(result):
        if not isinstance(item, dict):
            print(f"[validate] skip item #{index}: expected object", flush=True)
            continue

        path = item.get("path")
        labels = item.get("labels")
        if not isinstance(path, str) or not path.strip():
            print(f"[validate] skip item #{index}: missing path", flush=True)
            continue
        if not isinstance(labels, list):
            print(f"[validate] skip path={path}: labels must be a list", flush=True)
            continue

        normalized_labels: list[str] = []
        seen_labels: set[str] = set()
        invalid_labels: list[Any] = []
        for label in labels:
            if not isinstance(label, str):
                invalid_labels.append(label)
                continue
            normalized_label = label.strip().lower()
            if normalized_label not in LABEL_SET:
                invalid_labels.append(label)
                continue
            if normalized_label not in seen_labels:
                normalized_labels.append(normalized_label)
                seen_labels.add(normalized_label)

        if invalid_labels:
            print(
                f"[validate] ignored invalid labels for path={path}: {invalid_labels}",
                flush=True,
            )
        if not normalized_labels:
            print(f"[validate] skip path={path}: labels cannot be empty", flush=True)
            continue

        validated.append(
            {
                "path": normalize_path_text(path),
                "labels": normalized_labels,
            }
        )

    return validated


def keep_results_for_batch(
    results: list[dict],
    summaries: list[FolderSummary],
) -> list[dict]:
    path_map = {path_key(summary.path): summary.path for summary in summaries}
    kept: list[dict] = []
    seen_paths: set[str] = set()

    for item in results:
        path = normalize_path_text(item["path"])
        canonical_path = path_map.get(path_key(path))
        if canonical_path is None:
            print(f"[validate] skip path not in current batch: {path}", flush=True)
            continue
        canonical_key = path_key(canonical_path)
        if canonical_key in seen_paths:
            print(f"[validate] skip duplicate path in batch: {canonical_path}", flush=True)
            continue

        kept.append({"path": canonical_path, "labels": item["labels"]})
        seen_paths.add(canonical_key)

    missing_paths = [summary.path for summary in summaries if path_key(summary.path) not in seen_paths]
    if missing_paths:
        print(
            f"[validate] missing labels for {len(missing_paths)} folder(s) in batch",
            flush=True,
        )

    return kept


def label_batch_with_llm(
    client: OpenAI,
    model: str,
    pending_summaries: list[PendingSummary],
) -> list[dict]:
    payload = [pending_summary_to_prompt_dict(pending) for pending in pending_summaries]
    user_prompt = (
        "请为下面的文件夹摘要输出 JSON 数组。每个元素必须包含 path、labels、"
        "should_descend 和 reason。path 必须原样返回，labels 必须只使用允许的标签集合。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        content = response.choices[0].message.content or ""
    except Exception as error:
        print(f"[label] API error: {type(error).__name__}: {error}", flush=True)
        return []

    try:
        parsed = json.loads(extract_json_array(content))
    except (JSONDecodeError, ValueError) as error:
        preview = content.strip().replace("\n", " ")[:500]
        print(
            f"[label] invalid JSON: {error}. response_preview={preview!r}",
            flush=True,
        )
        return []

    if not isinstance(parsed, list):
        print(f"[validate] expected list, got {type(parsed).__name__}", flush=True)
        return []

    path_map = {
        path_key(pending.summary.path): pending
        for pending in pending_summaries
    }
    decisions_by_path: dict[str, dict] = {}
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            print(f"[validate] skip item #{index}: expected object", flush=True)
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            print(f"[validate] skip item #{index}: missing path", flush=True)
            continue
        key = path_key(path)
        if key not in path_map:
            print(f"[validate] skip path not in current batch: {path}", flush=True)
            continue
        if key in decisions_by_path:
            print(f"[validate] skip duplicate path in batch: {path}", flush=True)
            continue
        decisions_by_path[key] = item

    normalized_decisions: list[dict] = []
    for pending in pending_summaries:
        key = path_key(pending.summary.path)
        item = decisions_by_path.get(key)
        if item is None:
            print(f"[validate] missing decision for path={pending.summary.path}", flush=True)
            continue
        normalized_decisions.append(
            normalize_decision_by_rules(
                decision=item,
                summary=pending.summary,
                parent_labels=pending.parent_labels,
            )
        )

    return normalized_decisions


def load_existing_labels(output_path: Path) -> list[dict]:
    if not output_path.exists():
        return []

    try:
        with output_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except JSONDecodeError as error:
        print(f"[resume] invalid JSON in {output_path}: {error}", flush=True)
        return []
    except FileNotFoundError:
        return []
    except OSError as error:
        print(f"[resume] cannot read {output_path}: {type(error).__name__}: {error}", flush=True)
        return []

    validated = validate_labels(data)
    deduped: list[dict] = []
    seen_paths: set[str] = set()
    for item in validated:
        path = normalize_path_text(item["path"])
        key = path_key(path)
        if key in seen_paths:
            continue
        deduped.append({"path": path, "labels": item["labels"]})
        seen_paths.add(key)
    return deduped


def save_labels(output_path: Path, labels: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(labels, file, ensure_ascii=False, indent=4)
        file.write("\n")
    temp_path.replace(output_path)


def add_results(
    all_results: list[dict],
    new_results: list[dict],
    seen_paths: set[str],
) -> int:
    added = 0
    for item in new_results:
        path = normalize_path_text(item["path"])
        key = path_key(path)
        if key in seen_paths:
            continue
        all_results.append({"path": path, "labels": item["labels"]})
        seen_paths.add(key)
        added += 1
    return added


def to_training_item(decision: dict) -> dict:
    return {
        "path": normalize_path_text(decision["path"]),
        "labels": list(decision["labels"]),
    }


def to_debug_item(decision: dict, pending: PendingSummary) -> dict:
    return {
        "path": normalize_path_text(decision["path"]),
        "labels": list(decision["labels"]),
        "should_descend": bool(decision["should_descend"]),
        "reason": str(decision.get("reason", "")),
        "parent_path": pending.parent_path,
        "parent_labels": pending.parent_labels,
    }


def load_debug_decisions(debug_output_path: Path) -> list[dict]:
    if not debug_output_path.exists():
        return []

    try:
        with debug_output_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except JSONDecodeError as error:
        print(f"[resume] invalid debug JSON in {debug_output_path}: {error}", flush=True)
        return []
    except FileNotFoundError:
        return []
    except OSError as error:
        print(
            f"[resume] cannot read {debug_output_path}: {type(error).__name__}: {error}",
            flush=True,
        )
        return []

    if not isinstance(data, list):
        print(f"[resume] debug JSON must be a list: {debug_output_path}", flush=True)
        return []

    debug_items: list[dict] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"[resume] skip debug item #{index}: expected object", flush=True)
            continue
        path = item.get("path")
        labels = normalize_label_list(item.get("labels"))
        should_descend = item.get("should_descend")
        if not isinstance(path, str) or not path.strip() or not labels:
            print(f"[resume] skip debug item #{index}: missing path or labels", flush=True)
            continue
        if not isinstance(should_descend, bool):
            should_descend = False
        reason = item.get("reason")
        debug_items.append(
            {
                "path": normalize_path_text(path),
                "labels": labels,
                "should_descend": should_descend,
                "reason": reason if isinstance(reason, str) else "",
                "parent_path": item.get("parent_path"),
                "parent_labels": item.get("parent_labels"),
            }
        )
    return debug_items


def save_debug_decisions(debug_output_path: Path, debug_items: list[dict]) -> None:
    debug_output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = debug_output_path.with_name(f"{debug_output_path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(debug_items, file, ensure_ascii=False, indent=4)
        file.write("\n")
    temp_path.replace(debug_output_path)


def upsert_debug_item(
    debug_items: list[dict],
    debug_index: dict[str, int],
    item: dict,
) -> None:
    key = path_key(item["path"])
    if key in debug_index:
        debug_items[debug_index[key]] = item
        return
    debug_index[key] = len(debug_items)
    debug_items.append(item)


def enqueue_children(
    queue: deque[ScanNode],
    pending: PendingSummary,
    decision: dict,
    max_depth: int,
) -> None:
    labels_json = json.dumps(decision["labels"], ensure_ascii=False)
    if decision["should_descend"] and pending.summary.depth < max_depth:
        child_parent_labels = None if pending.summary.depth == 0 else list(decision["labels"])
        for child_path in sorted(pending.child_paths, key=lambda path: path.name.casefold()):
            queue.append(
                ScanNode(
                    path=child_path,
                    depth=pending.summary.depth + 1,
                    parent_path=decision["path"],
                    parent_labels=child_parent_labels,
                )
            )
        print(
            f"[descend] {decision['path']} labels={labels_json} children={len(pending.child_paths)}",
            flush=True,
        )
        return

    print(
        f"[stop] {decision['path']} labels={labels_json} reason={decision.get('reason', '')}",
        flush=True,
    )


def scan_and_label_with_llm(
    root: Path,
    client: OpenAI,
    model: str,
    output_path: Path,
    max_depth: int,
    max_files_per_folder: int,
    batch_size: int,
    resume: bool = False,
    debug_output_path: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    print(
        f"[start] root={normalize_path_text(str(root))} max_depth={max_depth} batch_size={batch_size}",
        flush=True,
    )

    try:
        root = root.expanduser().resolve(strict=False)
        if not root.exists():
            print(f"[scan] root not found: {normalize_path_text(str(root))}", flush=True)
            return [], []
        if not root.is_dir():
            print(f"[scan] root is not a directory: {normalize_path(root)}", flush=True)
            return [], []
    except (PermissionError, FileNotFoundError, OSError) as error:
        print(
            f"[scan] cannot access root={normalize_path_text(str(root))} "
            f"error={type(error).__name__}: {error}",
            flush=True,
        )
        return [], []

    all_results = load_existing_labels(output_path) if resume else []
    seen_paths = {path_key(item["path"]) for item in all_results}
    existing_labels_by_key = {
        path_key(item["path"]): list(item["labels"])
        for item in all_results
    }
    if resume:
        print(f"[resume] loaded {len(all_results)} existing labels", flush=True)

    debug_items: list[dict] = []
    if resume and debug_output_path is not None:
        debug_items = load_debug_decisions(debug_output_path)
        print(f"[resume] loaded {len(debug_items)} debug decisions", flush=True)
    debug_index = {path_key(item["path"]): index for index, item in enumerate(debug_items)}
    debug_by_key = {path_key(item["path"]): item for item in debug_items}

    queue: deque[ScanNode] = deque([ScanNode(path=root, depth=0)])
    batch_index = 0

    while queue:
        nodes: list[ScanNode] = []
        while queue and len(nodes) < batch_size:
            nodes.append(queue.popleft())

        pending_to_label: list[PendingSummary] = []
        for node in nodes:
            if node.depth > max_depth:
                continue
            if node.depth > 0 and should_skip_dir(node.path):
                continue

            summary, child_paths = summarize_folder(
                folder=node.path,
                depth=node.depth,
                max_files_per_folder=max_files_per_folder,
            )
            if summary is None:
                continue

            pending = PendingSummary(
                summary=summary,
                child_paths=child_paths,
                parent_path=node.parent_path,
                parent_labels=node.parent_labels,
            )
            key = path_key(summary.path)

            if resume and key in seen_paths:
                debug_decision = debug_by_key.get(key)
                if debug_decision is None:
                    print(
                        f"[resume] warning: {summary.path} is already labeled but has no "
                        "debug decision; not descending",
                        flush=True,
                    )
                    continue

                labels = existing_labels_by_key.get(key, debug_decision["labels"])
                normalized_decision = normalize_decision_by_rules(
                    decision={
                        **debug_decision,
                        "labels": labels,
                        "should_descend": debug_decision.get("should_descend"),
                    },
                    summary=summary,
                    parent_labels=node.parent_labels,
                )
                enqueue_children(queue, pending, normalized_decision, max_depth)
                continue

            pending_to_label.append(pending)

        if not pending_to_label:
            continue

        batch_index += 1
        print(
            f"[label] batch={batch_index} size={len(pending_to_label)} queue={len(queue)}",
            flush=True,
        )
        decisions = label_batch_with_llm(
            client=client,
            model=model,
            pending_summaries=pending_to_label,
        )

        pending_by_key = {
            path_key(pending.summary.path): pending
            for pending in pending_to_label
        }
        for decision in decisions:
            key = path_key(decision["path"])
            pending = pending_by_key.get(key)
            if pending is None:
                continue

            added = add_results(all_results, [to_training_item(decision)], seen_paths)
            if added:
                existing_labels_by_key[key] = list(decision["labels"])
            debug_item = to_debug_item(decision, pending)
            upsert_debug_item(debug_items, debug_index, debug_item)
            debug_by_key[key] = debug_item
            enqueue_children(queue, pending, decision, max_depth)

        try:
            save_labels(output_path, all_results)
            if debug_output_path is not None:
                save_debug_decisions(debug_output_path, debug_items)
            print(f"[save] wrote {len(all_results)} labels", flush=True)
        except OSError as error:
            print(
                f"[save] failed: {type(error).__name__}: {error}",
                flush=True,
            )

    print(f"[done] labeled={len(all_results)} debug={len(debug_items)}", flush=True)
    return all_results, debug_items


def iter_batches(items: list[FolderSummary], batch_size: int) -> list[list[FolderSummary]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan folders and use a Minimax/OpenAI-compatible LLM to generate folder labels.",
    )
    parser.add_argument("--root", required=True, help="Root directory to scan, for example D:/")
    parser.add_argument("--output", required=True, help="Output folder_labels.json path")
    parser.add_argument("--api-key", default=os.getenv("MINIMAX_API_KEY"), help="Minimax API key")
    parser.add_argument(
        "--base-url",
        default="https://api.minimax.chat/v1",
        help="Minimax/OpenAI-compatible API base URL",
    )
    parser.add_argument("--model", default="MiniMax-M2.7", help="LLM model name")
    parser.add_argument("--max-depth", type=int, default=4, help="Maximum scan depth")
    parser.add_argument(
        "--max-files-per-folder",
        type=int,
        default=80,
        help="Maximum sampled file names per folder",
    )
    parser.add_argument("--batch-size", type=int, default=10, help="Folders per LLM request")
    parser.add_argument("--resume", action="store_true", help="Skip paths already in output")
    parser.add_argument("--dry-run", action="store_true", help="Scan and print summaries only")
    parser.add_argument(
        "--debug-output",
        help="Optional debug JSON path with should_descend and reason",
    )

    args = parser.parse_args(argv)
    if args.max_depth < 0:
        parser.error("--max-depth must be >= 0")
    if args.max_files_per_folder < 0:
        parser.error("--max-files-per-folder must be >= 0")
    if args.batch_size <= 0:
        parser.error("--batch-size must be > 0")
    return args


def dry_run(summaries: list[FolderSummary]) -> None:
    print("[dry-run] showing first 20 folder summaries", flush=True)
    print(
        json.dumps(
            [asdict(summary) for summary in summaries[:20]],
            ensure_ascii=False,
            indent=4,
        ),
        flush=True,
    )


def print_done(summaries: list[FolderSummary], seen_paths: set[str]) -> None:
    scanned_paths = {path_key(summary.path) for summary in summaries}
    success_paths = scanned_paths & seen_paths
    failed = len(scanned_paths) - len(success_paths)
    print(
        f"[done] total={len(summaries)} success={len(success_paths)} failed={failed}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    output = Path(args.output).expanduser().resolve(strict=False)
    debug_output = (
        Path(args.debug_output).expanduser().resolve(strict=False)
        if args.debug_output
        else None
    )

    if args.dry_run:
        print(f"[scan] root={args.root} max_depth={args.max_depth}", flush=True)
        summaries = scan_folders(
            root=root,
            max_depth=args.max_depth,
            max_files_per_folder=args.max_files_per_folder,
        )
        print(f"[scan] collected {len(summaries)} folders", flush=True)
        dry_run(summaries)
        return 0

    if not args.api_key:
        print(
            "Missing Minimax API key. Provide --api-key or set MINIMAX_API_KEY.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    try:
        from openai import OpenAI
    except ImportError as error:
        print(
            "[error] openai package is not installed. Install it with: pip install openai",
            flush=True,
        )
        print(f"[error] {type(error).__name__}: {error}", flush=True)
        return 1

    client = OpenAI(api_key=args.api_key, base_url=args.base_url)
    scan_and_label_with_llm(
        root=root,
        client=client,
        model=args.model,
        output_path=output,
        max_depth=args.max_depth,
        max_files_per_folder=args.max_files_per_folder,
        batch_size=args.batch_size,
        resume=args.resume,
        debug_output_path=debug_output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
