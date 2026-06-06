"""Scan installed Windows applications from registry and collect metadata."""

from __future__ import annotations

import json
import os
import re
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Registry paths to scan
UNINSTALL_PATHS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

SKIP_PREFIXES = (
    "Microsoft ", "Windows ", "Update for ", "Security Update",
    "Hotfix for ", "Service Pack ", "MSXML", "Microsoft_",
)
SKIP_NAMES = frozenset({"", "(no name)", "Default"})


@dataclass
class InstalledApp:
    name: str
    publisher: str = ""
    version: str = ""
    install_location: str = ""
    uninstall_string: str = ""
    icon_path: str = ""
    icon_index: int = 0
    registry_hive: str = ""
    registry_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "publisher": self.publisher,
            "version": self.version,
            "install_location": self.install_location,
            "uninstall_string": self.uninstall_string,
            "icon_path": self.icon_path,
            "icon_index": self.icon_index,
        }

    @property
    def exists(self) -> bool:
        if not self.install_location:
            return False
        return os.path.isdir(self.install_location)


def _parse_icon(value: str) -> tuple[str, int]:
    """Parse a DisplayIcon registry value like 'C:\\app\\app.exe,0'."""
    if not value:
        return "", 0
    match = re.match(r'^(.+?),(-?\d+)$', value.strip())
    if match:
        return match.group(1).strip('"'), int(match.group(2))
    return value.strip('"'), 0


def _read_key(reg_key: winreg.HKEYType, sub_key: str) -> dict[str, Any] | None:
    try:
        with winreg.OpenKey(reg_key, sub_key) as key:
            values: dict[str, Any] = {}
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                    values[name] = data
                    i += 1
                except OSError:
                    break
            return values
    except OSError:
        return None


def get_installed_apps() -> list[InstalledApp]:
    """Scan all uninstall registry locations and return deduplicated app list."""
    seen: dict[str, InstalledApp] = {}

    for hive, path in UNINSTALL_PATHS:
        try:
            with winreg.OpenKey(hive, path) as parent:
                i = 0
                while True:
                    try:
                        sub_key_name = winreg.EnumKey(parent, i)
                        i += 1
                    except OSError:
                        break

                    values = _read_key(parent, sub_key_name)
                    if values is None:
                        continue

                    name = str(values.get("DisplayName", "")).strip()
                    if not name or name in SKIP_NAMES:
                        continue

                    icon_path, icon_index = _parse_icon(
                        str(values.get("DisplayIcon", ""))
                    )

                    app = InstalledApp(
                        name=name,
                        publisher=str(values.get("Publisher", "")).strip(),
                        version=str(values.get("DisplayVersion", "")).strip(),
                        install_location=str(values.get("InstallLocation", "")).strip(),
                        uninstall_string=str(values.get("UninstallString", "")).strip(),
                        icon_path=icon_path,
                        icon_index=icon_index,
                        registry_hive={winreg.HKEY_LOCAL_MACHINE: "HKLM", winreg.HKEY_CURRENT_USER: "HKCU"}.get(hive, ""),
                        registry_path=f"{path}\\{sub_key_name}",
                    )

                    key = name.lower()
                    if key in seen:
                        existing = seen[key]
                        if app.version and not existing.version:
                            seen[key] = app
                        elif app.install_location and not existing.install_location:
                            seen[key] = app
                    else:
                        seen[key] = app

        except OSError:
            continue

    apps = sorted(seen.values(), key=lambda a: a.name.lower())
    return [a for a in apps if not a.name.startswith(SKIP_PREFIXES)]


def _find_in_dir(directory: Path, pattern: str, max_depth: int = 2) -> str | None:
    """Search for a file matching pattern up to max_depth levels deep."""
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.lower().endswith((".exe", ".ico")):
                return str(entry)
        if max_depth > 1:
            for entry in directory.iterdir():
                if entry.is_dir():
                    result = _find_in_dir(entry, pattern, max_depth - 1)
                    if result:
                        return result
    except (OSError, PermissionError):
        pass
    return None


