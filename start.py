"""
AIFM launcher — start the FastAPI backend and open the browser.

Usage
-----
  python start.py                 Production mode — serves the pre-built React app
                                  at http://127.0.0.1:8000/
  python start.py --app           Desktop window mode — opens a native PySide6
                                  window instead of the system browser
  python start.py --dev           Dev mode — expects `npm run dev` to be running
                                  separately; opens http://127.0.0.1:5173/
  python start.py --reload        Enable uvicorn hot-reload (useful during backend work)
  python start.py --no-browser    Start server without opening the browser

First-time setup
----------------
  pip install -r requirements.txt
  cd "AI Chat and Browser App"
  npm install
  npm run build          # only needed for production mode
  cd ..
  python start.py
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
REACT_DIR  = ROOT / "AI Chat and Browser App"
REACT_DIST = REACT_DIR / "dist"

API_HOST = "127.0.0.1"
API_PORT  = 8000
API_URL   = f"http://{API_HOST}:{API_PORT}"
DEV_URL   = "http://127.0.0.1:5173"

PYTHON = sys.executable


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_react() -> None:
    """Run `npm run build` inside the React project directory."""
    print("Building React app (this takes ~5 s)…")
    result = subprocess.run(
        "npm run build",
        cwd=str(REACT_DIR),
        shell=True,
    )
    if result.returncode != 0:
        print(
            "\nERROR: React build failed.\n"
            "Make sure Node.js is installed, then run:\n"
            f'  cd "{REACT_DIR}"\n'
            "  npm install\n"
            "  npm run build\n"
        )
        sys.exit(1)
    print("React build complete.")


def start_uvicorn(reload: bool) -> subprocess.Popen:
    cmd = [
        PYTHON, "-m", "uvicorn",
        "api.server:app",
        "--host", API_HOST,
        "--port", str(API_PORT),
    ]
    if reload:
        cmd.append("--reload")
    return subprocess.Popen(cmd, cwd=str(ROOT))


def wait_for_server(timeout: int = 20) -> bool:
    """Poll /api/health until the server responds or timeout is reached."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{API_URL}/api/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def open_app_window(url: str) -> None:
    """Open *url* in a native PySide6 + QWebEngineView desktop window.

    The import is local so that users who only ever run in browser mode
    never need to install PySide6.  When PySide6 is missing the function
    prints a friendly message and returns immediately.
    """
    try:
        from PySide6.QtCore import QUrl                 # type: ignore[import-untyped]
        from PySide6.QtWebEngineWidgets import QWebEngineView    # type: ignore[import-untyped]
        from PySide6.QtWidgets import QApplication       # type: ignore[import-untyped]
    except ImportError:
        print(
            "PySide6 is not installed.  Install it to use --app mode:\n"
            "  pip install PySide6\n"
            "Falling back to browser mode."
        )
        import webbrowser
        webbrowser.open(url)
        return

    app = QApplication(["AIFM"])
    view = QWebEngineView()
    view.setWindowTitle("AIFM — AI File Manager")
    view.resize(1280, 800)
    view.load(QUrl(url))
    view.show()
    print("Desktop window opened.  Close the window to exit.")
    app.exec()  # blocks until the user closes the window


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global API_PORT, API_URL   # declared first so reads below are unambiguous

    parser = argparse.ArgumentParser(
        description="AIFM launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Dev mode: open the Vite dev server at :5173 instead of the built app",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Pass --reload to uvicorn (auto-restart on Python file changes)",
    )
    parser.add_argument(
        "--app",
        action="store_true",
        help="Open the UI in a native desktop window instead of the browser",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the server without opening a browser window",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=API_PORT,
        help=f"Backend port (default: {API_PORT})",
    )
    args = parser.parse_args()

    API_PORT = args.port
    API_URL  = f"http://{API_HOST}:{API_PORT}"

    # In production mode, build React if dist/ is missing.
    if not args.dev and not REACT_DIST.exists():
        print(f"No dist/ found at {REACT_DIST}")
        build_react()

    print(f"Starting AIFM backend on {API_URL} …")
    proc = start_uvicorn(reload=args.dev or args.reload)

    # Wait up to 20 s for the server to accept connections.
    target_url = DEV_URL if args.dev else API_URL
    server_ready = wait_for_server(timeout=20)

    if server_ready:
        print(f"Server ready → {target_url}")
    else:
        print("WARNING: server did not respond within 20 s; opening UI anyway.")

    if not args.no_browser:
        if args.app:
            # Desktop window mode — open_app_window blocks until the user closes
            # the window, then we shut down uvicorn cleanly.
            open_app_window(target_url)
            print("Window closed.  Shutting down…")
            proc.terminate()
            proc.wait()
            return
        else:
            webbrowser.open(target_url)

    if args.dev:
        print(
            "\nDev mode: backend is running. Start the frontend separately with:\n"
            f'  cd "{REACT_DIR}"\n'
            "  npm run dev\n"
        )

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down…")
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
