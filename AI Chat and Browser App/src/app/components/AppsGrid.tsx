/**
 * AppsGrid — installed-applications icon grid.
 *
 * Fetches /api/apps (non-system), renders a filterable icon grid.  Each tile
 * has a right-click menu (Open in AIFM / Open in Explorer / Delete) and
 * double-click launches the application.
 */
import { useState, useEffect, useMemo, useCallback } from 'react';
import { Loader2, Package, RefreshCw, Eye } from 'lucide-react';
import { API_BASE } from '../../api/client';
import {
  ContextMenu, ContextMenuTrigger, ContextMenuContent,
  ContextMenuItem, ContextMenuSeparator,
} from './ui/context-menu';

// ── Types ─────────────────────────────────────────────────────────────────────

interface AppItem {
  name: string
  publisher: string
  version: string
  install_location: string
  uninstall_string: string
  icon_path: string | null
  exists: boolean
}

// ── Image with placeholder fallback ───────────────────────────────────────────

function AppIcon({ iconPath, name }: { iconPath: string | null; name: string }) {
  const [err, setErr] = useState(false);
  const src = iconPath && !err
    ? `${API_BASE}/api/apps/icon?path=${encodeURIComponent(iconPath)}`
    : '';

  if (!src) {
    return (
      <div className="w-12 h-12 rounded-lg bg-gray-100 flex items-center justify-center shrink-0">
        <Package className="w-6 h-6 text-gray-300" />
      </div>
    );
  }
  return (
    <img src={src} alt={name} className="w-12 h-12 object-contain shrink-0"
      onError={() => setErr(true)} />
  );
}

// ── App tile ──────────────────────────────────────────────────────────────────

function AppTile({
  app, onOpenInAIFM, onOpenInExplorer, onLaunch, onDelete, onViewFiles,
}: {
  app: AppItem
  onOpenInAIFM: (app: AppItem) => void
  onOpenInExplorer: (app: AppItem) => void
  onLaunch: (app: AppItem) => void
  onDelete: (app: AppItem) => void
  onViewFiles: (app: AppItem) => void
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          title={[
            app.name,
            app.version ? `v${app.version}` : '',
            app.install_location || '',
          ].filter(Boolean).join('\n')}
          className="flex flex-col items-center gap-1 p-3 rounded-lg border border-gray-100 hover:border-blue-300 hover:bg-blue-50/30 cursor-pointer transition-colors group"
          onDoubleClick={() => onLaunch(app)}
        >
          <AppIcon iconPath={app.icon_path} name={app.name} />
          <span className="text-[11px] text-gray-700 text-center leading-tight line-clamp-2 break-words max-w-full">
            {app.name}
          </span>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="w-48">
        <ContextMenuItem onClick={() => onLaunch(app)}>
          Launch
        </ContextMenuItem>
        {app.install_location && (
          <ContextMenuItem onClick={() => onOpenInAIFM(app)}>
            Open in AIFM
          </ContextMenuItem>
        )}
        {app.install_location && (
          <ContextMenuItem onClick={() => onOpenInExplorer(app)}>
            Open in Explorer
          </ContextMenuItem>
        )}
        <ContextMenuItem onClick={() => onViewFiles(app)}>
          <Eye className="w-3.5 h-3.5 mr-1.5" />
          View files
        </ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem onClick={() => onDelete(app)} className="text-red-600 focus:text-red-600">
          Delete from list
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface AppsGridProps {
  onOpenInAIFM: (installDir: string) => void;
  onOpenInExplorer: (installDir: string) => void;
  onViewFiles: (appName: string) => void;
}

export function AppsGrid({ onOpenInAIFM, onOpenInExplorer, onViewFiles }: AppsGridProps) {
  const [apps,     setApps]     = useState<AppItem[]>([]);
  const [hidden,   setHidden]   = useState<Set<string>>(new Set());
  const [filter,   setFilter]   = useState('');
  const [loading,  setLoading]  = useState(true);
  const [error,    setError]    = useState('');
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildStats, setRebuildStats] = useState<{scanned: number; matched: number; apps_with_files: number} | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`${API_BASE}/api/apps?filter_system=true`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((data: AppItem[]) => { if (!cancelled) setApps(data); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return apps.filter(a => !hidden.has(a.name) && (!q || a.name.toLowerCase().includes(q)));
  }, [apps, filter, hidden]);

  const handleLaunch = useCallback(async (app: AppItem) => {
    try {
      const r = await fetch(`${API_BASE}/api/apps/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: app.name }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        console.error('Launch failed:', d.detail ?? r.statusText);
      }
    } catch (e) {
      console.error('Launch error:', e);
    }
  }, []);

  const handleDelete = useCallback((app: AppItem) => {
    setHidden(prev => new Set(prev).add(app.name));
  }, []);

  const handleRebuild = useCallback(async () => {
    setRebuilding(true);
    setRebuildStats(null);
    try {
      const r = await fetch(`${API_BASE}/api/app-files/rebuild`, { method: 'POST' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const stats = await r.json();
      setRebuildStats(stats);
    } catch (e) {
      console.error('Rebuild failed:', e);
    } finally {
      setRebuilding(false);
    }
  }, []);

  if (loading) {
    return (
      <div className="flex items-center gap-2 p-8 text-gray-400">
        <Loader2 className="w-5 h-5 animate-spin" />
        <span className="text-sm">Scanning installed apps…</span>
      </div>
    );
  }

  if (error) {
    return <div className="p-8 text-sm text-red-500">Failed to load apps: {error}</div>;
  }

  return (
    <div className="h-full flex flex-col p-4 gap-3">
      {/* Header + filter */}
      <div className="flex items-center gap-3 shrink-0">
        <h2 className="text-base font-semibold text-gray-900">Installed Applications</h2>
        <button
          onClick={handleRebuild}
          disabled={rebuilding}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 disabled:opacity-50 transition-colors"
        >
          {rebuilding ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <RefreshCw className="w-3.5 h-3.5" />
          )}
          Rebuild index
        </button>
        {rebuildStats && (
          <span className="text-xs text-green-600">
            scanned {rebuildStats.scanned} / matched {rebuildStats.matched} / {rebuildStats.apps_with_files} apps
          </span>
        )}
        <span className="text-xs text-gray-400 ml-auto">
          {visible.length} / {apps.length}
        </span>
        {hidden.size > 0 && (
          <button
            onClick={() => setHidden(new Set())}
            className="text-xs text-blue-500 hover:text-blue-700"
          >
            Show all
          </button>
        )}
      </div>
      <input
        type="text"
        value={filter}
        onChange={e => setFilter(e.target.value)}
        placeholder="Filter by name…"
        className="w-full max-w-sm px-3 py-1.5 text-sm border border-gray-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-500"
      />

      {/* Grid */}
      <div className="flex-1 overflow-y-auto">
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))' }}>
          {visible.map(app => (
            <AppTile
              key={app.name}
              app={app}
              onLaunch={handleLaunch}
              onOpenInAIFM={a => { if (a.install_location) onOpenInAIFM(a.install_location); }}
              onOpenInExplorer={a => { if (a.install_location) onOpenInExplorer(a.install_location); }}
              onDelete={handleDelete}
              onViewFiles={a => onViewFiles(a.name)}
            />
          ))}
        </div>
        {visible.length === 0 && (
          <p className="text-center text-sm text-gray-400 mt-8">
            {apps.length === 0 ? 'No installed apps found.' : 'No apps match the filter.'}
          </p>
        )}
      </div>
    </div>
  );
}
