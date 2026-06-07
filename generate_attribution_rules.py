"""LLM-driven incremental rule generation for app file attribution.

Scans the local filesystem for candidate directories, sends them together
with the installed-application list to an LLM, and merges the LLMʼs
attribution suggestions into ``app_attribution_rules.json``.

**LLM is only invoked when the user runs this script manually** — never
during normal FastAPI or build_index operation.

Usage::

    python generate_attribution_rules.py          # incremental merge
    python generate_attribution_rules.py --dry-run  # preview only, no write
    python generate_attribution_rules.py --replace-llm  # replace all llm rules
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ── project imports (works when run from the project root) ────────────────────

_RULES_PATH = Path(__file__).resolve().parent / "app_attribution_rules.json"

MAX_DIRS = 200                   # cap on candidate directories to keep prompts manageable
LLM_TIMEOUT = 300                # seconds — large prompt needs longer processing


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Candidate directory scanning
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_shallow(path: Path, depth: int = 2) -> list[str]:
    """Return directory paths under *path* up to *depth* levels.

    Skips hidden / system directories (names starting with ``.`` or ``$``)
    and the Windows directory.
    """
    result: list[str] = []
    if depth <= 0:
        return result
    try:
        for child in sorted(path.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(".") or name.startswith("$"):
                continue
            lower = name.lower()
            if lower in {"windows", "winnt", "system volume information",
                         "recovery", "$recycle.bin", "config.msi"}:
                continue
            result.append(str(child))
            if depth > 1:
                result.extend(_scan_shallow(child, depth - 1))
    except (OSError, PermissionError):
        pass
    return result


def collect_candidate_dirs() -> list[str]:
    """Return up to *MAX_DIRS* candidate directory paths.

    Scans:
    * ``Documents``, ``Desktop``, ``Downloads`` (1 level deep)
    * Each drive root (2 levels deep)
    """
    seen: set[str] = set()
    result: list[str] = []

    def _add(p: str) -> None:
        if p not in seen:
            seen.add(p)
            result.append(p)

    # Per-user known folders.
    home = Path.home()
    for folder in ("Documents", "Desktop", "Downloads"):
        candidate = home / folder
        if candidate.is_dir():
            _add(str(candidate))
            for d in _scan_shallow(candidate, depth=1):
                _add(d)

    # Drive roots.
    for letter in "CDEFGHAB":
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        _add(str(root))
        for d in _scan_shallow(root, depth=2):
            _add(d)

    return result[:MAX_DIRS]


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  LLM prompt construction
# ═══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
You are a software-installation classifier.  Given two lists:

1) INSTALLED APPLICATIONS — names of software known to be installed on this
   Windows machine.  Each entry looks like:
     name | install_location | publisher

2) CANDIDATE DIRECTORIES — real directory paths found on this machineʼs
   filesystem.

Your task: determine which installed application owns each candidate directory
(if any), and produce **only** a JSON array of attribution rules.

Each rule in the array must have this shape:

{
  "app_name": "<exact name from the installed-apps list>",
  "match": [
    {"type": "root",    "value": "<directory path>"},
    {"type": "keyword", "value": "<lowercase keyword>"},
    {"type": "re",      "value": "<regex pattern>"}
  ],
  "note": "brief explanation (optional)"
}

**match** can contain any mix of the three types.  Guidelines:

* ``root`` — a directory that clearly belongs to this app.  The system does
  prefix matching: any file whose full path starts with this directory is
  attributed to the app.  Use this for an appʼs install folder (e.g. a
  directory named after the app or vendor under Program Files).

* ``keyword`` — a substring that, when it appears ANYWHERE in the fileʼs
  full path, signals this app.  Lowercase only.  Example: "autodesk" for
  Autodesk apps, "blender" for Blender.  Be conservative — avoid generic
  words (like "data", "config", "bin") that would match unrelated files.

* ``re`` — a regular expression for cases that keywords cannot express.
  Pattern is matched against the full path (case-insensitive).  Only use
  this when root/keyword are insufficient.  Be conservative to avoid
  overly broad matches.  Example: "node_modules.*package\\\\.json$"

Important constraints:
* ``app_name`` MUST be an exact string from the installed-applications list.
  Do not invent new names or modify existing ones.
* If you are unsure whether a directory belongs to an app, DO NOT include it.
  It is better to miss a few matches than to create false attributions.
* Regex patterns should be conservative — avoid patterns like ".*" or "."
  that would match everything.
* Use root matching whenever possible (it is the fastest and most precise).
* Output ONLY the JSON array — no explanation, no markdown outside the JSON."""


