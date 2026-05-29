"""High-performance file path retrieval via Everything SDK."""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mainFunctions import EverythingSdkSearch


JsonDict = dict[str, Any]

_MISSING = object()


class ContractError(ValueError):
    """Raised when LLM-generated code violates the result variable contract."""


class FilterCompileError(ContractError):
    """Raised when LLM-generated code contains a syntax error."""


# ── Built-in extension defaults ────────────────────────────────────────────────

_BUILTIN_EXTENSIONS: dict[str, list[str]] = {
    "document": [
        ".csv", ".doc", ".docx", ".htm", ".html", ".json", ".jsonl",
        ".markdown", ".md", ".pdf", ".ppt", ".pptx", ".rtf", ".tex",
        ".txt", ".xls", ".xlsx", ".xml", ".yaml", ".yml",
    ],
    "image": [
        ".bmp", ".gif", ".heic", ".ico", ".jpeg", ".jpg", ".png",
        ".psd", ".raw", ".svg", ".tif", ".tiff", ".webp",
    ],
    "video": [
        ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4",
        ".mpeg", ".mpg", ".rmvb", ".ts", ".webm", ".wmv",
    ],
}

_CONFIG_ENV_VAR = "EVERYTHING_EXTENSIONS_CONFIG"
_CONFIG_FILENAME = "extensions_config.json"


