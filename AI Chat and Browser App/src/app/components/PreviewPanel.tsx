/**
 * PreviewPanel — metadata + content preview for the focused file or folder.
 *
 * Folder: shows stat metadata + analysis record (if available).
 * Text file: shows stat metadata + first 64 KB of content.
 * Image file: shows stat metadata + inline image.
 * Binary: shows stat metadata only.
 */
import { useState, useEffect } from 'react';
import { Loader2, FileText } from 'lucide-react';
import { statPath, previewFile, imageUrl } from '../../api/client';
import type { StatResponse, PreviewResponse } from '../../api/types';

// ── Tiny helper ───────────────────────────────────────────────────────────────

function Row({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <tr className="border-b border-gray-100">
      <td className="py-1.5 pr-3 text-gray-500 font-medium whitespace-nowrap align-top text-xs w-24">{label}</td>
      <td className="py-1.5 text-gray-900 break-all text-xs" title={String(value ?? '')}>
        {value ?? '—'}
      </td>
    </tr>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface PreviewPanelProps {
  path: string | null;
}

export function PreviewPanel({ path }: PreviewPanelProps) {
  const [stat, setStat]       = useState<StatResponse | null>(null);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState<string | null>(null);

  useEffect(() => {
    if (!path) { setStat(null); setPreview(null); return; }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setStat(null);
    setPreview(null);

    statPath(path)
      .then(async (s) => {
        if (cancelled) return;
        setStat(s);
        if (s.is_file) {
          try {
            const p = await previewFile(path);
            if (!cancelled) setPreview(p);
          } catch { /* non-critical */ }
        }
      })
      .catch((err: Error) => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; };
  }, [path]);

  // ── Empty / loading / error states ──────────────────────────────────────────
  if (!path) return <p className="text-xs text-gray-400 p-4">Select a file to preview</p>;
  if (loading) return (
    <div className="flex items-center gap-2 p-4 text-gray-400">
      <Loader2 className="w-4 h-4 animate-spin" />
      <span className="text-sm">Loading…</span>
    </div>
  );
  if (error || !stat) return <p className="text-xs text-red-500 p-4">{error ?? 'Failed to load'}</p>;

  // ── Metadata table ───────────────────────────────────────────────────────────
  return (
    <div className="h-full overflow-auto p-3 flex flex-col gap-3">
      <table className="w-full">
        <tbody>
          <Row label="Name"     value={stat.name} />
          <Row label="Path"     value={stat.path} />
          <Row label="Type"     value={stat.type} />
          {stat.is_file && <Row label="Size" value={stat.size || '—'} />}
          <Row label="Modified" value={stat.modified} />
        </tbody>
      </table>

      {/* Folder analysis */}
      {stat.is_dir && stat.analysis && (
        <>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider border-t border-gray-200 pt-2">
            Analysis
          </p>
          <table className="w-full">
            <tbody>
              <Row label="Total size"  value={stat.analysis.size} />
              <Row label="Files"       value={stat.analysis.file_count} />
              <Row label="Subfolders"  value={stat.analysis.folder_count} />
              <Row label="Errors"      value={stat.analysis.error_count} />
              <Row label="Analysed at" value={stat.analysis.analysed_at} />
            </tbody>
          </table>
        </>
      )}

      {/* Text content */}
      {preview?.kind === 'text' && (
        <>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider border-t border-gray-200 pt-2 flex items-center gap-1">
            <FileText className="w-3 h-3" /> Content
            {preview.truncated && <span className="ml-auto text-amber-500 normal-case font-normal">truncated at 64 KB</span>}
          </p>
          <pre className="text-xs text-gray-800 bg-gray-50 rounded p-2 overflow-auto max-h-64 whitespace-pre-wrap break-all font-mono">
            {preview.content}
          </pre>
        </>
      )}

      {/* Image preview */}
      {preview?.kind === 'image' && (
        <>
          <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider border-t border-gray-200 pt-2">
            Preview
          </p>
          <img
            src={imageUrl(path)}
            alt={stat.name}
            className="max-w-full max-h-48 object-contain rounded border border-gray-200 bg-gray-50"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
          />
        </>
      )}
    </div>
  );
}
