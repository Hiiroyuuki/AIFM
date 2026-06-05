/**
 * FolderTree — lazy-loading folder navigation tree.
 *
 * Each node fetches its children only when first expanded (lazy load),
 * so large directory trees never cause an upfront data avalanche.
 * Only folders are shown; files stay in the main file table.
 *
 * Tree-to-address-bar sync (auto-expand when navigating via address bar)
 * is not implemented here — clicking tree items drives navigation.
 */
import { useState, useEffect, useCallback } from 'react';
import { ChevronRight, Folder, FolderOpen, Loader2 } from 'lucide-react';
import { listFolder } from '../../api/client';

// ── helpers ──────────────────────────────────────────────────────────────────

/** Normalise a Windows path for case-insensitive comparison. */
function normPath(p: string) {
  return p.toLowerCase().replace(/\//g, '\\').replace(/\\+$/, '');
}

/** Extract drive root like "C:\\" from any Windows path. */
function driveRoot(path: string): string | null {
  const m = path.match(/^([A-Za-z]:[/\\])/);
  return m ? m[1].toUpperCase().replace('/', '\\') : null;
}

// ── TreeNode component ────────────────────────────────────────────────────────

interface TreeNodeProps {
  path: string;
  name: string;
  level: number;
  currentPath: string;
  onNavigate: (path: string) => void;
}

function TreeNode({ path, name, level, currentPath, onNavigate }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<{ name: string; path: string }[] | null>(null);
  const [loading, setLoading] = useState(false);

  const isActive = normPath(path) === normPath(currentPath);

  const handleClick = useCallback(async () => {
    onNavigate(path);

    if (expanded) {
      setExpanded(false);
      return;
    }

    // First expand: fetch children.
    if (children === null) {
      setLoading(true);
      try {
        const data = await listFolder(path);
        setChildren(
          data.entries
            .filter((e) => e.is_dir)
            .map((e) => ({ name: e.name, path: e.path })),
        );
      } catch {
        setChildren([]);
      } finally {
        setLoading(false);
      }
    }
    setExpanded(true);
  }, [path, expanded, children, onNavigate]);

  return (
    <div>
      <div
        className={[
          'flex items-center gap-1 py-[3px] pr-1 rounded cursor-pointer text-xs select-none',
          isActive
            ? 'bg-blue-100 text-blue-800 font-medium'
            : 'hover:bg-gray-100 text-gray-700',
        ].join(' ')}
        style={{ paddingLeft: level * 12 + 4 }}
        onClick={handleClick}
      >
        {loading ? (
          <Loader2 className="w-3 h-3 shrink-0 animate-spin text-gray-400" />
        ) : (
          <ChevronRight
            className={[
              'w-3 h-3 shrink-0 text-gray-400 transition-transform',
              expanded ? 'rotate-90' : '',
            ].join(' ')}
          />
        )}
        {isActive ? (
          <FolderOpen className="w-3.5 h-3.5 shrink-0 text-yellow-600" />
        ) : (
          <Folder className="w-3.5 h-3.5 shrink-0 text-yellow-500" />
        )}
        <span className="truncate">{name}</span>
      </div>

      {expanded && children && children.length > 0 && (
        <div>
          {children.map((child) => (
            <TreeNode
              key={child.path}
              path={child.path}
              name={child.name}
              level={level + 1}
              currentPath={currentPath}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── FolderTree (root component) ───────────────────────────────────────────────

interface FolderTreeProps {
  currentPath: string;
  onNavigate: (path: string) => void;
}

export function FolderTree({ currentPath, onNavigate }: FolderTreeProps) {
  const [drives, setDrives] = useState<string[]>([]);

  // Detect available drive roots at mount time.
  useEffect(() => {
    const candidates = ['C:\\', 'D:\\', 'E:\\', 'F:\\', 'G:\\'];
    Promise.allSettled(candidates.map((d) => listFolder(d))).then((results) => {
      const available = candidates.filter((_, i) => results[i].status === 'fulfilled');
      // Always show at least C:\.
      if (available.length === 0) available.push('C:\\');
      setDrives(available);
    });
  }, []);

  // Append a new drive root when the user navigates to one not yet listed.
  useEffect(() => {
    const root = driveRoot(currentPath);
    if (!root) return;
    setDrives((prev) =>
      prev.some((d) => d.toLowerCase() === root.toLowerCase())
        ? prev
        : [...prev, root],
    );
  }, [currentPath]);

  return (
    <div className="h-full overflow-y-auto p-1.5">
      {drives.map((drive) => (
        <TreeNode
          key={drive}
          path={drive}
          name={drive.replace('\\', '')}   // "C:\" → "C:"
          level={0}
          currentPath={currentPath}
          onNavigate={onNavigate}
        />
      ))}
    </div>
  );
}
