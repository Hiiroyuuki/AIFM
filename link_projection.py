"""
Symbolic-link aggregation tree for AIFM.

Builds a flat directory tree where every installed application gets a
subdirectory containing links to its indexed files:

    <projection_root>/
        <safe_app_name>/
            <file_name>         ← link → real file on disk

Any software (Explorer, Word, image viewers) can browse these folders and
double-click files.  The provider tries three link strategies in order:

  symlink   –  os.symlink(); requires Administrator or Developer Mode on Windows
  hardlink  –  os.link();  only works for files on the same volume
  .lnk      –  Windows shell shortcut (IShellLinkW via raw ctypes COM)

Usage:
    python link_projection.py [projection_root]

    Default projection_root is ./aifm_projection.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app_file_index import AppFileIndexStore


# ═══════════════════════════════════════════════════════════════════════════════
#  Windows .lnk shortcut  (PowerShell via subprocess — zero third-party deps)
# ═══════════════════════════════════════════════════════════════════════════════

def _create_lnk_batch(pairs: list[tuple[str, str]]) -> tuple[int, int]:
    """Create many .lnk shortcuts in batches via temporary PowerShell scripts.

    *pairs* is a list of ``(src_path, dst_lnk_path)`` tuples.  Returns
    ``(created, failed)`` counts.

    Each batch writes a ``.ps1`` file and runs it via ``powershell -File …``,
    avoiding both command-line length limits and encoding issues.
    """
    if not pairs or os.name != "nt":
        return (0, len(pairs))

    prologue = (
        "$ProgressPreference = 'SilentlyContinue'; "
        "$ws = New-Object -ComObject WScript.Shell; "
    )

    # Each chunk becomes one temp .ps1 file.
    CHUNK = 200
    created, failed = 0, 0

    for i in range(0, len(pairs), CHUNK):
        chunk = pairs[i : i + CHUNK]

        lines: list[str] = []
        for src, dst in chunk:
            _src = src.replace("'", "''")
            _dst = dst.replace("'", "''")
            _dir = os.path.dirname(src).replace("'", "''")
            lines.append(
                f"try{{$sc=$ws.CreateShortcut('{_dst}');"
                f"$sc.TargetPath='{_src}';"
                f"$sc.WorkingDirectory='{_dir}';"
                f"$sc.Save()}}catch{{}}"
            )
        script = prologue + "; ".join(lines)

        # Write to a temp file and execute it.
        # Write script to a temp file, run it, then clean up.
        tmp_path = ""
        try:
            # utf-8-sig adds a BOM so PowerShell reads the file as UTF-8
            # rather than the system ANSI code page.  This is essential for
            # paths that contain non-ASCII characters (e.g. Chinese, emoji).
            tf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".ps1", prefix="aifm_lnk_",
                delete=False, encoding="utf-8-sig",
            )
            tmp_path = tf.name
            tf.write(script)
            tf.close()

            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-File", tmp_path],
                    capture_output=True,
                    timeout=120,
                )
            except Exception:
                pass
        except OSError:
            failed += len(chunk)
            continue
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Verify each expected output file exists.
        for _, dst in chunk:
            if os.path.exists(dst):
                created += 1
            else:
                failed += 1

    return (created, failed)


# ═══════════════════════════════════════════════════════════════════════════════
#  LinkProjectionBuilder
# ═══════════════════════════════════════════════════════════════════════════════

class LinkProjectionBuilder:
    """Build and maintain a link-tree projection of the app→file index."""

    def __init__(
        self,
        root: str | Path = "./aifm_projection",
        index_store: AppFileIndexStore | None = None,
    ):
        self._root = Path(root).resolve()
        self._store = index_store or AppFileIndexStore()

    # -- public API ------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def rebuild(self) -> dict:
        """Delete the old projection tree and rebuild it from scratch.

        Returns a stats dict with keys ``root``, ``apps``, ``links``,
        ``by_method``, ``skipped``.
        """
        # 1.  Safely clear the old tree.
        self.clear()

        # 2.  Create the root.
        self._root.mkdir(parents=True, exist_ok=True)

        # 3.  Gather data.
        apps = self._store.apps_with_files()          # [{app_name, file_count}, …]

        stats = {
            "root": str(self._root),
            "apps": 0,
            "links": 0,
            "by_method": {"symlink": 0, "hardlink": 0, "lnk": 0},
            "skipped": 0,
        }
        apps_with_links = 0
        all_lnk: list[tuple[str, str]] = []   # (src, dst) for batch .lnk creation

        for entry in apps:
            app_name = entry["app_name"]
            safe_name = _safe_folder_name(app_name)
            app_dir = self._root / safe_name

            # Skip if we somehow can't create the directory.
            try:
                app_dir.mkdir(parents=False, exist_ok=True)
            except OSError:
                continue

            files = self._store.files_for_app(app_name)   # [{file_path, file_name, suffix}, …]
            used_names: set[str] = set()

            for f in files:
                src = f["file_path"]
                base_name = f["file_name"]
                dst = _unique_child(app_dir, base_name, used_names)
                used_names.add(dst.name.lower())

                method = self._make_link(src, str(dst), deferred_lnk=all_lnk)
                if method == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["links"] += 1
                    stats["by_method"][method] += 1

        # Phase 2 — batch-create every collected .lnk in one/few PowerShell calls.
        if all_lnk:
            created, failed = _create_lnk_batch(all_lnk)
            # Adjust stats: all were pre-counted as "lnk"; remove failures.
            stats["links"] -= failed
            stats["by_method"]["lnk"] = created
            stats["skipped"] += failed

        # Phase 3 — count apps with links and remove empty app directories.
        # MUST run after the batch phase because directories that only contain
        # deferred .lnk files would appear empty during Phase 1.
        for entry in apps:
            safe_name = _safe_folder_name(entry["app_name"])
            app_dir = self._root / safe_name
            if app_dir.is_dir():
                if any(app_dir.iterdir()):
                    apps_with_links += 1
                else:
                    try:
                        app_dir.rmdir()
                    except OSError:
                        pass

        stats["apps"] = apps_with_links
        return stats

    def clear(self) -> None:
        """Remove everything inside *root* without following links.

        Only touches paths that are confirmed to be inside the projection root.
        Symlinks and hardlinks are deleted with ``os.unlink`` / ``Path.unlink``,
        which never follow the link target.  After all links are gone we remove
        the now-empty app subdirectories.
        """
        root = self._root
        if not root.exists():
            return

        resolved_root = root.resolve()

        for app_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if not app_dir.is_dir():
                continue
            if not _is_inside(app_dir, resolved_root):
                continue

            # Remove every link inside this app directory.
            for child in sorted(app_dir.iterdir(), key=lambda p: p.name):
                if not _is_inside(child, resolved_root):
                    continue
                try:
                    # os.unlink / Path.unlink does NOT follow symlinks on
                    # Windows, and on POSIX it only removes the link itself.
                    child.unlink()
                except OSError:
                    pass

            # Directory should be empty now; remove it.
            try:
                app_dir.rmdir()
            except OSError:
                pass

    # -- internal --------------------------------------------------------------

    def _make_link(self, src: str, dst: str,
                    deferred_lnk: list[tuple[str, str]] | None = None) -> str:
        """Try to create a link at *dst* pointing to *src*.

        Symlinks and hardlinks are attempted immediately.  .lnk shortcuts are
        collected into *deferred_lnk* (when provided) so they can be created
        in a single batch later — this avoids spawning one PowerShell process
        per file.

        Returns one of ``"symlink"``, ``"hardlink"``, ``"lnk"``, or ``"skipped"``.
        Never raises.
        """
        # 1.  symlink  (needs admin / Developer Mode on Windows)
        try:
            os.symlink(src, dst)
            return "symlink"
        except OSError:
            pass

        # 2.  hardlink  (same volume only; files only)
        if os.name == "nt":
            try:
                if Path(src).drive.lower() == Path(dst).drive.lower():
                    os.link(src, dst)
                    return "hardlink"
            except OSError:
                pass

        # 3.  .lnk shortcut — deferred to batch  (Windows only)
        if os.name == "nt":
            lnk_dst = dst if dst.lower().endswith(".lnk") else dst + ".lnk"
            if deferred_lnk is not None:
                deferred_lnk.append((src, lnk_dst))
            else:
                # No batch list — try immediately (single file, slower).
                created, _ = _create_lnk_batch([(src, lnk_dst)])
                if created:
                    return "lnk"
            return "lnk"

        return "skipped"


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_folder_name(name: str) -> str:
    """Replace filesystem-illegal characters with underscores."""
    cleaned = "".join(
        "_" if c in '<>:"/\\|?*' else c
        for c in str(name)
    )
    cleaned = cleaned.strip().strip(".")
    return cleaned or "App"


def _unique_child(parent: Path, base_name: str, used: set[str]) -> Path:
    """Return a path *parent / name* where *name* does not collide with *used*.

    If *base_name* already appears in *used* (case-insensitive), appends
    `` (2)``, `` (3)``, … before the extension.

    **used** must contain lowercased names for correct collision detection on
    case-insensitive filesystems like NTFS.
    """
    stem, ext = os.path.splitext(base_name)
    candidate_name = base_name
    counter = 2
    while candidate_name.lower() in used:
        candidate_name = f"{stem} ({counter}){ext}"
        counter += 1
    return parent / candidate_name


def _is_inside(path: Path, root: Path) -> bool:
    """Return ``True`` when *path* is inside *root* (safe-deletion guard)."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        # Accept if equal to root or a child.
        if resolved == root_resolved:
            return True
        resolved.relative_to(root_resolved)
        return True
    except (ValueError, OSError):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    root_arg = sys.argv[1] if len(sys.argv) > 1 else "./aifm_projection"
    builder = LinkProjectionBuilder(root=root_arg)

    print(f"Building link projection at {builder.root} …")
    stats = builder.rebuild()

    print(f"""
  Root    : {stats['root']}
  Apps    : {stats['apps']}
  Links   : {stats['links']}  (symlink: {stats['by_method']['symlink']},
             hardlink: {stats['by_method']['hardlink']},
             lnk: {stats['by_method']['lnk']})
  Skipped : {stats['skipped']}
""")
    print(f"Open '{builder.root}' in Explorer to browse the projected tree.")