def _normalise_extensions(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for v in values:
        ext = str(v).strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext not in seen:
            seen.add(ext)
            result.append(ext)
    return result


def _load_extensions_config(
    config_path: str | Path | None = None,
) -> dict[str, frozenset[str]]:
    """Load extension→category mapping from JSON, falling back to built-in defaults."""
    path: Path | None = None

    if config_path is not None:
        path = Path(config_path)
    elif _CONFIG_ENV_VAR in os.environ:
        path = Path(os.environ[_CONFIG_ENV_VAR])
    else:
        path = Path(__file__).parent / _CONFIG_FILENAME

    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not raw:
                raise ValueError("Config must be a non-empty JSON object")

            loaded: dict[str, frozenset[str]] = {}
            for category, exts in raw.items():
                if not isinstance(exts, list):
                    raise ValueError(f"Extensions for '{category}' must be a list")
                normalised = _normalise_extensions(exts)
                if normalised:
                    loaded[category] = frozenset(normalised)

            if loaded:
                return loaded

            warnings.warn(
                f"Extension config at '{path}' produced no valid entries; "
                "using built-in defaults.",
                stacklevel=2,
            )
        except Exception as exc:
            warnings.warn(
                f"Failed to load extension config from '{path}': {exc}. "
                "Using built-in defaults.",
                stacklevel=2,
            )
    elif path is not None and config_path is not None:
        warnings.warn(
            f"Extension config not found at '{path}'; using built-in defaults.",
            stacklevel=2,
        )

    return {
        category: frozenset(_normalise_extensions(exts))
        for category, exts in _BUILTIN_EXTENSIONS.items()
    }


def reload_extensions_config(config_path: str | Path | None = None) -> None:
    """Reload extension maps at runtime (e.g. after editing extensions_config.json)."""
    global EXTENSIONS_BY_CATEGORY, CATEGORY_BY_EXTENSION, DEFAULT_CATEGORIES
    global DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

    EXTENSIONS_BY_CATEGORY = _load_extensions_config(config_path)
    CATEGORY_BY_EXTENSION = {
        ext: category
        for category, exts in EXTENSIONS_BY_CATEGORY.items()
        for ext in exts
    }
    DEFAULT_CATEGORIES = tuple(EXTENSIONS_BY_CATEGORY)
    DOCUMENT_EXTENSIONS = EXTENSIONS_BY_CATEGORY.get("document", frozenset())
    IMAGE_EXTENSIONS = EXTENSIONS_BY_CATEGORY.get("image", frozenset())
    VIDEO_EXTENSIONS = EXTENSIONS_BY_CATEGORY.get("video", frozenset())


def get_meta(path: str) -> dict[str, Any]:
    """Return metadata for a file path by querying the metadata database.

    TODO: Connect to the actual metadata database.
          Replace the stub body below with a real DB lookup.

    Signature contract (do not change — LLM code depends on these keys):
        path      (str)        : the queried path, as supplied
        name      (str)        : filename including extension
        extension (str)        : lowercase dot-prefixed extension, e.g. ".pdf"
        size      (int | None) : file size in bytes; None if not in DB
        mtime     (float | None): last-modified Unix timestamp; None if not in DB
        ctime     (float | None): created/inode-change Unix timestamp; None if not in DB
    """
    # TODO: replace with actual DB lookup, e.g.:
    #   row = db.execute("SELECT size, mtime, ctime FROM files WHERE path=?", (path,)).fetchone()
    #   return {"path": path, "name": ..., "extension": ...,
    #           "size": row["size"], "mtime": row["mtime"], "ctime": row["ctime"]}
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    return {
        "path": path,
        "name": name,
        "extension": ext,
        "size": None,
        "mtime": None,
        "ctime": None,
    }


# ── Module-level extension maps (built once at import) ─────────────────────────

EXTENSIONS_BY_CATEGORY: dict[str, frozenset[str]] = _load_extensions_config()
CATEGORY_BY_EXTENSION: dict[str, str] = {
    ext: category
    for category, exts in EXTENSIONS_BY_CATEGORY.items()
    for ext in exts
}
DEFAULT_CATEGORIES: tuple[str, ...] = tuple(EXTENSIONS_BY_CATEGORY)

# Named frozensets kept for backwards compatibility
DOCUMENT_EXTENSIONS: frozenset[str] = EXTENSIONS_BY_CATEGORY.get("document", frozenset())
IMAGE_EXTENSIONS: frozenset[str] = EXTENSIONS_BY_CATEGORY.get("image", frozenset())
VIDEO_EXTENSIONS: frozenset[str] = EXTENSIONS_BY_CATEGORY.get("video", frozenset())


# ── JSON Schema for FileFilter — use directly as an LLM tool parameter def ────

FILE_FILTER_SCHEMA: JsonDict = {
    "type": "object",
    "description": "Filter configuration for EverythingFilesExtractor.",
    "properties": {
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "File-category groups to include. Each name maps to an extension set "
                "defined in extensions_config.json (built-in: 'document', 'image', 'video'). "
                "Drives the ext: clause sent to Everything."
            ),
            "default": list(DEFAULT_CATEGORIES),
        },
        "root_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Restrict results to files under these directories. "
                "Encoded into the Everything query — not a post-filter."
            ),
            "default": [],
        },
        "include_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Only return paths that contain ALL of these substrings "
                "(case-insensitive match against the full path string)."
            ),
            "default": [],
        },
        "exclude_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exclude paths that contain ANY of these substrings "
                "(case-insensitive match against the full path string)."
            ),
            "default": [],
        },
        "extensions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exact extension allowlist, e.g. [\".pdf\", \".docx\"]. "
                "When non-empty, overrides 'categories' for the Everything ext: query."
            ),
            "default": [],
        },
        "exclude_extensions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extensions to exclude from results, e.g. [\".tmp\"].",
            "default": [],
        },
        "limit": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Maximum number of file paths to return. Null means no limit.",
            "default": None,
        },
    },
    "additionalProperties": False,
}


