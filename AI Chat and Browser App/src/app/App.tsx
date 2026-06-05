/**
 * App — top-level controller for the AIFM React front-end.
 *
 * Phase 4 additions over Phase 3:
 *   • AgentPanel replaces the "AI Preview" placeholder (streaming WS chat)
 *   • LLMSettingsDialog wired to the Settings button (provider CRUD)
 *   • PreviewPanel now shows text content + inline images
 */
import {
  useState, useEffect, useCallback, useRef,
  type MouseEvent,
} from 'react';
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import {
  Sparkles, ChevronLeft, ChevronRight, Search,
  Undo2, Redo2, ScanSearch, FolderPlus, Copy, Scissors,
  Trash2, Settings, RefreshCw, Loader2, AlertCircle,
  ClipboardPaste,
} from 'lucide-react';

import {
  listFolder, searchFiles, pasteFiles, deleteFiles,
  undoOperation, redoOperation, getUndoState,
  runAnalysis, createAIFolder,
} from '../api/client';
import type { FileEntry, SortCol, SortDir } from '../api/types';

import { FolderTree }         from './components/FolderTree';
import { FileTable, toSearchEntries, type SearchEntry } from './components/FileTable';
import { PreviewPanel }        from './components/PreviewPanel';
import { AgentPanel }          from './components/AgentPanel';
import { LLMSettingsDialog }   from './components/LLMSettingsDialog';
import { DeleteConfirmDialog } from './components/DeleteConfirmDialog';
import { NewAIFolderDialog }   from './components/NewAIFolderDialog';

import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
} from './components/ui/context-menu';

// ── Types ─────────────────────────────────────────────────────────────────────

interface Clipboard { paths: string[]; move: boolean }
interface ContextTarget { path: string; is_dir: boolean }

// ── Constants ─────────────────────────────────────────────────────────────────

