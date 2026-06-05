# AIFM — AI File Manager

A Windows desktop file manager with an embedded LLM agent.  
The UI is a **React 18 / Vite / shadcn** SPA served by a **FastAPI** backend
that exposes the file-system, Everything search, and agent APIs.

---

## Architecture

```
┌─────────────────────────────────────────┐
│  Browser / Webview                      │
│  React 18 + Vite + shadcn/ui + Tailwind │
│  ws://127.0.0.1:8000/ws/agent  (WS)     │
│  http://127.0.0.1:8000/api/*   (REST)   │
└───────────────┬─────────────────────────┘
                │ HTTP + WebSocket
┌───────────────▼─────────────────────────┐
│  FastAPI  (api/server.py)               │
│  api/routers/  fs · search · analysis   │
│                ai_folders · config      │
│                ws_agent (streaming)     │
└──────┬──────────────────────────────────┘
       │ direct function calls
┌──────▼──────────────────────────────────┐
│  Python backend modules                 │
│  mainFunctions.py  FileOperationService │
│                    EverythingSdkSearch  │
│                    FolderAnalysisStore  │
│                    AIFolderStore        │
│  agent.py          FileManagerAgent     │
│  models.py         OpenAI-compat client │
│  config_loader.py  Config / ProviderSpec│
└─────────────────────────────────────────┘
```

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.10 | Tested with Miniconda `python310-base` |
| Node.js | 18 | For building the React frontend |
| Everything | 1.4 | Must be running for search; bundled in `Everything/` |

---

## Quick Start (production)

```powershell
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install and build the React frontend (once)
cd "AI Chat and Browser App"
npm install
npm run build
cd ..

# 3. Run
python start.py
```

`start.py` starts `uvicorn`, waits for it to come up, and opens
`http://127.0.0.1:8000` in your default browser.  
Press **Ctrl+C** to stop.

---

## Development Mode

Run the backend and frontend in separate terminals for hot-reload:

```powershell
# Terminal 1 — backend with auto-reload
python start.py --reload --no-browser

# Terminal 2 — Vite dev server (HMR)
cd "AI Chat and Browser App"
npm run dev
# → http://localhost:5173
```

Or use the convenience flag:
```powershell
python start.py --dev   # opens :5173, expects npm run dev running separately
```

---

## Project Structure

```
AIFM/
├── api/                        FastAPI service layer
│   ├── server.py               App entry point, static-file mount
│   ├── state.py                Shared service singletons + undo/redo stacks
│   └── routers/
│       ├── fs.py               File-system browse + write ops + preview
│       ├── search.py           Everything SDK search
│       ├── analysis.py         Folder size analysis
│       ├── ai_folders.py       AI-managed folder registry
│       ├── config.py           LLM provider config CRUD
│       └── ws_agent.py         Streaming WebSocket agent
│
├── "AI Chat and Browser App"/  React frontend
│   ├── src/
│   │   ├── api/                Typed fetch wrappers (client.ts, types.ts)
│   │   └── app/
│   │       ├── App.tsx         Top-level state + layout controller
│   │       └── components/     FolderTree, FileTable, PreviewPanel,
│   │                           AgentPanel, LLMSettingsDialog, …
│   └── dist/                   Built output (created by npm run build)
│
├── mainFunctions.py            Core file-ops, search, analysis, AI folders
├── agent.py                    FileManagerAgent (tool-calling LLM loop)
├── models.py                   OpenAI-compatible multi-provider client
├── config_loader.py            config.json loader / ProviderSpec
├── config.json                 LLM providers, Everything settings, prefs
│
├── start.py                    One-command launcher
├── requirements.txt            Python dependencies
└── Everything-SDK/             Everything IPC DLL + headers
```

---

## Feature Map

| Feature | Implementation |
|---|---|
| Folder tree (lazy expand) | `FolderTree.tsx` → `GET /api/fs/list` |
| File table (virtual scroll, 4-col sort) | `FileTable.tsx` + `@tanstack/react-virtual` |
| Folder analysis sizes in Size column | `GET /api/fs/list` → `child_folder_size_map()` |
| Everything search | address bar → `GET /api/search` |
| Address bar + back/forward history | `App.tsx` navigation state |
| Copy / Cut / Paste | `App.tsx` clipboard + `POST /api/fs/paste` |
| Delete (undoable, moves to app-trash) | `POST /api/fs/delete` |
| Undo / Redo | `POST /api/fs/undo|redo` (server-side stack) |
| Right-click context menu | `ContextMenu` (Radix UI) in `App.tsx` |
| Keyboard shortcuts (Ctrl+C/X/V/A/Z/Y, Del) | `useEffect` keydown handler in `App.tsx` |
| Analyse folder sizes | `POST /api/analysis/run` |
| New AIFolder + auth mode | `POST /api/ai-folders` via `NewAIFolderDialog` |
| File preview (text + image + metadata) | `PreviewPanel.tsx` → `GET /api/fs/preview` |
| AI agent chat (streaming) | `AgentPanel.tsx` ↔ `WS /ws/agent` |
| LLM settings (provider CRUD) | `LLMSettingsDialog.tsx` → `api/routers/config.py` |

---

## LLM Configuration

Edit `config.json` or use the **Settings** button in the UI.

```jsonc
{
  "provider": "minimax",         // active provider key
  "models": {
    "minimax": {
      "base_url": "https://api.minimaxi.com/v1",
      "api_key": "YOUR_KEY_HERE",
      "default_model": "MiniMax-M2.7-highspeed"
    }
  }
}
```

Any OpenAI-compatible provider works (OpenAI, DeepSeek, Kimi, Moonshot, etc.).

---

## API Reference

Interactive docs at **http://127.0.0.1:8000/docs** while the server is running.

Key endpoints:

```
GET  /api/fs/list?path=        List folder contents
GET  /api/fs/stat?path=        File/folder metadata + analysis
GET  /api/fs/preview?path=     Text content or image/binary type
GET  /api/fs/image?path=       Serve an image file inline
POST /api/fs/paste             Copy or move files
POST /api/fs/delete            Move to app-trash (undoable)
POST /api/fs/undo              Undo last file operation
POST /api/fs/redo              Redo last undone operation

GET  /api/search?q=            Everything SDK search
POST /api/analysis/run         Recursive folder size analysis

GET  /api/config/providers     List LLM providers
PUT  /api/config/active-provider  Switch active provider
PUT  /api/config/providers/{name} Update provider settings

WS   /ws/agent                 Streaming agent (tool calls + observations)
```
