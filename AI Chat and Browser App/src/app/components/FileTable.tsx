/**
 * FileTable — virtualised file/folder listing.
 *
 * Uses @tanstack/react-virtual to render only the visible rows so that
 * directories with thousands of entries (e.g. C:\Windows\System32) remain
 * smooth.  Sorting is done client-side; column widths are resizable by
 * dragging the divider between column headers.
 *
 * Click behaviour (file-manager style):
 *   plain click — select only that row, deselect others, show preview
 *   Ctrl+click  — toggle selection of that row, keep other selections
 *   Shift+click — range-select from the last-clicked row to this one
 *   checkbox click — just toggle that row
 *
 * In search mode the table shows two columns (Name, Path) instead of four.
 */
import {
  useRef, useMemo, useCallback, useState, useEffect,
  type MouseEvent as RMouseEvent,
} from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import {
  FolderOpen, File, FileText, FileArchive, FileAudio, FileVideo,
  Image as ImageIcon, Code,
  ChevronUp, ChevronDown, GripVertical,
} from 'lucide-react';
import { API_BASE } from '../../api/client';
import type { FileEntry, SortCol, SortDir } from '../../api/types';

// ── Extension → icon category (fallback when system icon fails) ────────────

const IMAGE_EXTS = new Set([
  'jpg','jpeg','png','gif','bmp','svg','webp','ico','tiff','tif','heic','heif',
  'raw','psd','ai','eps','xcf','pcx','tga','dds','exr','hdr','jp2','j2k',
]);
const CODE_EXTS = new Set([
  'js','mjs','cjs','ts','tsx','jsx','py','pyw','go','rs','cpp','cc','cxx','c','h','hpp',
  'cs','java','rb','php','swift','kt','kts','sh','bash','zsh','ps1','bat','cmd',
  'html','htm','css','scss','sass','less','vue','svelte',
  'sql','r','lua','scala','dart','elm','ex','exs','erl','hrl',
  'pl','pm','groovy','gradle','clj','cljs','cljc','edn','fs','fsx','fsi',
  'ml','mli','nim','cr','v','zig','jl','rkt','ss','scm','lisp',
  'asm','s','S','wasm','wat','json','jsonl','json5','xml','xsl','xslt',
  'yaml','yml','toml','ini','cfg','conf','env','properties',
  'proto','graphql','gql','prisma','cmake','makefile','dockerfile',
]);
const DOC_EXTS = new Set([
  'txt','md','markdown','rst','tex','ltx','latex','bib',
  'pdf','doc','docx','rtf','odt','pages','wpd','wps',
  'xls','xlsx','xlsm','csv','tsv','ods','numbers',
  'ppt','pptx','pptm','odp','key',
  'log','readme','license','changelog',
]);
const ARCHIVE_EXTS = new Set([
  'zip','rar','7z','tar','gz','tgz','bz2','tbz2','xz','txz','lz','lz4',
  'zst','cab','iso','dmg','pkg','deb','rpm','apk','jar','war','ear',
]);
const AUDIO_EXTS = new Set([
  'mp3','wav','flac','aac','ogg','oga','wma','m4a','aiff','aif',
  'opus','alac','ape','mid','midi','mod','xm','it','s3m',
]);
const VIDEO_EXTS = new Set([
  'mp4','avi','mkv','mov','wmv','flv','webm','m4v','mpg','mpeg',
  '3gp','3g2','ogv','ts','m2ts','vob','divx','xvid',
]);

