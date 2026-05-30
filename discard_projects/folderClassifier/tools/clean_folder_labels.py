from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any


LABELS = (
    "app",
    "db",
    "cache",
    "user_data",
    "media",
    "document",
)
LABEL_SET = set(LABELS)
INVALID_LABELS = {"mixed", "other"}


@dataclass(frozen=True)
class FolderLabel:
    path: str
    labels: tuple[str, ...]
    depth: int


def normalize_path(path: str) -> str:
    normalized = " ".join(str(path).strip().split())
    normalized = str(Path(normalized).expanduser()).replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if len(normalized) > 3:
        normalized = normalized.rstrip("/")
    return normalized


def path_key(path: str) -> str:
    return normalize_path(path).casefold()


def path_parts(path: str) -> list[str]:
    normalized = normalize_path(path)
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[2:]
    return [part for part in normalized.strip("/").split("/") if part]


def path_depth(path: str) -> int:
    return len(path_parts(path))


def project_key(path: str) -> str:
    parts = path_parts(path)
    if len(parts) >= 2:
        return "/".join(parts[:2])
    if parts:
        return parts[0]
    return normalize_path(path)


def normalize_labels(raw_labels: Any) -> tuple[str, ...] | None:
    if not isinstance(raw_labels, list):
        return None

    labels: list[str] = []
    seen: set[str] = set()
    for raw_label in raw_labels:
        if not isinstance(raw_label, str):
            continue
        label = raw_label.strip().lower()
        if not label or label in INVALID_LABELS or label not in LABEL_SET:
            continue
        if label not in seen:
            labels.append(label)
            seen.add(label)

    if not labels:
        return None
    return tuple(labels)


def load_folder_labels(input_path: Path) -> tuple[list[FolderLabel], dict[str, Any]]:
    try:
        with input_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(f"Input file not found: {input_path}") from None
    except JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {input_path}: {error}") from error

    if not isinstance(data, list):
        raise ValueError("Input JSON must be a list.")

    samples: list[FolderLabel] = []
    invalid_items = 0
    invalid_labels = 0
    missing_paths = 0

    for item in data:
        if not isinstance(item, dict):
            invalid_items += 1
            continue

        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            missing_paths += 1
            continue

        labels = normalize_labels(item.get("labels"))
        if labels is None:
            invalid_labels += 1
            continue

        path = normalize_path(raw_path)
        samples.append(FolderLabel(path=path, labels=labels, depth=path_depth(path)))

    load_stats = {
        "raw_items": len(data),
        "valid_samples": len(samples),
        "invalid_items": invalid_items,
        "missing_paths": missing_paths,
        "invalid_labels": invalid_labels,
    }
    return samples, load_stats


def counter_to_sorted_dict(counter: Counter[Any], limit: int | None = None) -> dict[str, int]:
    items = counter.most_common(limit)
    return {str(key): count for key, count in items}


def summarize(samples: list[FolderLabel], load_stats: dict[str, Any], top_n: int) -> dict[str, Any]:
    path_counts = Counter(path_key(sample.path) for sample in samples)
    duplicate_paths = {
        sample.path
        for sample in samples
        if path_counts[path_key(sample.path)] > 1
    }

    label_counts = Counter(label for sample in samples for label in sample.labels)
    label_combo_counts = Counter(",".join(sample.labels) for sample in samples)
    label_size_counts = Counter(len(sample.labels) for sample in samples)
    depth_counts = Counter(sample.depth for sample in samples)
    project_counts = Counter(project_key(sample.path) for sample in samples)
    top_folder_counts = Counter(path_parts(sample.path)[0] if path_parts(sample.path) else sample.path for sample in samples)

    multi_label_samples = sum(1 for sample in samples if len(sample.labels) > 1)
    max_depth = max((sample.depth for sample in samples), default=0)
    average_depth = (
        round(sum(sample.depth for sample in samples) / len(samples), 2)
        if samples
        else 0
    )

    return {
        **load_stats,
        "unique_paths": len(path_counts),
        "duplicate_path_count": len(duplicate_paths),
        "multi_label_samples": multi_label_samples,
        "single_label_samples": len(samples) - multi_label_samples,
        "max_depth": max_depth,
        "average_depth": average_depth,
        "label_distribution": {label: label_counts.get(label, 0) for label in LABELS},
        "label_combo_distribution_top": counter_to_sorted_dict(label_combo_counts, top_n),
        "label_count_distribution": counter_to_sorted_dict(label_size_counts),
        "depth_distribution": counter_to_sorted_dict(depth_counts),
        "top_projects": counter_to_sorted_dict(project_counts, top_n),
        "top_level_folders": counter_to_sorted_dict(top_folder_counts, top_n),
        "duplicate_paths_preview": sorted(duplicate_paths, key=str.casefold)[:top_n],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_input = Path(__file__).resolve().parents[1] / "folder_labels.json"
    parser = argparse.ArgumentParser(
        description="Print statistics for folder_labels.json.",
    )
    parser.add_argument(
        "--input",
        default=str(default_input),
        help="Input folder_labels.json. Defaults to folderClassifier/folder_labels.json",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Maximum number of top entries to print for ranked distributions",
    )

    args = parser.parse_args(argv)
    if args.top_n <= 0:
        parser.error("--top-n must be > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input).expanduser()

    try:
        samples, load_stats = load_folder_labels(input_path)
    except (OSError, ValueError) as error:
        print(f"[error] {error}", file=sys.stderr, flush=True)
        return 1

    stats = summarize(samples=samples, load_stats=load_stats, top_n=args.top_n)
    print(json.dumps(stats, ensure_ascii=False, indent=4), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
