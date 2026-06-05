/**
 * NewAIFolderDialog — create an AI-managed folder with a chosen authorization mode.
 *
 * The three authorization modes match AIFolderStore constants:
 *   user_required  – every AI write needs user confirmation (default)
 *   ai_decides     – AI can decide autonomously, but may ask
 *   always_allowed – AI can do anything without confirmation
 */
import { useState, useEffect } from 'react';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';

const AUTH_MODES = [
  { value: 'user_required',  label: 'User Required',   desc: 'AI must ask before every write' },
  { value: 'ai_decides',     label: 'AI Decides',      desc: 'AI chooses when to ask' },
  { value: 'always_allowed', label: 'Always Allowed',  desc: 'AI writes without asking' },
] as const;

interface NewAIFolderDialogProps {
  open: boolean;
  defaultParent: string;
  onConfirm: (parent: string, name: string, authMode: string) => void;
  onCancel: () => void;
}

export function NewAIFolderDialog({
  open,
  defaultParent,
  onConfirm,
  onCancel,
}: NewAIFolderDialogProps) {
  const [parent, setParent] = useState(defaultParent);
  const [name, setName] = useState('New AIFolder');
  const [authMode, setAuthMode] = useState<string>('user_required');

  // Sync parent when prop changes (e.g., right-click on a different folder).
  useEffect(() => {
    setParent(defaultParent);
  }, [defaultParent]);

  const handleConfirm = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    onConfirm(parent, trimmed, authMode);
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New AIFolder</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* Parent folder */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Parent folder
            </label>
            <input
              type="text"
              value={parent}
              onChange={(e) => setParent(e.target.value)}
              className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          {/* Folder name */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Folder name
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleConfirm()}
              className="w-full px-3 py-1.5 text-sm border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
              autoFocus
            />
          </div>

          {/* Authorization mode */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Authorization mode
            </label>
            <div className="space-y-2">
              {AUTH_MODES.map((m) => (
                <label key={m.value} className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="radio"
                    name="auth-mode"
                    value={m.value}
                    checked={authMode === m.value}
                    onChange={() => setAuthMode(m.value)}
                    className="mt-0.5"
                  />
                  <div>
                    <span className="text-sm font-medium text-gray-900">{m.label}</span>
                    <p className="text-xs text-gray-500">{m.desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>
        </div>

        <DialogFooter>
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={!name.trim()}
            className="px-4 py-2 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            Create
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