function FileIcon({ entry }: { entry: FileEntry }) {
  const [failed, setFailed] = useState(false);

  if (entry.is_dir) return <FolderOpen className="w-4 h-4 shrink-0 text-yellow-600" />;

  const ext = entry.name.split('.').pop()?.toLowerCase() ?? '';

  // Image & code extensions — use lucide icons directly, no API call.
  if (IMAGE_EXTS.has(ext)) return <ImageIcon className="w-4 h-4 shrink-0 text-purple-500" />;
  if (CODE_EXTS.has(ext))  return <Code       className="w-4 h-4 shrink-0 text-blue-500"   />;

  // Everything else — real system icon, lucide fallback on error.
  if (failed) {
    if (DOC_EXTS.has(ext))     return <FileText    className="w-4 h-4 shrink-0 text-gray-600"  />;
    if (ARCHIVE_EXTS.has(ext)) return <FileArchive className="w-4 h-4 shrink-0 text-amber-600" />;
    if (AUDIO_EXTS.has(ext))   return <FileAudio   className="w-4 h-4 shrink-0 text-pink-500"  />;
    if (VIDEO_EXTS.has(ext))   return <FileVideo   className="w-4 h-4 shrink-0 text-red-500"   />;
    return                            <File        className="w-4 h-4 shrink-0 text-gray-400"  />;
  }

  const src = `${API_BASE}/api/fs/icon?path=${encodeURIComponent(entry.path)}&size=16`;
  return <img src={src} alt="" className="w-4 h-4 shrink-0 object-contain" onError={() => setFailed(true)} />;
}

function sortEntries(entries: FileEntry[], col: SortCol, dir: SortDir): FileEntry[] {
  return [...entries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let cmp = 0;
    switch (col) {
      case 'name':     cmp = a.name.localeCompare(b.name, undefined, { sensitivity:'base', numeric:true }); break;
      case 'size':     cmp = (a.size_bytes ?? -1) - (b.size_bytes ?? -1); break;
      case 'type':     cmp = a.type.localeCompare(b.type, undefined, { sensitivity:'base' }); break;
      case 'modified': cmp = (a.modified_ts ?? 0) - (b.modified_ts ?? 0); break;
    }
    return dir === 'asc' ? cmp : -cmp;
  });
}

export interface SearchEntry { path: string; name: string; parentDir: string }

export function toSearchEntries(paths: string[]): SearchEntry[] {
  return paths.map(p => {
    const sep = p.includes('\\') ? '\\' : '/';
    const idx = p.lastIndexOf(sep);
    return { path: p, name: idx >= 0 ? p.slice(idx + 1) : p, parentDir: idx >= 0 ? p.slice(0, idx) : '' };
  });
}

// ── Resize handle (absolutely positioned inside a <th>) ─────────────────────

function ResizeHandle({ onMouseDown }: { onMouseDown: (e: RMouseEvent) => void }) {
  return (
    <div
      className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize hover:bg-blue-400 transition-colors group"
      onMouseDown={onMouseDown}
    >
      <GripVertical className="w-3 h-3 text-gray-300 group-hover:text-blue-500 absolute top-1/2 -translate-y-1/2 left-1/2 -translate-x-1/2" />
    </div>
  );
}

// ── Sort header with resize ───────────────────────────────────────────────────

interface ColHeaderProps {
  label: string; col: SortCol;
  sortCol: SortCol; sortDir: SortDir;
  onClick: (c: SortCol) => void;
  width: number;
  /** Called with the absolute target width (pixels) while dragging. */
  onResize: (targetWidth: number) => void;
}

