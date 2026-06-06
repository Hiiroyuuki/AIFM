/**
 * LLMSettingsDialog — side Sheet for managing LLM provider profiles.
 *
 * Features:
 *  • List all providers with active indicator
 *  • Edit nickname / API key / base URL / default model per provider
 *  • Fetch available models from the provider's /models endpoint
 *  • Set active provider
 *  • Add new provider
 *  • Delete provider (not the last one)
 */
import { useState, useEffect, useCallback } from 'react';
import { Loader2, Plus, Trash2, CheckCircle2, RefreshCw, ChevronDown } from 'lucide-react';
import {
  getProviders, getActiveProvider,
  setActiveProvider, updateProvider, addProvider,
  deleteProvider, fetchProviderModels,
} from '../../api/client';
import type { ProviderView } from '../../api/types';

import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from './ui/sheet';

// ── Field ─────────────────────────────────────────────────────────────────────

function Field({
  label, value, onChange, type = 'text', placeholder = '',
}: {
  label: string; value: string;
  onChange: (v: string) => void;
  type?: string; placeholder?: string;
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-600 mb-0.5">{label}</label>
      <input
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full px-2 py-1.5 text-xs border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
    </div>
  );
}

// ── ProviderCard ──────────────────────────────────────────────────────────────

interface ProviderCardProps {
  provider: ProviderView;
  isActive: boolean;
  onSetActive: (name: string) => void;
  onSave: (name: string, data: SaveData) => Promise<void>;
  onDelete: (name: string) => void;
  canDelete: boolean;
}

interface SaveData { nickname: string; api_key?: string; model: string; base_url: string }

