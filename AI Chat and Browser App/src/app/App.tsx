/**
 * App — top-level controller for the AIFM React front-end.
 *
 * Multi-tab support: each tab has independent browsing state (path, history,
 * entries, sorting, selection, search mode).  Shared state (clipboard,
 * undo/redo, dialogs) lives outside the tabs array.
 */
import {
  useState, useEffect, useCallback, useRef, useMemo,
  type MouseEvent, type KeyboardEvent,
} from 'react';
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import {
  Sparkles, ChevronLeft, ChevronRight, Search,
  Undo2, Redo2, ScanSearch, FolderPlus, Copy, Scissors,
  Trash2, Settings, RefreshCw, Loader2,
  ClipboardPaste, Plus, X, LayoutGrid, FolderOpen,
} from 'lucide-react';

import {
  API_BASE, listFolder, searchFiles, pasteFiles, deleteFiles,
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
import { AppsGrid }            from './components/AppsGrid';
import { AppFilesDialog }      from './components/AppFilesDialog';

import {
  ContextMenu, ContextMenuTrigger, ContextMenuContent,
  ContextMenuItem, ContextMenuSeparator, ContextMenuShortcut,
} from './components/ui/context-menu';

// ── Types ─────────────────────────────────────────────────────────────────────

interface Clipboard { paths: string[]; move: boolean }
interface ContextTarget { path: string; is_dir: boolean }

type TabState = {
  id: string
  path: string
  history: string[]
  historyIndex: number
  label: string             // last path segment, or "C:" for drive root
  navInput: string
  entries: FileEntry[]
  loading: boolean
  sortCol: SortCol
  sortDir: SortDir
  selectedPaths: Set<string>
  focusedPath: string | null
  folderMeta: { total: number; files: number; folders: number }
  isSearchMode: boolean
  searchEntries: SearchEntry[]
  searchMeta: { query: string; shown: number; total: number; message: string } | null
  searchLoading: boolean
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const DEFAULT_PATH = 'C:\\';
const SEARCH_PREFIX = '__s__:';
const TEMP_STATUS_DURATION = 3500;
const DRIVE_ROOT_RE = /^[A-Za-z]:[/\\]?$/;

function tabLabelFromPath(p: string): string {
  const cleaned = p.replace(/\\+$/, '');
  if (DRIVE_ROOT_RE.test(cleaned)) return cleaned[0].toUpperCase() + ':';
  const parts = cleaned.split('\\');
  return parts[parts.length - 1] || cleaned;
}

function makeTab(path = DEFAULT_PATH): TabState {
  return {
    id: crypto.randomUUID(),
    path,
    history: [path],
    historyIndex: 0,
    label: tabLabelFromPath(path),
    navInput: path,
    entries: [],
    loading: false,
    sortCol: 'name',
    sortDir: 'asc',
    selectedPaths: new Set(),
    focusedPath: null,
    folderMeta: { total: 0, files: 0, folders: 0 },
    isSearchMode: false,
    searchEntries: [],
    searchMeta: null,
    searchLoading: false,
  };
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {

  // === Per-tab state ===
  const [tabs,        setTabs]        = useState<TabState[]>([makeTab()]);
  const [activeTabId, setActiveTabId] = useState(tabs[0].id);

  // Derive the active tab (never null — always at least one tab).
  const activeTab = useMemo(
    () => tabs.find(t => t.id === activeTabId) ?? tabs[0],
    [tabs, activeTabId],
  );

  // Abort controllers live in a ref so navigateTo doesn't depend on `tabs`.
  const abortRefs = useRef<Map<string, AbortController>>(new Map());
  // Active tab id ref — always up-to-date, used inside navigateTo/timeouts.
  const activeTabIdRef = useRef(activeTabId);
  useEffect(() => { activeTabIdRef.current = activeTabId; }, [activeTabId]);

  // Convenience updater: applys a patch to ONLY the active tab.
  const patchActive = useCallback((patch: Partial<TabState>) => {
    setTabs(prev => prev.map(t => t.id === activeTabId ? { ...t, ...patch } : t));
  }, [activeTabId]);

  // === Shared (non-tab) state ===

  const [clipboard,     setClipboard]    = useState<Clipboard | null>(null);
  const [canUndo,       setCanUndo]      = useState(false);
  const [canRedo,       setCanRedo]      = useState(false);
  const [opLoading,     setOpLoading]    = useState(false);
  const [analyseLoading,setAnalyseLoading] = useState(false);

  const [delConfirmOpen,   setDelConfirmOpen]   = useState(false);
  const [pathsToDelete,    setPathsToDelete]    = useState<string[]>([]);
  const [aiFolderOpen,     setAiFolderOpen]     = useState(false);
  const [aiFolderParent,   setAiFolderParent]   = useState('');
  const [llmSettingsOpen,  setLlmSettingsOpen]  = useState(false);
  const [appFilesDialogOpen, setAppFilesDialogOpen] = useState(false);
  const [appFilesDialogName, setAppFilesDialogName] = useState('');
  const [configVersion,    setConfigVersion]    = useState(0);
  const [view,             setView]             = useState<'files' | 'apps'>('files');
  const [previousView,     setPreviousView]     = useState<'files' | 'apps' | null>(null);

  const switchView = useCallback((v: 'files' | 'apps') => {
    if (v === view) return;
    setPreviousView(view);
    setView(v);
  }, [view]);

  const [contextTarget, setContextTarget] = useState<ContextTarget | null>(null);
  const [tempStatus,   setTempStatus]     = useState<string | null>(null);
  const tempTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showStatus = useCallback((msg: string, dur = TEMP_STATUS_DURATION) => {
    setTempStatus(msg);
    if (tempTimerRef.current) clearTimeout(tempTimerRef.current);
    tempTimerRef.current = setTimeout(() => setTempStatus(null), dur);
  }, []);

  const syncUndoState = useCallback((u: boolean, r: boolean) => {
    setCanUndo(u); setCanRedo(r);
  }, []);

  // ── Navigation (per-tab) ─────────────────────────────────────────────────

  const navigateTo = useCallback(async (path: string, addToHistory = true) => {
    const tabId = activeTabIdRef.current;   // always fresh, even inside setTimeout
    abortRefs.current.get(tabId)?.abort();
    const ctrl = new AbortController();
    abortRefs.current.set(tabId, ctrl);

    // Mark this tab as loading, reset its select/search state.
    setTabs(prev => prev.map(t =>
      t.id === tabId
        ? { ...t, loading: true, isSearchMode: false, selectedPaths: new Set(), focusedPath: null }
        : t,
    ));

    try {
      const data = await listFolder(path);
      if (ctrl.signal.aborted) return;

      setTabs(prev => prev.map(t => {
        if (t.id !== tabId) return t;
        let newHist = t.history;
        let newIdx = t.historyIndex;
        if (addToHistory) {
          const trimmed = t.history.slice(0, t.historyIndex + 1);
          if (trimmed.at(-1)?.toLowerCase() !== data.path.toLowerCase()) {
            trimmed.push(data.path);
          }
          newHist = trimmed;
          newIdx = newHist.length - 1;
        }
        return {
          ...t,
          path: data.path,
          navInput: data.path,
          label: tabLabelFromPath(data.path),
          entries: data.entries,
          folderMeta: { total: data.total, files: data.files, folders: data.folders },
          history: newHist,
          historyIndex: newIdx,
          loading: false,
        };
      }));
    } catch (err: unknown) {
      if (ctrl.signal.aborted) return;
      setTabs(prev => prev.map(t => t.id === tabId ? { ...t, loading: false } : t));
      if (addToHistory) runSearch(path);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTabId]);

  const runSearch = useCallback(async (query: string, addToHistory = true) => {
    const tabId = activeTabId;
    setTabs(prev => prev.map(t =>
      t.id === tabId
        ? { ...t, searchLoading: true, isSearchMode: true, selectedPaths: new Set(), focusedPath: null, navInput: query }
        : t,
    ));
    try {
      const data = await searchFiles(query);
      setTabs(prev => prev.map(t => {
        if (t.id !== tabId) return t;
        const entry = SEARCH_PREFIX + query;
        let newHist = t.history;
        let newIdx = t.historyIndex;
        if (addToHistory) {
          const trimmed = t.history.slice(0, t.historyIndex + 1);
          if (trimmed.at(-1) !== entry) trimmed.push(entry);
          newHist = trimmed;
          newIdx = newHist.length - 1;
        }
        return {
          ...t, searchLoading: false,
          searchEntries: toSearchEntries(data.paths),
          searchMeta: { query: data.query, shown: data.shown, total: data.total, message: data.message },
          history: newHist, historyIndex: newIdx, path: entry,
        };
      }));
    } catch (err: unknown) {
      showStatus(`Search error: ${err instanceof Error ? err.message : String(err)}`);
      setTabs(prev => prev.map(t =>
        t.id === tabId ? { ...t, searchLoading: false, searchEntries: [] } : t,
      ));
    }
  }, [activeTabId, showStatus]);

  const handleNavSubmit = useCallback(() => {
    const raw = activeTab.navInput.trim();
    if (raw) navigateTo(raw);
  }, [activeTab.navInput, navigateTo]);

  const canGoBack    = activeTab.historyIndex > 0;
  const canGoForward = activeTab.historyIndex < activeTab.history.length - 1;

  const goBack = useCallback(() => {
    if (!canGoBack) return;
    const idx = activeTab.historyIndex - 1;
    const entry = activeTab.history[idx];
    setTabs(prev => prev.map(t => t.id === activeTabId ? { ...t, historyIndex: idx } : t));
    if (entry.startsWith(SEARCH_PREFIX)) {
      runSearch(entry.slice(SEARCH_PREFIX.length), false);
    } else {
      navigateTo(entry, false);
    }
  }, [canGoBack, activeTabId, activeTab.history, activeTab.historyIndex, navigateTo, runSearch]);

  const goForward = useCallback(() => {
    if (!canGoForward) return;
    const idx = activeTab.historyIndex + 1;
    const entry = activeTab.history[idx];
    setTabs(prev => prev.map(t => t.id === activeTabId ? { ...t, historyIndex: idx } : t));
    if (entry.startsWith(SEARCH_PREFIX)) {
      runSearch(entry.slice(SEARCH_PREFIX.length), false);
    } else {
      navigateTo(entry, false);
    }
  }, [canGoForward, activeTabId, activeTab.history, activeTab.historyIndex, navigateTo, runSearch]);

  const handleRefresh = useCallback(() => {
    if (activeTab.isSearchMode && activeTab.searchMeta) runSearch(activeTab.searchMeta.query, false);
    else navigateTo(activeTab.path, false);
  }, [activeTab.isSearchMode, activeTab.searchMeta, activeTab.path, navigateTo, runSearch]);

  // ── Sort / Select ─────────────────────────────────────────────────────────

  const handleSort = useCallback((col: SortCol) => {
    setTabs(prev => prev.map(t => {
      if (t.id !== activeTabId) return t;
      if (t.sortCol === col) return { ...t, sortDir: t.sortDir === 'asc' ? 'desc' : 'asc' as SortDir };
      return { ...t, sortCol: col, sortDir: 'asc' as SortDir };
    }));
  }, [activeTabId]);

  const handleSelect = useCallback((path: string, checked: boolean) => {
    setTabs(prev => prev.map(t => {
      if (t.id !== activeTabId) return t;
      const next = new Set(t.selectedPaths);
      if (checked) next.add(path); else next.delete(path);
      return { ...t, selectedPaths: next };
    }));
  }, [activeTabId]);

  const handleSelectAll = useCallback((checked: boolean) => {
    setTabs(prev => prev.map(t => {
      if (t.id !== activeTabId) return t;
      if (t.isSearchMode) return { ...t, selectedPaths: checked ? new Set(t.searchEntries.map(e => e.path)) : new Set() };
      return { ...t, selectedPaths: checked ? new Set(t.entries.map(e => e.path)) : new Set() };
    }));
  }, [activeTabId]);

  const handleSelectOnly = useCallback((path: string) => {
    patchActive({ selectedPaths: new Set([path]), focusedPath: path });
  }, [patchActive]);

  const handleSelectRange = useCallback((fromPath: string, toPath: string) => {
    setTabs(prev => prev.map(t => {
      if (t.id !== activeTabId) return t;
      const list = t.isSearchMode ? t.searchEntries.map(e => e.path) : t.entries.map(e => e.path);
      const fi = list.indexOf(fromPath), ti = list.indexOf(toPath);
      if (fi < 0 || ti < 0) return t;
      const lo = Math.min(fi, ti), hi = Math.max(fi, ti);
      return { ...t, selectedPaths: new Set(list.slice(lo, hi + 1)) };
    }));
  }, [activeTabId]);

  // ── Clipboard ─────────────────────────────────────────────────────────────

  const { selectedPaths, focusedPath, entries, isSearchMode, path: currentPath } = activeTab;

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
      showStatus(`${clipboard.move ? 'Moved' : 'Copied'}: ${res.done.length}` +
        (res.errors.length ? ` | Errors: ${res.errors.length}` : ''));
    } catch (err: unknown) {
      showStatus(`Paste failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setOpLoading(false); }
  }, [canPaste, clipboard, currentPath, navigateTo, syncUndoState, showStatus]);

  // ── Delete ────────────────────────────────────────────────────────────────

  const handleDeleteRequest = useCallback(() => {
    const paths = Array.from(selectedPaths).filter(p => !DRIVE_ROOT_RE.test(p));
    if (!paths.length) { showStatus('No items selected for delete.'); return; }
    setPathsToDelete(paths);
    setDelConfirmOpen(true);
  }, [selectedPaths, showStatus]);

  const handleDeleteConfirmed = useCallback(async () => {
    setDelConfirmOpen(false); setOpLoading(true);
    try {
      const res = await deleteFiles(pathsToDelete);
      syncUndoState(res.can_undo, res.can_redo);
      patchActive({ selectedPaths: new Set() });
      await navigateTo(currentPath, false);
      showStatus(`Deleted: ${res.done.length}` + (res.errors.length ? ` | Errors: ${res.errors.length}` : ''));
    } catch (err: unknown) {
      showStatus(`Delete failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setOpLoading(false); }
  }, [pathsToDelete, currentPath, navigateTo, syncUndoState, showStatus, patchActive]);

  // ── Undo / Redo ───────────────────────────────────────────────────────────

  const handleUndo = useCallback(async () => {
    if (!canUndo) return; setOpLoading(true);
    try {
      const res = await undoOperation();
      syncUndoState(res.can_undo, res.can_redo);
      await navigateTo(currentPath, false);
      showStatus(`Undo: ${res.done.length}` + (res.errors.length ? ` | Errors: ${res.errors.length}` : ''));
    } catch (err: unknown) {
      showStatus(`Undo failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setOpLoading(false); }
  }, [canUndo, currentPath, navigateTo, syncUndoState, showStatus]);

  const handleRedo = useCallback(async () => {
    if (!canRedo) return; setOpLoading(true);
    try {
      const res = await redoOperation();
      syncUndoState(res.can_undo, res.can_redo);
      await navigateTo(currentPath, false);
      showStatus(`Redo: ${res.done.length}` + (res.errors.length ? ` | Errors: ${res.errors.length}` : ''));
    } catch (err: unknown) {
      showStatus(`Redo failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setOpLoading(false); }
  }, [canRedo, currentPath, navigateTo, syncUndoState, showStatus]);

  // ── Analyse ───────────────────────────────────────────────────────────────

  const handleAnalyse = useCallback(async (targetPath?: string) => {
    const path = targetPath ?? (focusedPath && entries.find(e => e.path === focusedPath && e.is_dir)?.path) ?? currentPath;
    setAnalyseLoading(true);
    showStatus(`Analysing: ${path}`, 60_000);
    try {
      const res = await runAnalysis(path);
      await navigateTo(currentPath, false);
      showStatus(`Analysed: ${res.folder_count + 1} folders | ${res.file_count} files | ${res.size}`);
    } catch (err: unknown) {
      showStatus(`Analysis failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setAnalyseLoading(false); }
  }, [focusedPath, entries, currentPath, navigateTo, showStatus]);

  // ── New AIFolder ──────────────────────────────────────────────────────────

  const handleNewAIFolder = useCallback((parentPath?: string) => {
    setAiFolderParent(parentPath ?? currentPath);
    setAiFolderOpen(true);
  }, [currentPath]);

  const handleCreateAIFolder = useCallback(async (parent: string, name: string, authMode: string) => {
    setAiFolderOpen(false); setOpLoading(true);
    try {
      await createAIFolder(parent, name, authMode);
      await navigateTo(parent, false);
      showStatus(`Created AIFolder: ${name} (${authMode})`);
    } catch (err: unknown) {
      showStatus(`AIFolder creation failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setOpLoading(false); }
  }, [navigateTo, showStatus]);

  // ── App file index ────────────────────────────────────────────────────────

  const handleViewAppFiles = useCallback((appName: string) => {
    setAppFilesDialogName(appName);
    setAppFilesDialogOpen(true);
  }, []);

  // ── Context menu ──────────────────────────────────────────────────────────

  const handleTableContextMenu = useCallback((e: MouseEvent) => {
    const row = (e.target as Element).closest('[data-path]');
    const path = row?.getAttribute('data-path') ?? null;
    if (path && !isSearchMode) {
      const entry = entries.find(x => x.path === path) ?? null;
      if (entry) {
        setContextTarget({ path, is_dir: entry.is_dir });
        if (!selectedPaths.has(path)) patchActive({ selectedPaths: new Set([path]), focusedPath: path });
      } else { setContextTarget(null); }
    } else { setContextTarget(null); }
  }, [entries, isSearchMode, selectedPaths, patchActive]);

  // ── Tab management ────────────────────────────────────────────────────────

  const handleNewTab = useCallback(() => {
    const t = makeTab();
    setTabs(prev => [...prev, t]);
    setActiveTabId(t.id);
  }, []);

  const handleCloseTab = useCallback((tabId: string) => {
    setTabs(prev => {
      if (prev.length <= 1) {
        // Last tab: reset to C:\ instead of removing.
        return [{ ...makeTab(), id: prev[0].id }];
      }
      const remaining = prev.filter(t => t.id !== tabId);
      if (tabId === activeTabId) {
        const idx = prev.findIndex(t => t.id === tabId);
        setActiveTabId(remaining[Math.min(idx, remaining.length - 1)].id);
      }
      // Clean up abort controller
      abortRefs.current.get(tabId)?.abort();
      abortRefs.current.delete(tabId);
      return remaining;
    });
  }, [activeTabId]);

  const handleTabClick = useCallback((tabId: string) => {
    if (tabId !== activeTabId) {
      setActiveTabId(tabId);
      // Re-navigate if the tab hasn't loaded yet (entries empty and path not C:\).
      const tab = tabs.find(t => t.id === tabId);
      if (tab && tab.entries.length === 0 && !tab.loading) {
        navigateTo(tab.path, false);
      }
    }
  }, [activeTabId, tabs, navigateTo]);

  // ── Keyboard shortcuts ───────────────────────────────────────────────────

  const handlersRef = useRef({ handleCopy, handleCut, handlePaste, handleDeleteRequest, handleUndo, handleRedo, handleSelectAll });
  useEffect(() => { handlersRef.current = { handleCopy, handleCut, handlePaste, handleDeleteRequest, handleUndo, handleRedo, handleSelectAll }; });

  const tabsRef = useRef(tabs);
  useEffect(() => { tabsRef.current = tabs; }, [tabs]);
  const activeRef = useRef(activeTabId);
  useEffect(() => { activeRef.current = activeTabId; }, [activeTabId]);

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
          case 't': e.preventDefault(); handleNewTab(); return;
          case 'w': e.preventDefault(); handleCloseTab(activeRef.current); return;
          case 'tab': {
            e.preventDefault();
            const ts = tabsRef.current;
            const idx = ts.findIndex(t => t.id === activeRef.current);
            const next = (idx + 1) % ts.length;
            setActiveTabId(ts[next].id);
            return;
          }
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
  }, [handleNewTab, handleCloseTab]);

  // ── Initialisation ────────────────────────────────────────────────────────

  useEffect(() => {
    navigateTo(DEFAULT_PATH, false);
    getUndoState().then(r => syncUndoState(r.can_undo, r.can_redo)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Status text ────────────────────────────────────────────────────────────

  const { folderMeta, searchMeta } = activeTab;
  const baseStatus = isSearchMode && searchMeta
    ? `Results: ${searchMeta.shown} shown / ${searchMeta.total} total${searchMeta.message ? '  |  ' + searchMeta.message : ''}`
    : `Items: ${folderMeta.total}  |  Files: ${folderMeta.files}  |  Folders: ${folderMeta.folders}`;
  const statusText = tempStatus ?? baseStatus;

  const busy = activeTab.loading || activeTab.searchLoading || opLoading || analyseLoading;

  // ── Tab bar scroll ref ─────────────────────────────────────────────────────

  const tabBarRef = useRef<HTMLDivElement>(null);

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="size-full flex bg-white">

      {/* ── Left sidebar ──────────────────────────────────────────────────── */}
      <div className="w-[180px] bg-gray-50 border-r border-gray-200 flex flex-col shrink-0">
        <div className="p-3 space-y-1 flex-1 overflow-y-auto">
          {/* View switcher */}
          <button
            onClick={() => switchView('files')}
            className={[
              'w-full px-3 py-2 rounded-md transition-colors text-sm font-medium flex items-center gap-2',
              view === 'files'
                ? 'bg-blue-50 text-blue-700 border border-blue-200'
                : 'text-gray-600 hover:bg-gray-100 border border-transparent',
            ].join(' ')}
          >
            <FolderOpen className="w-4 h-4" /> Files
          </button>
          <button
            onClick={() => switchView('apps')}
            className={[
              'w-full px-3 py-2 rounded-md transition-colors text-sm font-medium flex items-center gap-2',
              view === 'apps'
                ? 'bg-blue-50 text-blue-700 border border-blue-200'
                : 'text-gray-600 hover:bg-gray-100 border border-transparent',
            ].join(' ')}
          >
            <LayoutGrid className="w-4 h-4" /> Installed Apps
          </button>

          <div className="pt-2" />  {/* spacer */}

          <button onClick={() => handleAnalyse()} disabled={analyseLoading}
            className="w-full px-3 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-60">
            {analyseLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ScanSearch className="w-4 h-4" />}
            Analyse
          </button>
          <button onClick={() => handleNewAIFolder()}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2">
            <FolderPlus className="w-4 h-4" /> New AIFolder
          </button>
          <button onClick={handleCopy} disabled={selectedPaths.size === 0}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40">
            <Copy className="w-4 h-4" /> Copy
          </button>
          <button onClick={handleCut} disabled={selectedPaths.size === 0}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40">
            <Scissors className="w-4 h-4" /> Cut
          </button>
          <button onClick={handlePaste} disabled={!canPaste || opLoading}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40">
            <ClipboardPaste className="w-4 h-4" />{clipboard?.move ? 'Move here' : 'Paste'}
          </button>
          <button onClick={handleDeleteRequest} disabled={selectedPaths.size === 0 || opLoading}
            className="w-full px-3 py-2 bg-white border border-red-200 text-red-600 rounded-md hover:bg-red-50 transition-colors text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-40 disabled:text-gray-400 disabled:border-gray-300">
            <Trash2 className="w-4 h-4" /> Delete
          </button>
          <button onClick={handleRefresh}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2">
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
          <button onClick={() => setLlmSettingsOpen(true)}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2">
            <Settings className="w-4 h-4" /> Settings
          </button>
        </div>
        <div className="p-3 border-t border-gray-200 shrink-0">
          <button onClick={() => setLlmSettingsOpen(true)}
            className="w-full px-3 py-2 bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors text-sm font-medium flex items-center justify-center gap-2">
            <Settings className="w-4 h-4" /> LLM Settings
          </button>
        </div>
      </div>

      {/* ── Main content ──────────────────────────────────────────────────── */}
      {view === 'apps' ? (
        <div className="flex-1 flex flex-col min-w-0">
          <div className="px-4 py-2.5 flex items-center gap-2 border-b border-gray-200">
            {previousView && (
              <button onClick={() => { setView(previousView); setPreviousView(null); }}
                className="p-1 rounded hover:bg-gray-200 shrink-0" title={`Back to ${previousView}`}>
                <ChevronLeft className="w-4 h-4 text-gray-600" />
              </button>
            )}
            <LayoutGrid className="w-5 h-5 text-blue-600 shrink-0" />
            <h1 className="font-semibold text-gray-900">Installed Applications</h1>
          </div>
          <div className="flex-1 overflow-hidden">
            <AppsGrid
              onOpenInAIFM={(dir) => {
                const t = makeTab(dir);
                setTabs(prev => [...prev, t]);
                setActiveTabId(t.id);
                activeTabIdRef.current = t.id;
                switchView('files');
                navigateTo(dir, false);
              }}
              onOpenInExplorer={(dir) => {
                fetch(`${API_BASE}/api/fs/open`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ path: dir }),
                }).catch(() => {});
              }}
              onViewFiles={handleViewAppFiles}
            />
          </div>
        </div>
      ) : (
      <div className="flex-1 flex flex-col min-w-0">

        {/* Title row */}
        <div className="px-4 py-2.5 flex items-center gap-2 border-b border-gray-200">
          <Sparkles className="w-5 h-5 text-blue-600 shrink-0" />
          <h1 className="font-semibold text-gray-900">AI File Manager</h1>
          {previousView && (
            <button onClick={() => { setView(previousView); setPreviousView(null); }}
              className="p-1 rounded hover:bg-gray-200 shrink-0 ml-auto" title={`Back to ${previousView}`}>
              <ChevronLeft className="w-4 h-4 text-gray-600" />
            </button>
          )}
          {busy && <Loader2 className="w-4 h-4 animate-spin text-blue-400" />}
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
            value={activeTab.navInput}
            onChange={e => patchActive({ navInput: e.target.value })}
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

        {/* ── Tab bar ─────────────────────────────────────────────────────── */}
        <div
          ref={tabBarRef}
          className="flex items-center bg-gray-100 border-b border-gray-200 overflow-x-auto shrink-0"
        >
          {tabs.map(tab => (
            <div
              key={tab.id}
              onClick={() => handleTabClick(tab.id)}
              className={[
                'flex items-center gap-1 px-3 py-1.5 text-xs cursor-pointer border-r border-gray-200 hover:bg-gray-200 transition-colors select-none group shrink min-w-[72px]',
                tab.id === activeTabId ? 'bg-white border-b-[2px] border-b-blue-500 font-medium text-gray-900' : 'text-gray-600 border-b-2 border-b-transparent',
              ].join(' ')}
              style={{ flexBasis: '160px' }}
            >
              <span className="truncate flex-1 min-w-0">{tab.label}</span>
              <button
                onClick={e => { e.stopPropagation(); handleCloseTab(tab.id); }}
                className="w-4 h-4 rounded-full flex items-center justify-center text-gray-400 hover:text-gray-700 hover:bg-gray-300 transition-colors shrink-0"
                title="Close tab (Ctrl+W)"
              >
                <X className="w-3 h-3" />
              </button>
            </div>
          ))}
          <button
            onClick={handleNewTab}
            className="px-2.5 py-1.5 text-gray-500 hover:bg-gray-200 transition-colors shrink-0"
            title="New tab (Ctrl+T)"
          >
            <Plus className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Three-pane content */}
        <div className="flex-1 overflow-hidden">
          <PanelGroup direction="horizontal">
            <Panel defaultSize={65} minSize={40}>
              <PanelGroup direction="horizontal">
                <Panel defaultSize={28} minSize={15}>
                  <FolderTree currentPath={activeTab.path} onNavigate={navigateTo} />
                </Panel>
                <PanelResizeHandle className="w-px bg-gray-200 hover:bg-blue-400 transition-colors" />
                <Panel defaultSize={72}>
                  <div className="h-full flex flex-col">
                    <ContextMenu onOpenChange={open => { if (!open) setContextTarget(null); }}>
                      <ContextMenuTrigger asChild>
                      <div className="flex-1 overflow-hidden" onContextMenu={handleTableContextMenu}>
                        <FileTable
                          entries={activeTab.entries}
                          isSearchMode={activeTab.isSearchMode}
                          searchEntries={activeTab.searchEntries}
                          sortCol={activeTab.sortCol}
                          sortDir={activeTab.sortDir}
                          selectedPaths={activeTab.selectedPaths}
                          focusedPath={activeTab.focusedPath}
                          onSort={handleSort}
                          onSelect={handleSelect}
                          onSelectOnly={handleSelectOnly}
                          onSelectRange={handleSelectRange}
                          onSelectAll={handleSelectAll}
                          onFocus={p => patchActive({ focusedPath: p })}
                          onNavigate={navigateTo}
                        />
                      </div>
                      </ContextMenuTrigger>
                      <ContextMenuContent className="w-56">
                        {contextTarget && (<>
                          {contextTarget.is_dir && <ContextMenuItem onClick={() => navigateTo(contextTarget.path)}>Open</ContextMenuItem>}
                          {contextTarget.is_dir && <ContextMenuItem onClick={() => handleAnalyse(contextTarget.path)}>Analyse</ContextMenuItem>}
                          <ContextMenuItem onClick={() => handleNewAIFolder(contextTarget.is_dir ? contextTarget.path : undefined)}>New AIFolder…</ContextMenuItem>
                          <ContextMenuSeparator />
                        </>)}
                        <ContextMenuItem onClick={handleCopy} disabled={selectedPaths.size === 0}>Copy <ContextMenuShortcut>Ctrl+C</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuItem onClick={handleCut} disabled={selectedPaths.size === 0}>Cut <ContextMenuShortcut>Ctrl+X</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuItem onClick={handlePaste} disabled={!canPaste}>Paste <ContextMenuShortcut>Ctrl+V</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuItem onClick={handleDeleteRequest} disabled={selectedPaths.size === 0} className="text-red-600 focus:text-red-600">Delete <ContextMenuShortcut>Del</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuSeparator />
                        <ContextMenuItem onClick={handleUndo} disabled={!canUndo}>Undo <ContextMenuShortcut>Ctrl+Z</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuItem onClick={handleRedo} disabled={!canRedo}>Redo <ContextMenuShortcut>Ctrl+Y</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuSeparator />
                        {!contextTarget && <ContextMenuItem onClick={() => handleNewAIFolder()}>New AIFolder…</ContextMenuItem>}
                        <ContextMenuItem onClick={handleRefresh}>Refresh <ContextMenuShortcut>F5</ContextMenuShortcut></ContextMenuItem>
                        <ContextMenuItem onClick={() => handleSelectAll(true)}>Select All <ContextMenuShortcut>Ctrl+A</ContextMenuShortcut></ContextMenuItem>
                      </ContextMenuContent>
                    </ContextMenu>
                    {/* Status bar */}
                    <div className="shrink-0 bg-gray-50 border-t border-gray-200 px-3 py-1 flex items-center gap-3">
                      <button onClick={handleUndo} disabled={!canUndo || opLoading} title="Undo (Ctrl+Z)" className="p-1 rounded hover:bg-gray-200 disabled:opacity-30 disabled:cursor-not-allowed">
                        <Undo2 className="w-3.5 h-3.5 text-gray-600" />
                      </button>
                      <button onClick={handleRedo} disabled={!canRedo || opLoading} title="Redo (Ctrl+Y)" className="p-1 rounded hover:bg-gray-200 disabled:opacity-30 disabled:cursor-not-allowed">
                        <Redo2 className="w-3.5 h-3.5 text-gray-600" />
                      </button>
                      <span className={`text-xs truncate flex-1 ${tempStatus ? 'text-blue-700 font-medium' : 'text-gray-600'}`}>{statusText}</span>
                      {selectedPaths.size > 0 && <span className="text-xs text-blue-600 shrink-0">{selectedPaths.size} selected</span>}
                      {clipboard && <span className="text-xs text-amber-600 shrink-0">{clipboard.paths.length} {clipboard.move ? 'cut' : 'copied'}</span>}
                    </div>
                  </div>
                </Panel>
              </PanelGroup>
            </Panel>
            <PanelResizeHandle className="w-px bg-gray-200 hover:bg-blue-400 transition-colors" />
            <Panel defaultSize={35} minSize={20}>
              <PanelGroup direction="vertical">
                <Panel defaultSize={50} minSize={25}>
                  <div className="h-full bg-white flex flex-col border-l border-gray-200">
                    <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 shrink-0">
                      <h3 className="text-sm font-semibold text-gray-900">File Preview</h3>
                    </div>
                    <div className="flex-1 overflow-hidden"><PreviewPanel path={focusedPath} /></div>
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
                        currentPath={activeTab.path}
                        selectedPaths={activeTab.selectedPaths}
                        focusedPath={activeTab.focusedPath}
                        searchQuery={activeTab.isSearchMode && activeTab.searchMeta ? activeTab.searchMeta.query : ''}
                        onRefresh={handleRefresh}
                        configVersion={configVersion}
                      />
                    </div>
                  </div>
                </Panel>
              </PanelGroup>
            </Panel>
          </PanelGroup>
        </div>
      </div>
      )}

      <DeleteConfirmDialog open={delConfirmOpen} paths={pathsToDelete} onConfirm={handleDeleteConfirmed} onCancel={() => setDelConfirmOpen(false)} />
      <NewAIFolderDialog open={aiFolderOpen} defaultParent={aiFolderParent} onConfirm={handleCreateAIFolder} onCancel={() => setAiFolderOpen(false)} />
      <LLMSettingsDialog open={llmSettingsOpen} onClose={() => { setLlmSettingsOpen(false); setConfigVersion(v => v + 1); }} onConfigChanged={() => setConfigVersion(v => v + 1)} />
      <AppFilesDialog open={appFilesDialogOpen} appName={appFilesDialogName} onClose={() => setAppFilesDialogOpen(false)} />
    </div>
  );
}