def _build_prompt(apps: list[dict], dirs: list[str]) -> str:
    """Build the LLM prompt from app and directory lists."""
    # App list — include name, location, publisher for context.
    app_lines: list[str] = []
    for a in apps:
        loc = (a.get("install_location") or "").strip()
        pub = (a.get("publisher") or "").strip()
        app_lines.append(f"  {a['name']}  |  {loc}  |  {pub}")

    # Directory list.
    dir_lines = [f"  {d}" for d in dirs]

    return (
        "INSTALLED APPLICATIONS (name | install_location | publisher):\n"
        + "\n".join(app_lines)
        + "\n\nCANDIDATE DIRECTORIES:\n"
        + "\n".join(dir_lines)
        + "\n\nOutput a JSON array of attribution rules based on the above."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  LLM response parsing
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_llm_json(text: str) -> list[dict] | None:
    """Extract and parse a JSON array from LLM output.

    Handles ```json fences and leading/trailing non-JSON text.
    Returns the parsed list on success, or *None* on failure (after
    printing the raw text to stderr).
    """
    text = text.strip()

    # Strip ```json … ``` fences.
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()

    # Find the outermost JSON array.
    start = text.find("[")
    if start == -1:
        print("ERROR: LLM output contains no JSON array.", file=sys.stderr)
        print("--- raw output ---", file=sys.stderr)
        print(text, file=sys.stderr)
        print("--- end ---", file=sys.stderr)
        return None

    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        print("ERROR: Unbalanced JSON array in LLM output.", file=sys.stderr)
        print("--- raw output ---", file=sys.stderr)
        print(text, file=sys.stderr)
        print("--- end ---", file=sys.stderr)
        return None

    json_text = text[start : end + 1]

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse LLM JSON: {exc}", file=sys.stderr)
        print("--- raw output ---", file=sys.stderr)
        print(text, file=sys.stderr)
        print("--- end ---", file=sys.stderr)
        return None

    if not isinstance(parsed, list):
        print("ERROR: LLM JSON is not an array.", file=sys.stderr)
        return None

    # Validate each entry has app_name and match.
    valid: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not item.get("app_name"):
            continue
        if not isinstance(item.get("match"), list):
            continue
        valid.append(item)
    return valid


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  Incremental merge
# ═══════════════════════════════════════════════════════════════════════════════

def _load_existing_rules() -> tuple[int, list[dict]]:
    """Return (version, rules_list) from the on-disk JSON file.

    If the file is missing or corrupted, returns (1, []).
    """
    try:
        raw = _RULES_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            return (int(data.get("version", 1)), data["rules"])
    except Exception:
        pass
    return (1, [])


def _rule_key(rule: dict) -> str:
    """Normalised key for a match entry: type + lowercased value."""
    t = (rule.get("type") or "").strip().lower()
    v = (rule.get("value") or "").strip().lower()
    return f"{t}:{v}"


def _merge_rules(
    existing: list[dict],
    llm_rules: list[dict],
    replace_llm: bool,
) -> list[dict]:
    """Merge *llm_rules* into *existing* and return the new rules list.

    * Manual rules (origin == "manual") are always preserved as-is.
    * When *replace_llm* is True, all old LLM rules are dropped and only
      the new LLM output is used.
    * Otherwise each new LLM app either merges its match entries with an
      existing LLM rule for the same app, or is appended as a new rule.
      Old LLM rules for apps the LLM did not mention are kept.
    """
    # Separate manual and existing-llm rules.
    manual: list[dict] = []
    existing_llm: dict[str, dict] = {}  # app_name → rule

    for r in existing:
        origin = (r.get("origin") or "").strip().lower()
        if origin == "manual":
            manual.append(r)
        else:
            name = (r.get("app_name") or "").strip()
            if name:
                existing_llm[name] = r

    # Build new LLM rules.
    if replace_llm:
        # Start fresh for LLM rules; keep only manual.
        new_llm: dict[str, dict] = {}
    else:
        # Start from existing LLM rules (weʼll merge into them).
        new_llm = dict(existing_llm)

    new_apps = 0
    merged_apps = 0
    added_matches = 0

    for llm_rule in llm_rules:
        name = (llm_rule.get("app_name") or "").strip()
        if not name:
            continue

        llm_matches = llm_rule.get("match")
        if not isinstance(llm_matches, list):
            continue

        # Assign origin and note.
        llm_rule["origin"] = "llm"
        note = (llm_rule.get("note") or "").strip()
        llm_rule["note"] = note

        if name in new_llm:
            # Merge match entries (deduplicate by type+value).
            existing_matches = new_llm[name].get("match")
            if not isinstance(existing_matches, list):
                existing_matches = []
            existing_keys = {_rule_key(m) for m in existing_matches}

            for m in llm_matches:
                if _rule_key(m) not in existing_keys:
                    existing_matches.append(m)
                    existing_keys.add(_rule_key(m))
                    added_matches += 1

            new_llm[name]["match"] = existing_matches
            # Update note if the old one was empty and the new one has content.
            old_note = (new_llm[name].get("note") or "").strip()
            if note and not old_note:
                new_llm[name]["note"] = note
            merged_apps += 1
        else:
            # New app.
            new_llm[name] = {
                "app_name": name,
                "match": list(llm_matches),
                "origin": "llm",
                "note": note,
            }
            new_apps += 1
            added_matches += len(llm_matches)

    # Reconstruct the rules list: manual first, then LLM.
    result = list(manual)
    result.extend(sorted(new_llm.values(), key=lambda r: r["app_name"].lower()))

    print(f"  New apps      : {new_apps}")
    print(f"  Merged apps   : {merged_apps}")
    print(f"  New matches   : {added_matches}")

    return result


def _write_rules(version: int, rules: list[dict]) -> None:
    """Write the rules file atomically."""
    data = {"version": version, "rules": rules}
    tmp = _RULES_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(_RULES_PATH)


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run(dry_run: bool = False, replace_llm: bool = False,
        timeout: int = LLM_TIMEOUT) -> None:
    """Full pipeline: scan → prompt → LLM → parse → merge → write."""
    global LLM_TIMEOUT
    LLM_TIMEOUT = timeout

    # ── 5a.  Collect inputs ──────────────────────────────────────────────────
    print("Collecting installed applications …")
    from installed_apps import get_installed_apps

    raw_apps = get_installed_apps()
    apps = [a.to_dict() for a in raw_apps]
    print(f"  {len(apps)} installed apps")

    print("Collecting candidate directories …")
    dirs = collect_candidate_dirs()
    print(f"  {len(dirs)} candidate directories")

    # ── 5b.  Prompt LLM ──────────────────────────────────────────────────────
    prompt = _build_prompt(apps, dirs)
    print(f"  Prompt: {len(apps)} apps, {len(dirs)} dirs, "
          f"{len(prompt)} chars")

    print(f"Calling LLM (timeout={LLM_TIMEOUT}s) …")
    from models import create_model_client, system_message, user_message

    client = create_model_client(timeout_seconds=LLM_TIMEOUT)
    messages = [system_message(_SYSTEM_PROMPT), user_message(prompt)]
    response = client.chat(messages=messages)
    raw = response.content
    print(f"  Response: {len(raw)} chars")

    # ── 5c.  Parse response ──────────────────────────────────────────────────
    llm_rules = _parse_llm_json(raw)
    if llm_rules is None:
        sys.exit(1)

    print(f"  Parsed: {len(llm_rules)} rules")
    for r in llm_rules[:5]:
        name = r.get("app_name", "?")
        matches = len(r.get("match", []))
        print(f"    {name}: {matches} match entries")
    if len(llm_rules) > 5:
        print(f"    … and {len(llm_rules) - 5} more")

    if dry_run:
        print("\n--dry-run: not writing to disk.")
        return

    # ── 5d.  Merge & write ───────────────────────────────────────────────────
    version, existing = _load_existing_rules()
    print(f"\nExisting rules: {len(existing)} entries  (version {version})")

    merged = _merge_rules(existing, llm_rules, replace_llm)

    _write_rules(version, merged)
    print(f"Wrote {len(merged)} rules to {_RULES_PATH}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate app-attribution rules via LLM",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print LLM results without writing the rules file.",
    )
    parser.add_argument(
        "--replace-llm",
        action="store_true",
        help="Replace ALL existing llm-origin rules (manual rules are kept).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=LLM_TIMEOUT,
        help=f"LLM API timeout in seconds (default: {LLM_TIMEOUT})",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, replace_llm=args.replace_llm,
        timeout=args.timeout)