function ColHeader({ label, col, sortCol, sortDir, onClick, width, onResize }: ColHeaderProps) {
  const startX = useRef(0);
  const startW = useRef(0);

  const onMDown = useCallback((e: RMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    startX.current = e.clientX;
    startW.current = width;
    const onMove = (ev: globalThis.MouseEvent) => {
      onResize(startW.current + ev.clientX - startX.current);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [width, onResize]);

  return (
    <th
      className="px-3 py-2 text-left font-medium text-gray-700 cursor-pointer hover:bg-gray-100 select-none relative"
      style={{ width, minWidth: 40 }}
      onClick={() => onClick(col)}
    >
      <div className="flex items-center gap-1">
        {label}
        {sortCol === col && (sortDir === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />)}
      </div>
      <ResizeHandle onMouseDown={onMDown} />
    </th>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

const ROW_HEIGHT = 28;  // px

interface FileTableProps {
  entries: FileEntry[];
  isSearchMode: boolean;
  searchEntries: SearchEntry[];
  sortCol: SortCol; sortDir: SortDir;
  selectedPaths: Set<string>;
  focusedPath: string | null;
  onSort: (col: SortCol) => void;
  onSelect: (path: string, checked: boolean) => void;
  /** Select only `path` — deselect everything else. */
  onSelectOnly: (path: string) => void;
  /** Range-select between `from` and `to` (both included). */
  onSelectRange: (fromPath: string, toPath: string) => void;
  onSelectAll: (checked: boolean) => void;
  onFocus: (path: string) => void;
  onNavigate: (path: string) => void;
}

export function FileTable({
  entries, isSearchMode, searchEntries,
  sortCol, sortDir, selectedPaths, focusedPath,
  onSort, onSelect, onSelectOnly, onSelectRange,
  onSelectAll, onFocus, onNavigate,
}: FileTableProps) {

  const scrollRef = useRef<HTMLDivElement>(null);
  const lastClickedRef = useRef<number | null>(null);

  const [colName, setColName] = useState(180);
  const [colSize, setColSize] = useState(80);
  const [colType, setColType] = useState(110);
  const [colMod,  setColMod]  = useState(145);

  const sortedEntries = useMemo(() => sortEntries(entries, sortCol, sortDir), [entries, sortCol, sortDir]);
  const rowCount = isSearchMode ? searchEntries.length : sortedEntries.length;

  const virtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });
  const virt = virtualizer.getVirtualItems();
  const padTop = virt.length > 0 ? virt[0].start : 0;
  const padBot = virt.length > 0 ? virtualizer.getTotalSize() - virt[virt.length - 1].end : 0;

  const allSelected  = rowCount > 0 && selectedPaths.size === rowCount;
  const someSelected = selectedPaths.size > 0 && !allSelected;

  // Reset last-clicked when the visible entries change (new folder / new search).
  useEffect(() => { lastClickedRef.current = null; }, [entries, searchEntries]);

  // ── Row click with Ctrl / Shift ──────────────────────────────────────────

  const handleRowClick = useCallback(
    (e: RMouseEvent, path: string, index: number) => {
      if (e.ctrlKey || e.metaKey) {
        // Ctrl+Click — toggle this item, keep others.
        onSelect(path, !selectedPaths.has(path));
        onFocus(path);
        lastClickedRef.current = index;
      } else if (e.shiftKey && lastClickedRef.current != null) {
        // Shift+Click — range from last-clicked to this row.
        const list = isSearchMode
          ? searchEntries.map(x => x.path)
          : sortedEntries.map(x => x.path);
        const from = Math.min(lastClickedRef.current, index);
        const to   = Math.max(lastClickedRef.current, index);
        const startPath = list[from];
        const endPath   = list[to];
        if (startPath && endPath) onSelectRange(startPath, endPath);
        onFocus(path);
      } else {
        // Plain click — select only this row.
        onSelectOnly(path);
        onFocus(path);
        lastClickedRef.current = index;
      }
    },
    [isSearchMode, searchEntries, sortedEntries, selectedPaths, onSelect, onSelectOnly, onSelectRange, onFocus],
  );

  // ── Search mode ────────────────────────────────────────────────────────────

  if (isSearchMode) {
    return (
      <div ref={scrollRef} className="h-full overflow-auto">
        <table className="w-full text-sm border-collapse" style={{ tableLayout: 'fixed' }}>
          <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
            <tr>
              <th className="px-3 py-2" style={{ width: 32 }}>
                <input type="checkbox" checked={allSelected}
                  ref={el => { if (el) el.indeterminate = someSelected; }}
                  onChange={e => onSelectAll(e.target.checked)} className="cursor-pointer" />
              </th>
              <th className="px-3 py-2 text-left font-medium text-gray-700" style={{ width: 220 }}>Name</th>
              <th className="px-3 py-2 text-left font-medium text-gray-700">Path</th>
            </tr>
          </thead>
          <tbody>
            {padTop > 0 && <tr><td colSpan={3} style={{ height: padTop }} /></tr>}
            {virt.map(vRow => {
              const se = searchEntries[vRow.index];
              const sel = selectedPaths.has(se.path);
              const fcs = focusedPath === se.path;
              return (
                <tr key={se.path} data-path={se.path} style={{ height: ROW_HEIGHT }}
                  className={[
                    'border-b border-gray-100 cursor-pointer',
                    sel ? 'bg-[#e8f1ff]' : vRow.index % 2 === 0 ? 'bg-white hover:bg-gray-50' : 'bg-gray-50/50 hover:bg-gray-50',
                  ].join(' ')}
                  onClick={e => { onSelectOnly(se.path); onFocus(se.path); }}
                  onDoubleClick={() => {
                    const sep = se.path.includes('\\') ? '\\' : '/';
                    const idx = se.path.lastIndexOf(sep);
                    onNavigate(idx > 0 ? se.path.slice(0, idx) : sep);
                  }}
                >
                  <td className="px-3">
                    <input type="checkbox" checked={sel}
                      onChange={ev => onSelect(se.path, ev.target.checked)}
                      onClick={ev => ev.stopPropagation()} className="cursor-pointer" />
                  </td>
                  <td className="px-3 truncate">
                    <div className="flex items-center gap-2">
                      <FileIcon entry={{ path: se.path, is_dir: false, is_file: true, name: se.name } as FileEntry} />
                      <span className="truncate text-gray-900">{se.name}</span>
                    </div>
                  </td>
                  <td className="px-3 text-gray-500 truncate text-xs">{se.parentDir}</td>
                </tr>
              );
            })}
            {padBot > 0 && <tr><td colSpan={3} style={{ height: padBot }} /></tr>}
          </tbody>
        </table>
      </div>
    );
  }

  // ── Folder browse mode ─────────────────────────────────────────────────────

  return (
    <div ref={scrollRef} className="h-full overflow-auto">
      <table className="w-full text-sm border-collapse" style={{ tableLayout: 'fixed' }}>
        <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
          <tr>
            <th className="px-3 py-2" style={{ width: 32 }}>
              <input type="checkbox" checked={allSelected}
                ref={el => { if (el) el.indeterminate = someSelected; }}
                onChange={e => onSelectAll(e.target.checked)} className="cursor-pointer" />
            </th>
            <ColHeader label="Name"          col="name"     sortCol={sortCol} sortDir={sortDir} onClick={onSort} width={colName} onResize={w => setColName(Math.max(40, w))} />
            <ColHeader label="Size"          col="size"     sortCol={sortCol} sortDir={sortDir} onClick={onSort} width={colSize} onResize={w => setColSize(Math.max(40, w))} />
            <ColHeader label="Type"          col="type"     sortCol={sortCol} sortDir={sortDir} onClick={onSort} width={colType} onResize={w => setColType(Math.max(40, w))} />
            <ColHeader label="Date Modified" col="modified" sortCol={sortCol} sortDir={sortDir} onClick={onSort} width={colMod}  onResize={w => setColMod(Math.max(40, w))}  />
          </tr>
        </thead>
        <tbody>
          {padTop > 0 && <tr><td colSpan={5} style={{ height: padTop }} /></tr>}
          {virt.map(vRow => {
            const entry = sortedEntries[vRow.index];
            const sel = selectedPaths.has(entry.path);
            return (
              <tr key={entry.path} data-path={entry.path} style={{ height: ROW_HEIGHT }}
                className={[
                  'border-b border-gray-100 cursor-pointer',
                  sel ? 'bg-[#e8f1ff]' : vRow.index % 2 === 0 ? 'bg-white hover:bg-gray-50' : 'bg-gray-50/50 hover:bg-gray-50',
                ].join(' ')}
                onClick={e => handleRowClick(e, entry.path, vRow.index)}
                onDoubleClick={() => { if (entry.is_dir) onNavigate(entry.path); }}
              >
                <td className="px-3">
                  <input type="checkbox" checked={sel}
                    onChange={ev => onSelect(entry.path, ev.target.checked)}
                    onClick={ev => ev.stopPropagation()} className="cursor-pointer" />
                </td>
                <td className="px-3" style={{ width: colName }}>
                  <div className="flex items-center gap-2 overflow-hidden">
                    <FileIcon entry={entry} />
                    <span className="truncate text-gray-900">{entry.name}</span>
                  </div>
                </td>
                <td className="px-3 text-gray-600 truncate" style={{ width: colSize }}>{entry.size || '--'}</td>
                <td className="px-3 text-gray-600 truncate" style={{ width: colType }}>{entry.type}</td>
                <td className="px-3 text-gray-600 truncate" style={{ width: colMod }}>{entry.modified}</td>
              </tr>
            );
          })}
          {padBot > 0 && <tr><td colSpan={5} style={{ height: padBot }} /></tr>}
        </tbody>
      </table>
    </div>
  );
}
