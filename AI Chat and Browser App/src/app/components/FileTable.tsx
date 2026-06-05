/**
 * FileTable — virtualised file/folder listing.
 *
 * Uses @tanstack/react-virtual to render only the visible rows so that
 * directories with thousands of entries (e.g. C:\Windows\System32) remain
 * smooth.  Sorting is done client-side so there is no extra network round-
 * trip when the user clicks a column header.
 *
 * In search mode the table shows two columns (Name, Path) instead of four.
 */
import { useRef, useMemo, useCallback } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import {
  FolderOpen,
  File,
  FileText,
  Image as ImageIcon,
  Code,
  ChevronUp,
  ChevronDown,
} from 'lucide-react';
import type { FileEntry, SortCol, SortDir } from '../../api/types';

// ── helpers ──────────────────────────────────────────────────────────────────

const IMAGE_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg', 'webp', 'ico', 'tiff']);
const CODE_EXTS = new Set([
  'js', 'ts', 'tsx', 'jsx', 'py', 'go', 'rs', 'cpp', 'c', 'cs', 'java',
  'rb', 'php', 'swift', 'kt', 'sh', 'ps1', 'bat', 'cmd',
]);
const TEXT_EXTS = new Set(['txt', 'md', 'pdf', 'doc', 'docx', 'rtf', 'csv', 'log']);

function FileIcon({ entry }: { entry: FileEntry }) {
  if (entry.is_dir) return <FolderOpen className="w-4 h-4 shrink-0 text-yellow-600" />;
  const ext = entry.name.split('.').pop()?.toLowerCase() ?? '';
  if (IMAGE_EXTS.has(ext)) return <ImageIcon className="w-4 h-4 shrink-0 text-purple-500" />;
  if (CODE_EXTS.has(ext))  return <Code       className="w-4 h-4 shrink-0 text-blue-500"   />;
  if (TEXT_EXTS.has(ext))  return <FileText   className="w-4 h-4 shrink-0 text-gray-600"   />;
  return                          <File       className="w-4 h-4 shrink-0 text-gray-400"   />;
}

/** Client-side sort: folders always before files, then by chosen column. */
function sortEntries(
  entries: FileEntry[],
  col: SortCol,
  dir: SortDir,
): FileEntry[] {
  return [...entries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let cmp = 0;
    switch (col) {
      case 'name':
        cmp = a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true });
        break;
      case 'size':
        cmp = (a.size_bytes ?? -1) - (b.size_bytes ?? -1);
        break;
      case 'type':
        cmp = a.type.localeCompare(b.type, undefined, { sensitivity: 'base' });
        break;
      case 'modified':
        cmp = (a.modified_ts ?? 0) - (b.modified_ts ?? 0);
        break;
    }
    return dir === 'asc' ? cmp : -cmp;
  });
}

/** Search-mode display entry (path strings from /api/search). */
export interface SearchEntry {
  path: string;
  name: string;
  parentDir: string;
}

export function toSearchEntries(paths: string[]): SearchEntry[] {
  return paths.map((p) => {
    const sep = p.includes('\\') ? '\\' : '/';
    const idx = p.lastIndexOf(sep);
    return {
      path: p,
      name: idx >= 0 ? p.slice(idx + 1) : p,
      parentDir: idx >= 0 ? p.slice(0, idx) : '',
    };
  });
}

// ── Column header ─────────────────────────────────────────────────────────────

interface ColHeaderProps {
  label: string;
  col: SortCol;
  sortCol: SortCol;
  sortDir: SortDir;
  onClick: (c: SortCol) => void;
  className?: string;
}