const DEFAULT_PATH = 'C:\\';
const TEMP_STATUS_DURATION = 3500;

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {

  // ── Navigation ──────────────────────────────────────────────────────────────
  const [currentPath, setCurrentPath] = useState(DEFAULT_PATH);
  const [navInput,    setNavInput]    = useState(DEFAULT_PATH);
  const [history,     setHistory]     = useState<string[]>([DEFAULT_PATH]);
  const [histIdx,     setHistIdx]     = useState(0);

  // ── File table ──────────────────────────────────────────────────────────────
  const [entries,     setEntries]     = useState<FileEntry[]>([]);
  const [loading,     setLoading]     = useState(false);
  const [sortCol,     setSortCol]     = useState<SortCol>('name');
  const [sortDir,     setSortDir]     = useState<SortDir>('asc');
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [focusedPath, setFocusedPath] = useState<string | null>(null);
  const [folderMeta,  setFolderMeta]  = useState({ total: 0, files: 0, folders: 0 });

  // ── Search mode ─────────────────────────────────────────────────────────────
  const [isSearchMode,  setIsSearchMode]  = useState(false);
  const [searchEntries, setSearchEntries] = useState<SearchEntry[]>([]);
  const [searchMeta,    setSearchMeta]    = useState<{ query: string; shown: number; total: number; message: string } | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);

  // ── Write operations ────────────────────────────────────────────────────────
  const [clipboard,     setClipboard]    = useState<Clipboard | null>(null);
  const [canUndo,       setCanUndo]      = useState(false);
  const [canRedo,       setCanRedo]      = useState(false);
  const [opLoading,     setOpLoading]    = useState(false);
  const [analyseLoading,setAnalyseLoading] = useState(false);

  // ── Dialogs ─────────────────────────────────────────────────────────────────
  const [delConfirmOpen,   setDelConfirmOpen]   = useState(false);
  const [pathsToDelete,    setPathsToDelete]    = useState<string[]>([]);
  const [aiFolderOpen,     setAiFolderOpen]     = useState(false);
  const [aiFolderParent,   setAiFolderParent]   = useState('');
  const [llmSettingsOpen,  setLlmSettingsOpen]  = useState(false);

  // ── Context menu ────────────────────────────────────────────────────────────
  const [contextTarget, setContextTarget] = useState<ContextTarget | null>(null);

  // ── Status bar ──────────────────────────────────────────────────────────────
  const [tempStatus, setTempStatus] = useState<string | null>(null);
  const tempTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  // ── Helpers ──────────────────────────────────────────────────────────────────

  const showStatus = useCallback((msg: string, dur = TEMP_STATUS_DURATION) => {
    setTempStatus(msg);
    if (tempTimerRef.current) clearTimeout(tempTimerRef.current);
    tempTimerRef.current = setTimeout(() => setTempStatus(null), dur);
  }, []);

  const syncUndoState = useCallback((can_undo: boolean, can_redo: boolean) => {
    setCanUndo(can_undo);
    setCanRedo(can_redo);
  }, []);

  // ── Navigation ───────────────────────────────────────────────────────────────

  const navigateTo = useCallback(async (path: string, addToHistory = true) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setLoading(true);
    setIsSearchMode(false);
    setSelectedPaths(new Set());
    setFocusedPath(null);

    try {
      const data = await listFolder(path);
      if (ctrl.signal.aborted) return;

      setCurrentPath(data.path);
      setNavInput(data.path);
      setEntries(data.entries);
      setFolderMeta({ total: data.total, files: data.files, folders: data.folders });

      if (addToHistory) {
        setHistory(prev => {
          const trimmed = prev.slice(0, histIdx + 1);
          if (trimmed.at(-1)?.toLowerCase() === data.path.toLowerCase()) return trimmed;
          return [...trimmed, data.path];
        });
        setHistIdx(prev => prev + 1);
      }
    } catch (err: unknown) {
      if (ctrl.signal.aborted) return;
      // Treat navigation failures as search queries.
      if (addToHistory) runSearch(path);
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [histIdx]);

  const runSearch = useCallback(async (query: string) => {
    setSearchLoading(true);
    setIsSearchMode(true);
    setSelectedPaths(new Set());
    setFocusedPath(null);
    setNavInput(query);
    try {
      const data = await searchFiles(query);
      setSearchEntries(toSearchEntries(data.paths));
      setSearchMeta({ query: data.query, shown: data.shown, total: data.total, message: data.message });
    } catch (err: unknown) {
      showStatus(`Search error: ${err instanceof Error ? err.message : String(err)}`);
      setSearchEntries([]);
    } finally {
      setSearchLoading(false);
    }
  }, [showStatus]);

  const handleNavSubmit = useCallback(() => {
    const raw = navInput.trim();
    if (raw) navigateTo(raw);
  }, [navInput, navigateTo]);

  const canGoBack    = histIdx > 0;
  const canGoForward = histIdx < history.length - 1;

  const goBack = useCallback(() => {
    if (!canGoBack) return;
    const idx = histIdx - 1;
    setHistIdx(idx);
    navigateTo(history[idx], false);
  }, [canGoBack, histIdx, history, navigateTo]);

  const goForward = useCallback(() => {
    if (!canGoForward) return;
    const idx = histIdx + 1;
    setHistIdx(idx);
    navigateTo(history[idx], false);
  }, [canGoForward, histIdx, history, navigateTo]);

  const handleRefresh = useCallback(() => {
    if (isSearchMode && searchMeta) runSearch(searchMeta.query);
    else navigateTo(currentPath, false);
  }, [isSearchMode, searchMeta, currentPath, navigateTo, runSearch]);

  // ── Sort / Select ────────────────────────────────────────────────────────────

  const handleSort = useCallback((col: SortCol) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir('asc'); }
  }, [sortCol]);

  const handleSelect = useCallback((path: string, checked: boolean) => {
    setSelectedPaths(prev => {
      const next = new Set(prev);
      if (checked) next.add(path); else next.delete(path);
      return next;
    });
  }, []);

  const handleSelectAll = useCallback((checked: boolean) => {
    if (isSearchMode) {
      setSelectedPaths(checked ? new Set(searchEntries.map(e => e.path)) : new Set());
    } else {
      setSelectedPaths(checked ? new Set(entries.map(e => e.path)) : new Set());
    }
  }, [isSearchMode, searchEntries, entries]);

  // ── Clipboard ────────────────────────────────────────────────────────────────

  const handleCopy = useCallback(() => {
    const paths = Array.from(selectedPaths);
    if (!paths.length) { showStatus('No items selected.'); return; }
    setClipboard({ paths, move: false });
    showStatus(`Copied ${paths.length} item(s) to clipboard`);
  }, [selectedPaths, showStatus]);

  const handleCut = useCallback(() => {
    const paths = Array.from(selectedPaths);
    if (!paths.length) { showStatus('No items selected.'); return; }
    setClipboard({ paths, move: true });
    showStatus(`Cut ${paths.length} item(s) to clipboard`);
  }, [selectedPaths, showStatus]);

  const canPaste = !isSearchMode && (clipboard?.paths.length ?? 0) > 0;

  const handlePaste = useCallback(async () => {
    if (!canPaste || !clipboard) { showStatus('Nothing to paste.'); return; }
    setOpLoading(true);
    try {
      const res = await pasteFiles(clipboard.paths, currentPath, clipboard.move);
      syncUndoState(res.can_undo, res.can_redo);
      if (clipboard.move) setClipboard(null);
      await navigateTo(currentPath, false);
      showStatus(
        `${clipboard.move ? 'Moved' : 'Copied'}: ${res.done.length}` +
        (res.errors.length ? ` | Errors: ${res.errors.length}` : '')
      );
    } catch (err: unknown) {
      showStatus(`Paste failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setOpLoading(false);
    }
  }, [canPaste, clipboard, currentPath, navigateTo, syncUndoState, showStatus]);

  // ── Delete ───────────────────────────────────────────────────────────────────

  const DRIVE_ROOT_RE = /^[A-Za-z]:[/\\]?$/;

  const handleDeleteRequest = useCallback(() => {
    const paths = Array.from(selectedPaths).filter(p => !DRIVE_ROOT_RE.test(p));
    if (!paths.length) { showStatus('No items selected for delete.'); return; }
    setPathsToDelete(paths);
    setDelConfirmOpen(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPaths, showStatus]);

  const handleDeleteConfirmed = useCallback(async () => {
    setDelConfirmOpen(false);
    setOpLoading(true);
    try {
      const res = await deleteFiles(pathsToDelete);
      syncUndoState(res.can_undo, res.can_redo);
      setSelectedPaths(new Set());
      await navigateTo(currentPath, false);
      showStatus(
        `Deleted: ${res.done.length}` +
        (res.errors.length ? ` | Errors: ${res.errors.length}` : '')
      );
    } catch (err: unknown) {
      showStatus(`Delete failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setOpLoading(false);
    }
  }, [pathsToDelete, currentPath, navigateTo, syncUndoState, showStatus]);

  // ── Undo / Redo ──────────────────────────────────────────────────────────────

  const handleUndo = useCallback(async () => {
    if (!canUndo) return;
    setOpLoading(true);
    try {
      const res = await undoOperation();
      syncUndoState(res.can_undo, res.can_redo);
      await navigateTo(currentPath, false);
      showStatus(
        `Undo: ${res.done.length}` +
        (res.errors.length ? ` | Errors: ${res.errors.length}` : '')
      );
    } catch (err: unknown) {
      showStatus(`Undo failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setOpLoading(false);
    }
  }, [canUndo, currentPath, navigateTo, syncUndoState, showStatus]);

  const handleRedo = useCallback(async () => {
    if (!canRedo) return;
    setOpLoading(true);
    try {
      const res = await redoOperation();
      syncUndoState(res.can_undo, res.can_redo);
      await navigateTo(currentPath, false);
      showStatus(
        `Redo: ${res.done.length}` +
        (res.errors.length ? ` | Errors: ${res.errors.length}` : '')
      );
    } catch (err: unknown) {
      showStatus(`Redo failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setOpLoading(false);
    }
  }, [canRedo, currentPath, navigateTo, syncUndoState, showStatus]);

  // ── Analyse ──────────────────────────────────────────────────────────────────

  const handleAnalyse = useCallback(async (targetPath?: string) => {
    // Prefer explicit path, then focused folder, then current folder.
    const path =
      targetPath ??
      (focusedPath && entries.find(e => e.path === focusedPath && e.is_dir)?.path) ??
      currentPath;

    setAnalyseLoading(true);
    showStatus(`Analysing: ${path}`, 60_000);
    try {
      const res = await runAnalysis(path);
      await navigateTo(currentPath, false);   // Refresh Size column.
      showStatus(
        `Analysed: ${res.folder_count + 1} folders | ${res.file_count} files | ${res.size}`
      );
    } catch (err: unknown) {
      showStatus(`Analysis failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAnalyseLoading(false);
    }
  }, [focusedPath, entries, currentPath, navigateTo, showStatus]);

  // ── New AIFolder ─────────────────────────────────────────────────────────────

  const handleNewAIFolder = useCallback((parentPath?: string) => {
    setAiFolderParent(parentPath ?? currentPath);
    setAiFolderOpen(true);
  }, [currentPath]);

  const handleCreateAIFolder = useCallback(async (parent: string, name: string, authMode: string) => {
    setAiFolderOpen(false);
    setOpLoading(true);
    try {
      await createAIFolder(parent, name, authMode);
      await navigateTo(parent, false);
      showStatus(`Created AIFolder: ${name} (${authMode})`);
    } catch (err: unknown) {
      showStatus(`AIFolder creation failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setOpLoading(false);
    }
  }, [navigateTo, showStatus]);

  // ── Context menu helper ───────────────────────────────────────────────────────

  const handleTableContextMenu = useCallback((e: MouseEvent) => {
    const row = (e.target as Element).closest('[data-path]');
    const path = row?.getAttribute('data-path') ?? null;
    if (path && !isSearchMode) {
      const entry = entries.find(x => x.path === path) ?? null;
      if (entry) {
        setContextTarget({ path, is_dir: entry.is_dir });
        // Auto-select the right-clicked item if not already in selection.
        if (!selectedPaths.has(path)) {
          setSelectedPaths(new Set([path]));
          setFocusedPath(path);
        }
      } else {
        setContextTarget(null);
      }
    } else {
      setContextTarget(null);
    }
  }, [entries, isSearchMode, selectedPaths]);

  // ── Keyboard shortcuts ───────────────────────────────────────────────────────
  // Use a ref to always have the latest handler versions without re-attaching.

  const handlersRef = useRef({
    handleCopy, handleCut, handlePaste, handleDeleteRequest,
    handleUndo, handleRedo, handleSelectAll,
  });
  useEffect(() => {
    handlersRef.current = {
      handleCopy, handleCut, handlePaste, handleDeleteRequest,
      handleUndo, handleRedo, handleSelectAll,
    };
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as Element).tagName.toLowerCase();
      if (['input', 'textarea', 'select'].includes(tag)) return;

      const h = handlersRef.current;
      if (e.ctrlKey && !e.shiftKey) {
        switch (e.key.toLowerCase()) {
          case 'c': e.preventDefault(); h.handleCopy(); return;
          case 'x': e.preventDefault(); h.handleCut();  return;
          case 'v': e.preventDefault(); h.handlePaste(); return;
          case 'a': e.preventDefault(); h.handleSelectAll(true); return;
          case 'z': e.preventDefault(); h.handleUndo(); return;
          case 'y': e.preventDefault(); h.handleRedo(); return;
        }
      }
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'z') {
        e.preventDefault(); h.handleRedo(); return;
      }
      if (e.key === 'Delete' && !e.ctrlKey) {
        e.preventDefault(); h.handleDeleteRequest();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);   // Runs once; latest handlers accessed via ref.

  // ── Initialisation ───────────────────────────────────────────────────────────

  useEffect(() => {
    navigateTo(DEFAULT_PATH, false);
    getUndoState().then(r => syncUndoState(r.can_undo, r.can_redo)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Status text ──────────────────────────────────────────────────────────────

  const baseStatus = isSearchMode && searchMeta
    ? `Results: ${searchMeta.shown} shown / ${searchMeta.total} total${searchMeta.message ? '  |  ' + searchMeta.message : ''}`
    : `Items: ${folderMeta.total}  |  Files: ${folderMeta.files}  |  Folders: ${folderMeta.folders}`;

  const statusText = tempStatus ?? baseStatus;

  const busy = loading || searchLoading || opLoading || analyseLoading;

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div className="size-full flex bg-white">

      {/* ── Left sidebar ────────────────────────────────────────────────────── */}
      <div className="w-[180px] bg-gray-50 border-r border-gray-200 flex flex-col shrink-0">
        <div className="p-3 space-y-2 flex-1 overflow-y-auto">

          <button
            onClick={() => handleAnalyse()}
            disabled={analyseLoading}
            className="w-full px-3 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-60"
          >
            {analyseLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ScanSearch className="w-4 h-4" />}
            Analyse
          </button>

          <button
            onClick={() => handleNewAIFolder()}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2"
          >
            <FolderPlus className="w-4 h-4" /> New AIFolder
          </button>

          <button
            onClick={handleCopy}
            disabled={selectedPaths.size === 0}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40"
          >
            <Copy className="w-4 h-4" /> Copy
          </button>

          <button
            onClick={handleCut}
            disabled={selectedPaths.size === 0}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40"
          >
            <Scissors className="w-4 h-4" /> Cut
          </button>

          <button
            onClick={handlePaste}
            disabled={!canPaste || opLoading}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40"
          >
            <ClipboardPaste className="w-4 h-4" />
            {clipboard?.move ? 'Move here' : 'Paste'}
          </button>

          <button
            onClick={handleDeleteRequest}
            disabled={selectedPaths.size === 0 || opLoading}
            className="w-full px-3 py-2 bg-white border border-red-200 text-red-600 rounded-md hover:bg-red-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40 disabled:text-gray-400 disabled:border-gray-300"
          >
            <Trash2 className="w-4 h-4" /> Delete
          </button>

          <button
            onClick={handleRefresh}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2"
          >
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>

          <button
            onClick={() => setLlmSettingsOpen(true)}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2"
          >
            <Settings className="w-4 h-4" /> Settings
          </button>
        </div>

        {/* LLM quick-access button at the bottom of the sidebar */}
        <div className="p-3 border-t border-gray-200 shrink-0">
          <button
            onClick={() => setLlmSettingsOpen(true)}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2"
          >
            <Settings className="w-4 h-4" /> LLM Settings
          </button>
        </div>
      </div>

      {/* ── Main content ────────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0">

        {/* Title row */}
        <div className="px-4 py-2.5 flex items-center gap-2 border-b border-gray-200">
          <Sparkles className="w-5 h-5 text-blue-600 shrink-0" />
          <h1 className="font-semibold text-gray-900">AI File Manager</h1>
          {busy && <Loader2 className="w-4 h-4 animate-spin text-blue-400 ml-auto" />}
        </div>

        {/* Navigation toolbar */}
        <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center gap-2">
          <button onClick={goBack} disabled={!canGoBack}
            className="p-1.5 rounded hover:bg-gray-200 disabled:opacity-40 disabled:cursor-not-allowed">
            <ChevronLeft className="w-4 h-4 text-gray-600" />
          </button>
          <button onClick={goForward} disabled={!canGoForward}
            className="p-1.5 rounded hover:bg-gray-200 disabled:opacity-40 disabled:cursor-not-allowed">
            <ChevronRight className="w-4 h-4 text-gray-600" />
          </button>
          <input
            type="text"
            value={navInput}
            onChange={e => setNavInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleNavSubmit()}
            className="flex-1 px-3 py-1.5 text-sm border border-gray-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-500 min-w-0"
            placeholder="Path or search query…"
          />
          <button onClick={handleNavSubmit} disabled={busy}
            className="px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700 flex items-center gap-1.5 shrink-0 disabled:opacity-60">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
            <span className="text-sm font-medium">Go</span>
          </button>
        </div>

        {/* Three-pane content */}
        <div className="flex-1 overflow-hidden">
          <PanelGroup direction="horizontal">

            {/* ── Tree + Table ───────────────────────────────────────────── */}
            <Panel defaultSize={65} minSize={40}>
              <PanelGroup direction="horizontal">

                {/* Folder tree */}
                <Panel defaultSize={28} minSize={15}>
                  <FolderTree currentPath={currentPath} onNavigate={navigateTo} />
                </Panel>

                <PanelResizeHandle className="w-px bg-gray-200 hover:bg-blue-400 transition-colors" />

                {/* File table + status bar */}
                <Panel defaultSize={72}>
                  <div className="h-full flex flex-col">

                    {/* Context-menu wraps the table */}
                    <ContextMenu onOpenChange={open => { if (!open) setContextTarget(null); }}>
                      <ContextMenuTrigger asChild>
                      <div
                        className="flex-1 overflow-hidden"
                        onContextMenu={handleTableContextMenu}
                      >
                        <FileTable
                          entries={entries}
                          isSearchMode={isSearchMode}
                          searchEntries={searchEntries}
                          sortCol={sortCol}
                          sortDir={sortDir}
                          selectedPaths={selectedPaths}
                          focusedPath={focusedPath}
                          onSort={handleSort}
                          onSelect={handleSelect}
                          onSelectAll={handleSelectAll}
                          onFocus={path => setFocusedPath(path)}
                          onNavigate={navigateTo}
                        />
                      </div>
                      </ContextMenuTrigger>

                      <ContextMenuContent className="w-56">
                        {/* Entry-specific items */}
                        {contextTarget && (
                          <>
                            {contextTarget.is_dir && (
                              <ContextMenuItem onClick={() => navigateTo(contextTarget.path)}>
                                Open
                              </ContextMenuItem>
                            )}
                            {contextTarget.is_dir && (
                              <ContextMenuItem onClick={() => handleAnalyse(contextTarget.path)}>
                                Analyse
                              </ContextMenuItem>
                            )}
                            <ContextMenuItem onClick={() => handleNewAIFolder(contextTarget.is_dir ? contextTarget.path : undefined)}>
                              New AIFolder…
                            </ContextMenuItem>
                            <ContextMenuSeparator />
                          </>
                        )}

                        {/* Clipboard */}
                        <ContextMenuItem onClick={handleCopy} disabled={selectedPaths.size === 0}>
                          Copy <ContextMenuShortcut>Ctrl+C</ContextMenuShortcut>
                        </ContextMenuItem>
                        <ContextMenuItem onClick={handleCut} disabled={selectedPaths.size === 0}>
                          Cut <ContextMenuShortcut>Ctrl+X</ContextMenuShortcut>
                        </ContextMenuItem>
                        <ContextMenuItem onClick={handlePaste} disabled={!canPaste}>
                          Paste <ContextMenuShortcut>Ctrl+V</ContextMenuShortcut>
                        </ContextMenuItem>
                        <ContextMenuItem
                          onClick={handleDeleteRequest}
                          disabled={selectedPaths.size === 0}
                          className="text-red-600 focus:text-red-600"
                        >
                          Delete <ContextMenuShortcut>Del</ContextMenuShortcut>
                        </ContextMenuItem>

                        <ContextMenuSeparator />

                        {/* Undo / Redo */}
                        <ContextMenuItem onClick={handleUndo} disabled={!canUndo}>
                          Undo <ContextMenuShortcut>Ctrl+Z</ContextMenuShortcut>
                        </ContextMenuItem>
                        <ContextMenuItem onClick={handleRedo} disabled={!canRedo}>
                          Redo <ContextMenuShortcut>Ctrl+Y</ContextMenuShortcut>
                        </ContextMenuItem>

                        <ContextMenuSeparator />

                        {/* Non-entry items */}
                        {!contextTarget && (
                          <ContextMenuItem onClick={() => handleNewAIFolder()}>
                            New AIFolder…
                          </ContextMenuItem>
                        )}
                        <ContextMenuItem onClick={handleRefresh}>
                          Refresh <ContextMenuShortcut>F5</ContextMenuShortcut>
                        </ContextMenuItem>
                        <ContextMenuItem onClick={() => handleSelectAll(true)}>
                          Select All <ContextMenuShortcut>Ctrl+A</ContextMenuShortcut>
                        </ContextMenuItem>
                      </ContextMenuContent>
                    </ContextMenu>

                    {/* Status bar */}
                    <div className="shrink-0 bg-gray-50 border-t border-gray-200 px-3 py-1 flex items-center gap-3">
                      <button
                        onClick={handleUndo}
                        disabled={!canUndo || opLoading}
                        title="Undo (Ctrl+Z)"
                        className="p-1 rounded hover:bg-gray-200 disabled:opacity-30 disabled:cursor-not-allowed"
                      >
                        <Undo2 className="w-3.5 h-3.5 text-gray-600" />
                      </button>
                      <button
                        onClick={handleRedo}
                        disabled={!canRedo || opLoading}
                        title="Redo (Ctrl+Y)"
                        className="p-1 rounded hover:bg-gray-200 disabled:opacity-30 disabled:cursor-not-allowed"
                      >
                        <Redo2 className="w-3.5 h-3.5 text-gray-600" />
                      </button>
                      <span className={`text-xs truncate flex-1 ${tempStatus ? 'text-blue-700 font-medium' : 'text-gray-600'}`}>
                        {statusText}
                      </span>
                      {selectedPaths.size > 0 && (
                        <span className="text-xs text-blue-600 shrink-0">
                          {selectedPaths.size} selected
                        </span>
                      )}
                      {clipboard && (
                        <span className="text-xs text-amber-600 shrink-0">
                          {clipboard.paths.length} {clipboard.move ? 'cut' : 'copied'}
                        </span>
                      )}
                    </div>
                  </div>
                </Panel>
              </PanelGroup>
            </Panel>

            <PanelResizeHandle className="w-px bg-gray-200 hover:bg-blue-400 transition-colors" />

            {/* ── Preview panels ─────────────────────────────────────────── */}
            <Panel defaultSize={35} minSize={20}>
              <PanelGroup direction="vertical">

                <Panel defaultSize={50} minSize={25}>
                  <div className="h-full bg-white flex flex-col border-l border-gray-200">
                    <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 shrink-0">
                      <h3 className="text-sm font-semibold text-gray-900">File Preview</h3>
                    </div>
                    <div className="flex-1 overflow-hidden">
                      <PreviewPanel path={focusedPath} />
                    </div>
                  </div>
                </Panel>

                <PanelResizeHandle className="h-px bg-gray-200 hover:bg-blue-400 transition-colors" />

                <Panel defaultSize={50} minSize={25}>
                  <div className="h-full bg-white flex flex-col border-l border-gray-200">
                    <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 shrink-0 flex items-center gap-1.5">
                      <Sparkles className="w-3.5 h-3.5 text-blue-500" />
                      <h3 className="text-sm font-semibold text-gray-900">AI Agent</h3>
                    </div>
                    <div className="flex-1 overflow-hidden">
                      <AgentPanel
                        currentPath={currentPath}
                        selectedPaths={selectedPaths}
                        focusedPath={focusedPath}
                        searchQuery={isSearchMode && searchMeta ? searchMeta.query : ''}
                        onRefresh={handleRefresh}
                      />
                    </div>
                  </div>
                </Panel>
              </PanelGroup>
            </Panel>
          </PanelGroup>
        </div>
      </div>

      {/* ── Dialogs ─────────────────────────────────────────────────────────── */}
      <DeleteConfirmDialog
        open={delConfirmOpen}
        paths={pathsToDelete}
        onConfirm={handleDeleteConfirmed}
        onCancel={() => setDelConfirmOpen(false)}
      />

      <NewAIFolderDialog
        open={aiFolderOpen}
        defaultParent={aiFolderParent}
        onConfirm={handleCreateAIFolder}
        onCancel={() => setAiFolderOpen(false)}
      />

      <LLMSettingsDialog
        open={llmSettingsOpen}
        onClose={() => setLlmSettingsOpen(false)}
      />
    </div>
  );
}
