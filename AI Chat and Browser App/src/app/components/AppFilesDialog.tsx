/**
 * AppFilesDialog — shows indexed files for one installed application.
 *
 * Fetches GET /api/app-files/by-app?name=… on open.  Displays file_name
 * with a secondary file_path line and a tooltip on hover.
 */
import { useState, useEffect, useRef } from 'react';
import { Loader2, File, X } from 'lucide-react';
import { API_BASE } from '../../api/client';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from './ui/dialog';

// ── Types ───────────────────────────────────────────────────────────────────────

interface AppFileEntry {
  file_path: string;
  file_name: string;
  suffix: string;
}

interface Props {
  open: boolean;
  appName: string;
  onClose: () => void;
}

// ── Component ───────────────────────────────────────────────────────────────────

export function AppFilesDialog({ open, appName, onClose }: Props) {
  const [files,    setFiles]    = useState<AppFileEntry[]>([]);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState('');

  // Re-fetch whenever the dialog opens with a new app name.
  useEffect(() => {
    if (!open || !appName) return;

    let cancelled = false;
    setLoading(true);
    setError('');
    setFiles([]);

    fetch(`${API_BASE}/api/app-files/by-app?name=${encodeURIComponent(appName)}`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: AppFileEntry[]) => {
        if (!cancelled) setFiles(data);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
  }, [open, appName]);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-lg max-h-[70vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="truncate pr-6">{appName}</DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto min-h-0">
          {loading ? (
            <div className="flex items-center gap-2 py-8 justify-center text-gray-400">
              <Loader2 className="w-5 h-5 animate-spin" />
              <span className="text-sm">Loading files…</span>
            </div>
          ) : error ? (
            <p className="py-8 text-center text-sm text-red-500">{error}</p>
          ) : files.length === 0 ? (
            <p className="py-8 text-center text-sm text-gray-400">
              该应用暂无已索引的文件，请先重建索引。
            </p>
          ) : (
            <ul className="divide-y divide-gray-100">
              {files.map((f, i) => (
                <li
                  key={i}
                  title={f.file_path}
                  className="px-1 py-2 hover:bg-gray-50 rounded transition-colors"
                >
                  <div className="flex items-start gap-2">
                    <File className="w-4 h-4 text-gray-300 shrink-0 mt-0.5" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-gray-800 truncate">{f.file_name}</p>
                      <p className="text-xs text-gray-400 truncate">{f.file_path}</p>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Footer with count */}
        {!loading && !error && (
          <div className="text-xs text-gray-400 pt-2 border-t border-gray-100 shrink-0">
            {files.length} file{files.length !== 1 ? 's' : ''}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