LLM_FILTER_CONTRACT: str = """\
LLM Filter Code Contract
========================

Your code must assign a variable named `result` before it finishes.
The executor reads `result` after exec() and dispatches on its type.

── Allowed forms ─────────────────────────────────────────────────────────────

Form 1 — FileFilter instance
  Everything-backed search.  Fastest; no custom logic.

    result = FileFilter(
        categories=("document", "image"),
        root_paths=("D:\\\\Work",),
        exclude_keywords=("temp", "draft"),
        limit=500,
    )

Form 2 — plain dict  (converted via FileFilter.from_dict)
  Same as Form 1 but JSON-serialisable.  Unknown keys are ignored.

    result = {
        "categories": ["document"],
        "root_paths": ["D:\\\\Work"],
        "include_keywords": ["invoice"],
        "limit": 200,
    }

Form 3 — list[str]  (custom retrieval + arbitrary filtering)
  Use search_paths() to get candidates, then filter freely with re / get_meta.
  Every element must be a str.  Duplicates are silently removed.

    candidates = search_paths(categories=["document"], root_paths=["D:\\\\Work"])
    result = [
        p for p in candidates
        if re.search(r"invoice_\\d{4}", p, re.IGNORECASE)       # regex on path
        and (get_meta(p)["size"]  or 0) > 102_400               # AND > 100 KB
        and (get_meta(p)["mtime"] or 0) > 1_700_000_000         # AND after epoch ts
        and not re.search(r"\\\\archive\\\\", p, re.IGNORECASE) # AND NOT archived
    ]

── Pre-injected namespace ────────────────────────────────────────────────────

  FileFilter                  declare structured filters (Form 1 / 2)
  ExtractionResult            read-only reference; do not construct
  EXTENSIONS_BY_CATEGORY      dict[str, frozenset[str]]
  CATEGORY_BY_EXTENSION       dict[str, str]
  DEFAULT_CATEGORIES          tuple[str, ...]
  re                          standard-library re module
  get_meta(path: str) -> dict
      keys: path, name, extension (str)
            size (int|None), mtime (float|None), ctime (float|None)
  search_paths(
      categories : Iterable[str] | None = None,
      extensions : Iterable[str] | None = None,
      root_paths : Iterable[str] | None = None,
  ) -> list[str]              fetch candidate paths from Everything index

── Contract violations ───────────────────────────────────────────────────────

  `result` not defined              → ContractError
  `result` wrong type               → ContractError  (lists all allowed types)
  `result` list contains non-str    → ContractError  (offending index reported)
  `result` dict fails from_dict     → ContractError  (original error forwarded)
"""


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FileFilter:
    """Path-based filters — no filesystem I/O. Serialisable to/from plain dict."""

    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    root_paths: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    exclude_extensions: tuple[str, ...] = ()
    limit: int | None = None

    def to_dict(self) -> JsonDict:
        return {
            "categories": list(self.categories),
            "root_paths": list(self.root_paths),
            "include_keywords": list(self.include_keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "extensions": list(self.extensions),
            "exclude_extensions": list(self.exclude_extensions),
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileFilter:
        """Build a FileFilter from a plain dict. Unknown fields are silently ignored."""

        def _str_list(key: str) -> list[str]:
            value = data.get(key, [])
            if isinstance(value, str):
                value = [value]
            return [str(v).strip() for v in value if str(v).strip()]

        def _norm_keywords(key: str) -> tuple[str, ...]:
            return tuple(v.lower() for v in _str_list(key))

        def _norm_exts(key: str) -> tuple[str, ...]:
            return tuple(_normalise_extensions(_str_list(key)))

        categories_raw = tuple(c.lower() for c in _str_list("categories"))
        categories = tuple(c for c in categories_raw if c in EXTENSIONS_BY_CATEGORY)
        if not categories:
            categories = DEFAULT_CATEGORIES

        limit_raw = data.get("limit")
        limit: int | None = None
        if limit_raw is not None:
            try:
                limit = max(0, int(limit_raw))
            except (TypeError, ValueError):
                limit = None

        return cls(
            categories=categories,
            root_paths=tuple(_str_list("root_paths")),
            include_keywords=_norm_keywords("include_keywords"),
            exclude_keywords=_norm_keywords("exclude_keywords"),
            extensions=_norm_exts("extensions"),
            exclude_extensions=_norm_exts("exclude_extensions"),
            limit=limit,
        )


@dataclass
class ExtractionResult:
    """Everything extraction result: file paths plus a compact summary."""

    query: str
    total_results: int
    scanned: int
    kept: int
    paths: list[str] = field(default_factory=list)
    startup_message: str = ""
    message: str = ""

    def to_dict(self) -> JsonDict:
        return {
            "query": self.query,
            "startup_message": self.startup_message,
            "message": self.message,
            "total_results": self.total_results,
            "scanned": self.scanned,
            "kept": self.kept,
            "paths": self.paths,
        }


# ── Core extractor ─────────────────────────────────────────────────────────────

class EverythingFilesExtractor:
    """Retrieve file paths from Everything by extension — no per-file filesystem access."""

    def __init__(self, search_engine: EverythingSdkSearch | None = None):
        self.search_engine = search_engine or EverythingSdkSearch(result_limit=-1)
        self.started = False

    def extract(self, filters: FileFilter | None = None) -> ExtractionResult:
        """Return file paths from Everything that pass the supplied path-only filters."""
        active_filter = filters or FileFilter()
        categories = self.normalized_categories(active_filter.categories)
        query = self.build_query(categories, active_filter.extensions, active_filter.root_paths)

        startup_message = self.start()
        paths, message = self.search_engine.search(query)

        filtered = self.filter_paths(paths, active_filter)
        if active_filter.limit is not None and active_filter.limit >= 0:
            filtered = filtered[: active_filter.limit]

        return ExtractionResult(
            query=query,
            startup_message=startup_message,
            message=message,
            total_results=self.search_engine.total_result_count(),
            scanned=len(paths),
            kept=len(filtered),
            paths=filtered,
        )

    def start(self) -> str:
        """Start Everything once per extractor instance."""
        if self.started:
            return ""
        self.started = True
        return self.search_engine.start()

    def build_query(
        self,
        categories: Iterable[str] = DEFAULT_CATEGORIES,
        extensions: Iterable[str] = (),
        root_paths: Iterable[str] = (),
    ) -> str:
        """Build an Everything query that restricts results to root_paths at index level."""
        extension_values = self.normalized_extensions(extensions)
        if not extension_values:
            for category in self.normalized_categories(categories):
                extension_values.extend(sorted(EXTENSIONS_BY_CATEGORY[category]))

        everything_exts = ";".join(
            ext.lstrip(".") for ext in sorted(set(extension_values))
        )
        ext_clause = f"file: ext:{everything_exts}"

        roots = [str(r).strip() for r in root_paths if str(r).strip()]
        if not roots:
            return ext_clause

        def _normalise_root(r: str) -> str:
            r = r.rstrip("/\\")
            if len(r) == 2 and r[1] == ":":
                return r + "\\"
            return r + "\\"

        if len(roots) == 1:
            path_clause = f'"{_normalise_root(roots[0])}"'
        else:
            parts = " | ".join(f'"{_normalise_root(r)}"' for r in roots)
            path_clause = f"({parts})"

        return f"{path_clause} {ext_clause}"

    @classmethod
    def filter_paths(cls, paths: Iterable[str], filters: FileFilter) -> list[str]:
        """Filter raw Everything paths using string-only checks — zero filesystem access."""
        roots = tuple(
            cls._fast_normalized_path(root)
            for root in filters.root_paths
            if str(root).strip()
        )
        include_keywords = filters.include_keywords
        exclude_keywords = filters.exclude_keywords
        extensions = set(filters.extensions) if filters.extensions else None
        excluded_extensions = set(filters.exclude_extensions) if filters.exclude_extensions else None

        result = []
        for path_text in paths:
            suffix = os.path.splitext(path_text)[1].lower()

            if extensions is not None and suffix not in extensions:
                continue
            if excluded_extensions is not None and suffix in excluded_extensions:
                continue

            if roots:
                norm_path = cls._fast_normalized_path(path_text)
                if not any(cls._is_normalized_path_child(norm_path, root) for root in roots):
                    continue

            if include_keywords or exclude_keywords:
                searchable = path_text.lower()
                if include_keywords and not any(kw in searchable for kw in include_keywords):
                    continue
                if exclude_keywords and any(kw in searchable for kw in exclude_keywords):
                    continue

            result.append(path_text)

        return result

    @classmethod
    def extract_everything_files(
        cls,
        filters: FileFilter | dict[str, Any] | None = None,
        search_engine: EverythingSdkSearch | None = None,
    ) -> JsonDict:
        """Convenience wrapper. Accepts a FileFilter, a plain dict, or None."""
        if isinstance(filters, dict):
            filters = FileFilter.from_dict(filters)
        return cls(search_engine=search_engine).extract(filters).to_dict()

    @staticmethod
    def normalized_categories(categories: Iterable[str]) -> tuple[str, ...]:
        values = []
        for category in categories or DEFAULT_CATEGORIES:
            value = str(category).strip().lower()
            if value in EXTENSIONS_BY_CATEGORY and value not in values:
                values.append(value)
        return tuple(values or DEFAULT_CATEGORIES)

    @staticmethod
    def normalized_extensions(values: Iterable[str]) -> list[str]:
        return _normalise_extensions(values)

    @staticmethod
    def normalized_keywords(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            str(v).strip().lower() for v in values or () if str(v).strip()
        )

    @staticmethod
    def _fast_normalized_path(path_text: str) -> str:
        return os.path.normcase(os.path.normpath(path_text))

    @classmethod
    def is_same_or_child_path(cls, path_text: str, root_text: str) -> bool:
        path = cls._fast_normalized_path(path_text)
        root = cls._fast_normalized_path(root_text)
        return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

    @staticmethod
    def _is_normalized_path_child(norm_path: str, norm_root: str) -> bool:
        return norm_path == norm_root or norm_path.startswith(norm_root.rstrip(os.sep) + os.sep)


# ── LLM code executor ──────────────────────────────────────────────────────────

def compile_filter_code(code: str, filename: str = "<filter_script>"):
    """Pre-compile filter code, raising FilterCompileError on SyntaxError.

    Returns a code object ready for exec() — no redundant compilation at runtime.
    """
    try:
        return compile(code, filename, "exec")
    except SyntaxError as exc:
        raise FilterCompileError(
            f"Syntax error in filter code at line {exc.lineno}: {exc.msg}. "
            f"  {exc.text or ''}".rstrip()
        ) from exc


class LLMFilterExecutor:
    """Execute LLM-written Python code that produces a `result` variable.

    Dispatches on `result` type and always returns ExtractionResult.to_dict().
    See LLM_FILTER_CONTRACT for the full contract specification.

    Usage::

        executor = LLMFilterExecutor()
        output = executor.run(llm_code_string)
        paths = output["paths"]
    """

    def __init__(self, search_engine: EverythingSdkSearch | None = None):
        self._extractor = EverythingFilesExtractor(search_engine)

    def run(self, code: str) -> JsonDict:
        """Execute `code`, extract `result`, dispatch on type.

        Raises:
            FilterCompileError: if `code` contains a syntax error.
            ContractError: if `result` is missing, wrong type, or malformed.
            Any runtime exception raised by the LLM code propagates unchanged.
        """
        code_obj = compile_filter_code(code)
        namespace = self._build_namespace()
        exec(code_obj, namespace)  # noqa: S102 — caller is trusted
        result = namespace.get("result", _MISSING)
        return self._dispatch(result).to_dict()

    def _build_namespace(self) -> dict[str, Any]:
        extractor = self._extractor

        def search_paths(
            categories: Iterable[str] | None = None,
            extensions: Iterable[str] | None = None,
            root_paths: Iterable[str] | None = None,
        ) -> list[str]:
            """Fetch candidate paths from Everything — no filesystem access."""
            _filter = FileFilter(
                categories=tuple(categories) if categories is not None else DEFAULT_CATEGORIES,
                extensions=tuple(extensions) if extensions is not None else (),
                root_paths=tuple(root_paths) if root_paths is not None else (),
            )
            return extractor.extract(_filter).paths

        return {
            "FileFilter": FileFilter,
            "ExtractionResult": ExtractionResult,
            "EXTENSIONS_BY_CATEGORY": EXTENSIONS_BY_CATEGORY,
            "CATEGORY_BY_EXTENSION": CATEGORY_BY_EXTENSION,
            "DEFAULT_CATEGORIES": DEFAULT_CATEGORIES,
            "re": re,
            "get_meta": get_meta,
            "search_paths": search_paths,
        }

    def _dispatch(self, result: Any) -> ExtractionResult:
        if result is _MISSING:
            raise ContractError(
                "LLM code did not define `result`. "
                "Expected: FileFilter instance, dict, or list[str]. "
                "See LLM_FILTER_CONTRACT for examples."
            )

        if isinstance(result, FileFilter):
            return self._extractor.extract(result)

        if isinstance(result, dict):
            try:
                f = FileFilter.from_dict(result)
            except Exception as exc:
                raise ContractError(
                    f"`result` dict could not be converted to FileFilter: {exc}. "
                    "See FILE_FILTER_SCHEMA for allowed fields."
                ) from exc
            return self._extractor.extract(f)

        if isinstance(result, list):
            return self._accept_path_list(result)

        raise ContractError(
            f"`result` has unsupported type {type(result).__name__!r}. "
            "Allowed: FileFilter instance, dict, or list[str]. "
            "See LLM_FILTER_CONTRACT for the full contract."
        )

    @staticmethod
    def _accept_path_list(paths: list[Any]) -> ExtractionResult:
        validated: list[str] = []
        seen: set[str] = set()
        for i, item in enumerate(paths):
            if not isinstance(item, str):
                raise ContractError(
                    f"`result` list element at index {i} is {type(item).__name__!r}; "
                    "all elements must be file path strings. "
                    "Contract form 3 requires list[str]."
                )
            if item not in seen:
                seen.add(item)
                validated.append(item)

        return ExtractionResult(
            query="llm-custom",
            total_results=len(validated),
            scanned=len(validated),
            kept=len(validated),
            paths=validated,
        )


# ── Filter script storage ──────────────────────────────────────────────────────
#
# SECURITY NOTE: scripts stored here are executable Python code.
# Write access to the scripts directory is equivalent to code execution
# privilege.  Treat it like source code and keep it out of world-writable paths.

_SCRIPTS_DIR_ENV = "AIFM_SCRIPTS_DIR"
_SCRIPTS_DIRNAME = "filter_scripts"


def _base_dir() -> Path:
    """Resolve the application base directory, compatible with PyInstaller exe.

    Packed (frozen):  Path(sys.executable).parent  — directory containing the .exe
    Plain Python:     Path(__file__).parent         — directory containing this file
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _slug(name: str) -> str:
    """Convert a script name to a safe filename stem.

    Keeps only [a-z0-9_-], strips leading/trailing separators.
    Raises ValueError on empty result or residual path characters.
    """
    s = name.strip().lower().replace(" ", "_")
    s = re.sub(r"[^a-z0-9_-]", "", s).strip("_-")
    if not s:
        raise ValueError(
            f"Script name {name!r} produces an empty slug after sanitisation. "
            "Use alphanumeric characters, spaces, underscores, or hyphens."
        )
    if ".." in s or "/" in s or "\\" in s or os.sep in s:
        raise ValueError(
            f"Slug {s!r} derived from {name!r} contains illegal path characters."
        )
    return s


def _scripts_dir(override: str | Path | None = None) -> Path:
    """Return the filter-scripts directory, creating it if it does not exist.

    Priority: override arg → $AIFM_SCRIPTS_DIR env var → _base_dir()/filter_scripts/
    """
    if override is not None:
        base = Path(override)
    elif _SCRIPTS_DIR_ENV in os.environ:
        base = Path(os.environ[_SCRIPTS_DIR_ENV])
    else:
        base = _base_dir() / _SCRIPTS_DIRNAME

    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"Cannot create scripts directory '{base}': {exc}. "
            "Check write permissions."
        ) from exc

    return base


def list_scripts(scripts_dir: str | Path | None = None) -> list[str]:
    """Return the names (stems) of all filter scripts, sorted alphabetically."""
    return sorted(p.stem for p in _scripts_dir(scripts_dir).glob("*.py"))


def read_script(name: str, *, scripts_dir: str | Path | None = None) -> str:
    """Return the full content of a saved filter script.

    Raises FileNotFoundError listing available names when name is unknown.
    """
    directory = _scripts_dir(scripts_dir)
    path = directory / f"{_slug(name)}.py"
    if not path.exists():
        available = sorted(p.stem for p in directory.glob("*.py"))
        avail_str = ", ".join(repr(n) for n in available) or "(none)"
        raise FileNotFoundError(
            f"No script named {name!r} (looked for '{path}'). "
            f"Available: {avail_str}."
        )
    return path.read_text(encoding="utf-8")


def write_script(
    name: str,
    code: str,
    *,
    overwrite: bool = True,
    search_engine: EverythingSdkSearch | None = None,
    scripts_dir: str | Path | None = None,
) -> Path:
    """Validate and save LLM filter code as a pure .py script file.

    Validation: the code is compiled and executed via LLMFilterExecutor.run()
    before writing.  Any exception (FilterCompileError, ContractError,
    RuntimeError …) aborts the write — no file is created or modified on failure.

    NOTE: validation triggers a real Everything search.  Scripts whose result
    has no limit may scan large result sets and take noticeable time.

    Args:
        name:        Script name (slug-converted for the filename).
        code:        Python code following LLM_FILTER_CONTRACT.
        overwrite:   Raise FileExistsError when False and a script with this
                     name already exists.  Default is True (overwrite allowed).
        search_engine: Search engine for the validation run.
        scripts_dir: Override the storage directory.

    Returns:
        Path to the written file.
    """
    slug = _slug(name)
    directory = _scripts_dir(scripts_dir)
    target = directory / f"{slug}.py"

    if not overwrite and target.exists():
        raise FileExistsError(
            f"A script named {name!r} already exists at '{target}'. "
            "Pass overwrite=True to replace it."
        )

    # Validate — any exception propagates; disk is not touched
    LLMFilterExecutor(search_engine).run(code)

    try:
        target.write_text(code, encoding="utf-8")
    except OSError as exc:
        raise OSError(
            f"Failed to write script {name!r} to '{target}': {exc}. "
            "Check write permissions."
        ) from exc

    return target


def run_script(
    name: str,
    *,
    search_engine: EverythingSdkSearch | None = None,
    scripts_dir: str | Path | None = None,
) -> JsonDict:
    """Load a filter script by name and execute it via LLMFilterExecutor."""
    code = read_script(name, scripts_dir=scripts_dir)
    return LLMFilterExecutor(search_engine).run(code)


def delete_script(
    name: str,
    *,
    scripts_dir: str | Path | None = None,
) -> None:
    """Delete a filter script file.

    Raises FileNotFoundError listing available names when name is unknown.
    """
    directory = _scripts_dir(scripts_dir)
    path = directory / f"{_slug(name)}.py"

    if not path.exists():
        available = sorted(p.stem for p in directory.glob("*.py"))
        avail_str = ", ".join(repr(n) for n in available) or "(none)"
        raise FileNotFoundError(
            f"No script named {name!r} (looked for '{path}'). "
            f"Available: {avail_str}."
        )

    try:
        path.unlink()
    except OSError as exc:
        raise OSError(
            f"Failed to delete script {name!r} at '{path}': {exc}. "
            "Check file permissions."
        ) from exc