function ProviderCard({ provider, isActive, onSetActive, onSave, onDelete, canDelete }: ProviderCardProps) {
  const [expanded, setExpanded] = useState(isActive);
  const [form, setForm] = useState<SaveData>({
    nickname: provider.nickname,
    api_key: '',
    model: provider.default_model,
    base_url: provider.base_url,
  });
  const [models, setModels] = useState<string[]>(provider.models);
  const [fetchLoading, setFetchLoading] = useState(false);
  const [saveLoading,  setSaveLoading]  = useState(false);
  const [fetchError,   setFetchError]   = useState('');

  const set = (key: keyof SaveData) => (v: string) =>
    setForm(prev => ({ ...prev, [key]: v }));

  const handleFetchModels = async () => {
    setFetchLoading(true);
    setFetchError('');
    try {
      const res = await fetchProviderModels(provider.name, form.api_key || undefined, form.base_url || undefined);
      setModels(res.models);
      if (!form.model && res.models.length) setForm(prev => ({ ...prev, model: res.models[0] }));
    } catch (e: unknown) {
      setFetchError(e instanceof Error ? e.message : String(e));
    } finally {
      setFetchLoading(false);
    }
  };

  const handleSave = async () => {
    setSaveLoading(true);
    try {
      // Only send api_key if the user typed a new one; empty = keep existing.
      await onSave(provider.name, {
        ...form,
        api_key: form.api_key.trim() || undefined,
      });
      setForm(prev => ({ ...prev, api_key: '' }));
    } finally {
      setSaveLoading(false);
    }
  };

  return (
    <div className={`border rounded-lg overflow-hidden ${isActive ? 'border-blue-400' : 'border-gray-200'}`}>
      {/* Header row */}
      <div
        className={`flex items-center gap-2 px-3 py-2 cursor-pointer ${isActive ? 'bg-blue-50' : 'hover:bg-gray-50'}`}
        onClick={() => setExpanded(e => !e)}
      >
        {isActive && <CheckCircle2 className="w-3.5 h-3.5 text-blue-600 shrink-0" />}
        <span className="text-sm font-medium text-gray-900 flex-1">
          {provider.nickname || provider.name}
          {provider.nickname && <span className="text-xs text-gray-400 font-normal ml-1.5">({provider.name})</span>}
        </span>
        {provider.api_key_configured && (
          <span className="text-[10px] bg-green-100 text-green-700 px-1.5 py-0.5 rounded-full">key set</span>
        )}
        <ChevronDown className={`w-3.5 h-3.5 text-gray-400 transition-transform ${expanded ? 'rotate-180' : ''}`} />
      </div>

      {/* Expanded form */}
      {expanded && (
        <div className="px-3 pb-3 pt-1 space-y-2 border-t border-gray-100">
          <Field label="Nickname"  value={form.nickname} onChange={set('nickname')} placeholder="My Provider" />
          <Field label="Base URL"  value={form.base_url} onChange={set('base_url')} placeholder="https://api.example.com/v1" />
          <Field label="API Key"   value={form.api_key}  onChange={set('api_key')}  type="password" placeholder={provider.api_key_configured ? '(saved — enter new to change)' : 'sk-...'} />

          {/* Model selector + fetch */}
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-0.5">Model</label>
            <div className="flex gap-1">
              <select
                value={form.model}
                onChange={e => setForm(prev => ({ ...prev, model: e.target.value }))}
                className="flex-1 px-2 py-1.5 text-xs border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500 bg-white"
              >
                {!models.length && <option value="">— enter model name —</option>}
                {models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              <button
                onClick={handleFetchModels}
                disabled={fetchLoading}
                title="Fetch models from provider"
                className="px-2 py-1.5 border border-gray-300 rounded hover:bg-gray-50 transition-colors text-gray-600 disabled:opacity-50"
              >
                {fetchLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
              </button>
            </div>
            {/* Manual model input when list is empty */}
            {!models.length && (
              <input
                type="text"
                value={form.model}
                onChange={e => setForm(prev => ({ ...prev, model: e.target.value }))}
                placeholder="model-name"
                className="mt-1 w-full px-2 py-1.5 text-xs border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
            )}
            {fetchError && <p className="text-xs text-red-500 mt-1">{fetchError}</p>}
          </div>

          <div className="flex gap-2 pt-1">
            <button
              onClick={handleSave}
              disabled={saveLoading}
              className="flex items-center gap-1 px-3 py-1.5 bg-blue-600 text-white rounded text-xs hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              {saveLoading ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
              Save
            </button>
            {!isActive && (
              <button
                onClick={() => onSetActive(provider.name)}
                className="px-3 py-1.5 border border-blue-400 text-blue-600 rounded text-xs hover:bg-blue-50 transition-colors"
              >
                Set active
              </button>
            )}
            {canDelete && (
              <button
                onClick={() => onDelete(provider.name)}
                className="ml-auto px-2 py-1.5 text-red-500 hover:bg-red-50 rounded transition-colors"
                title="Delete provider"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── AddProviderForm ───────────────────────────────────────────────────────────

function AddProviderForm({ onAdd }: { onAdd: (data: AddData) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ provider: '', nickname: '', api_key: '', base_url: '', model: '' });
  const set = (k: keyof typeof form) => (v: string) => setForm(p => ({ ...p, [k]: v }));

  const handleAdd = async () => {
    if (!form.provider.trim()) return;
    setSaving(true);
    try {
      await onAdd(form);
      setForm({ provider: '', nickname: '', api_key: '', base_url: '', model: '' });
      setOpen(false);
    } finally { setSaving(false); }
  };

  return (
    <div className="border-2 border-dashed border-gray-200 rounded-lg">
      {!open ? (
        <button
          onClick={() => setOpen(true)}
          className="w-full flex items-center justify-center gap-2 py-3 text-sm text-gray-500 hover:text-gray-700 hover:bg-gray-50 transition-colors rounded-lg"
        >
          <Plus className="w-4 h-4" /> Add provider
        </button>
      ) : (
        <div className="p-3 space-y-2">
          <Field label="Provider key (e.g. openai)"  value={form.provider}  onChange={set('provider')}  placeholder="openai" />
          <Field label="Nickname"                    value={form.nickname}  onChange={set('nickname')}  placeholder="My OpenAI" />
          <Field label="Base URL"                    value={form.base_url}  onChange={set('base_url')}  placeholder="https://api.openai.com/v1" />
          <Field label="API Key"                     value={form.api_key}   onChange={set('api_key')}   type="password" />
          <Field label="Default model"               value={form.model}     onChange={set('model')}     placeholder="gpt-4o" />
          <div className="flex gap-2 pt-1">
            <button onClick={handleAdd} disabled={saving || !form.provider.trim()}
              className="px-3 py-1.5 bg-blue-600 text-white rounded text-xs hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1">
              {saving && <Loader2 className="w-3 h-3 animate-spin" />} Add
            </button>
            <button onClick={() => setOpen(false)} className="px-3 py-1.5 border rounded text-xs hover:bg-gray-50">Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

interface AddData { provider: string; nickname: string; api_key: string; base_url: string; model: string }

// ── LLMSettingsDialog ────────────────────────────────────────────────────────

interface LLMSettingsDialogProps {
  open: boolean;
  onClose: () => void;
  /** Fired every time a config mutation succeeds — triggers WS reconnect. */
  onConfigChanged: () => void;
}

export function LLMSettingsDialog({ open, onClose, onConfigChanged }: LLMSettingsDialogProps) {
  const [providers,        setProviders]        = useState<ProviderView[]>([]);
  const [activeProviderName, setActiveProviderName] = useState('');
  const [loading,          setLoading]          = useState(false);
  const [error,            setError]            = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [ps, active] = await Promise.all([getProviders(), getActiveProvider()]);
      setProviders(ps);
      setActiveProviderName(active.name);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { if (open) load(); }, [open, load]);

  const handleSetActive = async (name: string) => {
    await setActiveProvider(name);
    onConfigChanged();
    await load();
  };

  const handleSave = async (name: string, data: SaveData) => {
    await updateProvider(name, data);
    onConfigChanged();
    await load();
  };

  const handleAdd = async (data: AddData) => {
    await addProvider(data);
    onConfigChanged();
    await load();
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`Delete provider "${name}"?`)) return;
    await deleteProvider(name);
    onConfigChanged();
    await load();
  };

  return (
    <Sheet open={open} onOpenChange={v => !v && onClose()}>
      <SheetContent side="right" className="w-full sm:max-w-md overflow-y-auto">
        <SheetHeader>
          <SheetTitle>LLM Settings</SheetTitle>
        </SheetHeader>

        {loading && (
          <div className="flex items-center gap-2 mt-4 text-gray-500">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span className="text-sm">Loading…</span>
          </div>
        )}

        {error && (
          <p className="mt-4 text-sm text-red-600 bg-red-50 rounded p-3">{error}</p>
        )}

        {!loading && !error && (
          <div className="mt-4 space-y-3">
            {providers.map(p => (
              <ProviderCard
                key={p.name}
                provider={p}
                isActive={p.name === activeProviderName}
                onSetActive={handleSetActive}
                onSave={handleSave}
                onDelete={handleDelete}
                canDelete={providers.length > 1}
              />
            ))}
            <AddProviderForm onAdd={handleAdd} />
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