def _lookup_app_paths(app_name: str) -> str | None:
    """Look up an app's executable via HKLM App Paths registry."""
    base_name = Path(app_name).stem
    candidates = [app_name, base_name, base_name.lower()]
    for name in candidates:
        for exe_name in (name, f"{name}.exe"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{exe_name}",
                ) as key:
                    path = str(winreg.QueryValue(key, ""))
                    if os.path.isfile(path):
                        return path
            except OSError:
                pass
    return None


def _search_common_dirs(app_name: str) -> str | None:
    """Try to find an app's exe in common install directories by name."""
    name = re.sub(r"\s*\(.*?\)|\s*\d+\.\d+.*$", "", app_name).strip()
    if not name:
        return None

    for base in (
        os.environ.get("ProgramFiles", "C:\\Program Files"),
        os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ):
        if not base:
            continue
        for candidate in Path(base).glob(f"{name}*"):
            if candidate.is_dir():
                result = _find_in_dir(candidate, "*.exe")
                if result:
                    return result

    return None


def resolve_icon_path(app: InstalledApp) -> str | None:
    """Resolve the best icon source for an installed application."""
    if app.icon_path and os.path.isfile(app.icon_path):
        return app.icon_path

    if app.install_location:
        result = _find_in_dir(Path(app.install_location), "*.exe")
        if result:
            return result

    if app.uninstall_string:
        match = re.search(r'"([^"]+\.exe)"', app.uninstall_string)
        if match and os.path.isfile(match.group(1)):
            uninstall_path = Path(match.group(1)).parent
            if uninstall_path != Path(app.install_location or ""):
                result = _find_in_dir(uninstall_path, "*.exe")
                if result:
                    return result
            return match.group(1)

    result = _lookup_app_paths(app.name)
    if result:
        return result

    result = _search_common_dirs(app.name)
    if result:
        return result

    return None


def is_system_component(app: InstalledApp) -> bool:
    """Return True if the app looks like a system component, not a user-facing app."""
    name_lower = app.name.lower()
    publisher_lower = app.publisher.lower()

    system_keywords = (
        "sdk", "runtime", "redistributable", "component",
        "extension", "extensibility", "localization", "core",
        "library", "libraries", "headers", "tools", "debug",
        "intellisense", "configuration", "script", "additions",
        "prerequisites", "bootstrap", "clickonce", "protocol",
        "filehandler", "filetracker", "singleton", "font",
        "documentation", "test suite", "pip ",
        "driver", "plugin", "helper", "support",
        "package", "installer", "setup", "launcher",
        "manager", "updater", "update", "service",
        "resource", "resources", "module", "framework",
        "dependency", "bundle", "shared", "common",
        "wrapper", "bridge", "connector", "adapter",
        "plug-in", "add-in", "add-on", "addon",
        "bonjour", "hevc", "codec", "filter",
    )

    for kw in system_keywords:
        if kw in name_lower:
            return True

    system_publishers = (
        "microsoft corporation", "microsoft", "apple inc.",
        "nvidia corporation", "intel corporation", "amd",
        "dell inc.", "alienware", "realtek", "intel",
    )
    if any(p in publisher_lower for p in system_publishers):
        if "smart installer" in name_lower:
            return True
        if "package manager" in name_lower:
            return True
        if "refresh manager" in name_lower:
            return True
        if name_lower.startswith("alienware") and "command center" not in name_lower:
            return True

    if app.uninstall_string.startswith("MsiExec.exe") and not app.install_location:
        return True

    if re.match(r"^\{[0-9a-fA-F]{8}-", app.name):
        return True

    if "${{" in app.name or "{{" in app.name:
        return True

    return False


def dump_json(file_path: str | Path = "installed_apps.json") -> list[dict]:
    """Scan and save installed apps to a JSON file."""
    apps = get_installed_apps()
    data = [app.to_dict() for app in apps]
    path = Path(file_path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


if __name__ == "__main__":
    apps = get_installed_apps()
    print(f"Found {len(apps)} installed applications:\n")
    for app in apps:
        icon = resolve_icon_path(app) or "(no icon found)"
        print(f"  {app.name}")
        print(f"    Publisher: {app.publisher}")
        print(f"    Version:   {app.version}")
        print(f"    Location:  {app.install_location}")
        print(f"    Icon:      {icon}")
        print()