function ColHeader({ label, col, sortCol, sortDir, onClick, className }: ColHeaderProps) {
  return (
    <th
      className={`px-3 py-2 text-left font-medium text-gray-700 cursor-pointer hover:bg-gray-100 select-none ${className ?? ''}`}
      onClick={() => onClick(col)}
    >
      <div className="flex items-center gap-1">
        {label}
        {sortCol === col &&
          (sortDir === 'asc' ? (
            <ChevronUp className="w-3 h-3" />
          ) : (
            <ChevronDown className="w-3 h-3" />
          ))}
      </div>
    </th>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

const ROW_HEIGHT = 28; // px

interface FileTableProps {
  /** Normal folder listing — pass entries from /api/fs/list */
  entries: FileEntry[];
  /** True when showing Everything search results */
  isSearchMode: boolean;
  /** Search result entries (used when isSearchMode=true) */
  searchEntries: SearchEntry[];
  sortCol: SortCol;
  sortDir: SortDir;
  selectedPaths: Set<string>;
  focusedPath: string | null;
  onSort: (col: SortCol) => void;
  onSelect: (path: string, checked: boolean) => void;
  onSelectAll: (checked: boolean) => void;
  onFocus: (path: string) => void;
  onNavigate: (path: string) => void;
}

export function FileTable({
  entries,
  isSearchMode,
  searchEntries,
  sortCol,
  sortDir,
  selectedPaths,
  focusedPath,
  onSort,
  onSelect,
  onSelectAll,
  onFocus,
  onNavigate,
}: FileTableProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const sortedEntries = useMemo(
    () => sortEntries(entries, sortCol, sortDir),
    [entries, sortCol, sortDir],
  );

  const rowCount = isSearchMode ? searchEntries.length : sortedEntries.length;

  const virtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  const virtualItems = virtualizer.getVirtualItems();
  const paddingTop =
    virtualItems.length > 0 ? virtualItems[0].start : 0;
  const paddingBottom =
    virtualItems.length > 0
      ? virtualizer.getTotalSize() - virtualItems[virtualItems.length - 1].end
      : 0;

  const allSelected =
    rowCount > 0 && selectedPaths.size === rowCount;
  const someSelected = selectedPaths.size > 0 && !allSelected;

  const handleRowDoubleClick = useCallback(
    (entry: FileEntry) => {
      if (entry.is_dir) onNavigate(entry.path);
    },
    [onNavigate],
  );

  /** Navigate to the parent folder of a search result on double-click. */
  const handleSearchDoubleClick = useCallback(
    (resultPath: string) => {
      const sep = resultPath.includes('\\') ? '\\' : '/';
      const idx = resultPath.lastIndexOf(sep);
      const parent = idx > 0 ? resultPath.slice(0, idx) : sep;
      onNavigate(parent);
    },
    [onNavigate],
  );

  // ── Search mode ─────────────────────────────────────────────────────────────
  if (isSearchMode) {
    return (
      <div ref={scrollRef} className="h-full overflow-auto">
        <table
          className="w-full text-sm border-collapse"
          style={{ tableLayout: 'fixed', width: '100%' }}
        >
          <colgroup>
            <col style={{ width: 28 }} />
            <col style={{ width: 180 }} />
            <col />
          </colgroup>
          <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
            <tr>
              <th className="px-3 py-2 w-7">
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someSelected;
                  }}
                  onChange={(e) => onSelectAll(e.target.checked)}
                  className="cursor-pointer"
                />
              </th>
              <th className="px-3 py-2 text-left font-medium text-gray-700">Name</th>
              <th className="px-3 py-2 text-left font-medium text-gray-700">Path</th>
            </tr>
          </thead>
          <tbody>
            {paddingTop > 0 && (
              <tr>
                <td colSpan={3} style={{ height: paddingTop }} />
              </tr>
            )}
            {virtualItems.map((vRow) => {
              const se = searchEntries[vRow.index];
              const isSelected = selectedPaths.has(se.path);
              const isFocused = focusedPath === se.path;
              return (
                <tr
                  key={se.path}
                  data-path={se.path}
                  style={{ height: ROW_HEIGHT }}
                  className={[
                    'border-b border-gray-100 cursor-pointer',
                    isSelected || isFocused
                      ? 'bg-[#e8f1ff]'
                      : vRow.index % 2 === 0
                      ? 'bg-white hover:bg-gray-50'
                      : 'bg-gray-50/50 hover:bg-gray-50',
                  ].join(' ')}
                  onClick={() => onFocus(se.path)}
                  onDoubleClick={() => handleSearchDoubleClick(se.path)}
                >
                  <td className="px-3">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={(e) => onSelect(se.path, e.target.checked)}
                      onClick={(e) => e.stopPropagation()}
                      className="cursor-pointer"
                    />
                  </td>
                  <td className="px-3 truncate">
                    <div className="flex items-center gap-2">
                      <File className="w-4 h-4 shrink-0 text-gray-400" />
                      <span className="truncate text-gray-900">{se.name}</span>
                    </div>
                  </td>
                  <td className="px-3 text-gray-500 truncate text-xs">{se.parentDir}</td>
                </tr>
              );
            })}
            {paddingBottom > 0 && (
              <tr>
                <td colSpan={3} style={{ height: paddingBottom }} />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    );
  }

  // ── Folder browse mode ───────────────────────────────────────────────────────
  return (
    <div ref={scrollRef} className="h-full overflow-auto">
      <table
        className="w-full text-sm border-collapse"
        style={{ tableLayout: 'fixed', width: '100%' }}
      >
        <colgroup>
          <col style={{ width: 28 }} />
          <col />                          {/* Name — flex */}
          <col style={{ width: 80 }} />
          <col style={{ width: 110 }} />
          <col style={{ width: 145 }} />
        </colgroup>
        <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
          <tr>
            <th className="px-3 py-2 w-7">
              <input
                type="checkbox"
                checked={allSelected}
                ref={(el) => {
                  if (el) el.indeterminate = someSelected;
                }}
                onChange={(e) => onSelectAll(e.target.checked)}
                className="cursor-pointer"
              />
            </th>
            <ColHeader label="Name"          col="name"     sortCol={sortCol} sortDir={sortDir} onClick={onSort} />
            <ColHeader label="Size"          col="size"     sortCol={sortCol} sortDir={sortDir} onClick={onSort} />
            <ColHeader label="Type"          col="type"     sortCol={sortCol} sortDir={sortDir} onClick={onSort} />
            <ColHeader label="Date Modified" col="modified" sortCol={sortCol} sortDir={sortDir} onClick={onSort} />
          </tr>
        </thead>
        <tbody>
          {paddingTop > 0 && (
            <tr>
              <td colSpan={5} style={{ height: paddingTop }} />
            </tr>
          )}
          {virtualItems.map((vRow) => {
            const entry = sortedEntries[vRow.index];
            const isSelected = selectedPaths.has(entry.path);
            const isFocused = focusedPath === entry.path;
            return (
              <tr
                key={entry.path}
                data-path={entry.path}
                style={{ height: ROW_HEIGHT }}
                className={[
                  'border-b border-gray-100 cursor-pointer',
                  isSelected || isFocused
                    ? 'bg-[#e8f1ff]'
                    : vRow.index % 2 === 0
                    ? 'bg-white hover:bg-gray-50'
                    : 'bg-gray-50/50 hover:bg-gray-50',
                ].join(' ')}
                onClick={() => onFocus(entry.path)}
                onDoubleClick={() => handleRowDoubleClick(entry)}
              >
                <td className="px-3">
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={(e) => onSelect(entry.path, e.target.checked)}
                    onClick={(e) => e.stopPropagation()}
                    className="cursor-pointer"
                  />
                </td>
                <td className="px-3">
                  <div className="flex items-center gap-2 overflow-hidden">
                    <FileIcon entry={entry} />
                    <span className="truncate text-gray-900">{entry.name}</span>
                  </div>
                </td>
                <td className="px-3 text-gray-600 truncate">{entry.size || '--'}</td>
                <td className="px-3 text-gray-600 truncate">{entry.type}</td>
                <td className="px-3 text-gray-600 truncate">{entry.modified}</td>
              </tr>
            );
          })}
          {paddingBottom > 0 && (
            <tr>
              <td colSpan={5} style={{ height: paddingBottom }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
