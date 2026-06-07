"""Index Everything-discovered files by their owning installed application.

The ``build_index()`` method reads ``app_attribution_rules.json`` for
file→app mapping rules (root-prefix, keyword, regex) and falls back to
scanning installed apps from the Windows registry when no rule file
exists.  Query helpers ``apps_with_files()`` and ``files_for_app()``
remain unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path
from files_extractors import EverythingFilesExtractor, FileFilter

# ── rule file path ───────────────────────────────────────────────────────────

_DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "app_attribution_rules.json"


# ═══════════════════════════════════════════════════════════════════════════════
# Compile-time structures for preprocessed rules
# ═══════════════════════════════════════════════════════════════════════════════

class CompiledRules:
    """Pre-processed matching structures built from the JSON rules list."""

    __slots__ = ("roots", "regexes", "keywords")

    def __init__(
        self,
        roots: list[tuple[str, str]],                # [(norm_root, app_name)]  sorted by len desc
        regexes: list[tuple[re.Pattern, str]],       # [(compiled, app_name)]
        keywords: list[tuple[str, str]],             # [(lower_kw, app_name)]
    ):
        self.roots = roots
        self.regexes = regexes
        self.keywords = keywords


# ═══════════════════════════════════════════════════════════════════════════════
# AppFileIndexStore
# ═══════════════════════════════════════════════════════════════════════════════

class AppFileIndexStore:
    """SQLite-backed index mapping file paths to installed applications.

    Each call to ``build_index()`` performs a full rebuild: it reads the
    attribution rules, queries Everything for file paths, matches them,
    and re-populates the database table from scratch.
    """

    TABLE_NAME = "app_files"
    DB_FILENAME = "app_file_index.sqlite3"

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path(__file__).resolve().parent / self.DB_FILENAME
        self.db_path = Path(db_path)
        self.ensure_schema()

    # ── schema ──────────────────────────────────────────────────────────────

    def ensure_schema(self) -> None:
        with self.connect() as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    suffix TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_app_name
                ON {self.TABLE_NAME} (app_name)
                """
            )
            connection.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_file_path
                ON {self.TABLE_NAME} (file_path)
                """
            )

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.db_path))

    # ── rule loading & compilation ──────────────────────────────────────────

    def load_rules(self, rules_path: str | Path | None = None) -> list[dict]:
        """Read the attribution-rules JSON file.

        Returns the ``"rules"`` list, or an empty list when the file is
        missing, empty, or unparseable (a warning is printed but no
        exception is raised).
        """
        path = Path(rules_path) if rules_path else _DEFAULT_RULES_PATH
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            warnings.warn(f"Rules file not found: {path} — no rules loaded.")
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            warnings.warn(f"Rules file {path} is invalid JSON: {exc}")
            return []

        if not isinstance(data, dict):
            warnings.warn(f"Rules file {path} is not a JSON object — ignored.")
            return []

        rules = data.get("rules")
        if not isinstance(rules, list):
            warnings.warn(f"Rules file {path} has no 'rules' array — ignored.")
            return []

        return rules

    def compile_rules(self, rules: list[dict]) -> CompiledRules:
        """Pre-process *rules* into fast matching structures.

        Bad regexes are logged and skipped.  roots are sorted by
        normalised path length descending so the deepest match wins.
        """
        roots: list[tuple[str, str]] = []        # (norm_root, app_name)
        regexes: list[tuple[re.Pattern, str]] = []  # (compiled, app_name)
        keywords: list[tuple[str, str]] = []      # (lower_kw, app_name)

        for rule in rules:
            app_name = (rule.get("app_name") or "").strip()
            if not app_name:
                continue
            matches = rule.get("match")
            if not isinstance(matches, list):
                continue

            for m in matches:
                if not isinstance(m, dict):
                    continue
                mtype = (m.get("type") or "").strip().lower()
                value = (m.get("value") or "").strip()
                if not value:
                    continue

                if mtype == "root":
                    norm = EverythingFilesExtractor._fast_normalized_path(value)
                    roots.append((norm, app_name))
                elif mtype == "keyword":
                    keywords.append((value.lower(), app_name))
                elif mtype == "re":
                    try:
                        compiled = re.compile(value, re.IGNORECASE)
                    except re.error as exc:
                        warnings.warn(
                            f"Bad regex in rule '{app_name}': {value!r} — {exc}"
                        )
                        continue
                    regexes.append((compiled, app_name))

        # Sort root entries by normalised path length descending.
        roots.sort(key=lambda item: len(item[0]), reverse=True)

        return CompiledRules(roots=roots, regexes=regexes, keywords=keywords)

    def match_file_to_app(
        self, file_path: str, compiled: CompiledRules,
    ) -> str | None:
        """Return the best-matching ``app_name`` for *file_path*, or ``None``.

        Priority order (first-match-per-category)::

            1. root    — deepest (longest normalised path) wins
            2. re      — first matching rule in file order
            3. keyword — first matching rule in file order
        """
        norm_path = EverythingFilesExtractor._fast_normalized_path(file_path)
        searchable = file_path.lower()

        # 1. Root prefix — collect ALL matches, pick the deepest.
        best_root_len = -1
        best_root_app: str | None = None
        for norm_root, app_name in compiled.roots:
            if len(norm_root) <= best_root_len:
                # roots are already sorted descending; can't find a deeper one.
                break
            if EverythingFilesExtractor._is_normalized_path_child(norm_path, norm_root):
                best_root_len = len(norm_root)
                best_root_app = app_name
        if best_root_app is not None:
            return best_root_app

        # 2. Regex — first match in file order.
        for pattern, app_name in compiled.regexes:
            if pattern.search(file_path):
                return app_name

        # 3. Keyword — first match in file order.
        for lower_kw, app_name in compiled.keywords:
            if lower_kw in searchable:
                return app_name

        return None

    # ── build ───────────────────────────────────────────────────────────────

    def build_index(
        self,
        extractor: EverythingFilesExtractor | None = None,
        filter: FileFilter | None = None,
    ) -> dict:
        """Full rebuild: load rules, scan files, match, and write to DB.

        Returns a summary dict with keys ``scanned``, ``matched``,
        ``apps_with_files``.
        """
        # 1.  Load & compile attribution rules.
        rules = self.load_rules()
        compiled = self.compile_rules(rules)

        # 2.  Extract file paths from Everything.
        if extractor is None:
            extractor = EverythingFilesExtractor()
        result = extractor.extract(filter)
        paths: list[str] = result.paths

        # 3.  Match each path to its owning app (or drop).
        indexed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows: list[tuple[str, str, str, str, str]] = []

        for path_text in paths:
            app_name = self.match_file_to_app(path_text, compiled)
            if app_name is None:
                continue

            file_name = os.path.basename(path_text)
            suffix = os.path.splitext(path_text)[1].lower()
            rows.append((app_name, path_text, file_name, suffix, indexed_at))

        # 4.  Replace table contents.
        with self.connect() as connection:
            connection.execute(f"DELETE FROM {self.TABLE_NAME}")
            if rows:
                connection.executemany(
                    f"""
                    INSERT OR IGNORE INTO {self.TABLE_NAME}
                        (app_name, file_path, file_name, suffix, indexed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    rows,
                )

        # 5.  Compute stats.
        apps_with_files_count = 0
        if rows:
            with self.connect() as connection:
                apps_with_files_count = connection.execute(
                    f"SELECT COUNT(DISTINCT app_name) FROM {self.TABLE_NAME}"
                ).fetchone()[0]

        return {
            "scanned": len(paths),
            "matched": len(rows),
            "apps_with_files": apps_with_files_count,
        }

    # ── queries ─────────────────────────────────────────────────────────────

    def apps_with_files(self) -> list[dict]:
        """Return every app that has indexed files, with counts, most first."""
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                f"""
                SELECT app_name, COUNT(*) AS file_count
                FROM {self.TABLE_NAME}
                GROUP BY app_name
                ORDER BY file_count DESC
                """
            )
            return [dict(row) for row in cursor]

    def files_for_app(self, app_name: str) -> list[dict]:
        """Return all indexed files for *app_name*, sorted by file name."""
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                f"""
                SELECT file_path, file_name, suffix
                FROM {self.TABLE_NAME}
                WHERE app_name = ?
                ORDER BY file_name
                """,
                (app_name,),
            )
            return [dict(row) for row in cursor]


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    store = AppFileIndexStore()

    # Show loaded rules.
    rules = store.load_rules()
    compiled = store.compile_rules(rules)
    print(f"Rules loaded: {len(rules)} rules  →  "
          f"{len(compiled.roots)} root, "
          f"{len(compiled.regexes)} re, "
          f"{len(compiled.keywords)} keyword")
    if rules:
        for r in rules[:3]:
            name = r.get("app_name", "?")
            ms = r.get("match", [])
            print(f"  {name}: {len(ms)} match entries")
        if len(rules) > 3:
            print(f"  … and {len(rules) - 3} more")

    print("\nBuilding app file index …")
    stats = store.build_index()
    print(f"  scanned        : {stats['scanned']}")
    print(f"  matched        : {stats['matched']}")
    print(f"  apps_with_files: {stats['apps_with_files']}")

    top = store.apps_with_files()[:5]
    if top:
        print("\nTop 5 apps by file count:")
        for entry in top:
            print(f"  {entry['app_name']}  —  {entry['file_count']} files")
    else:
        print("\nNo apps with files found (no rules, or Everything may not be running).")
