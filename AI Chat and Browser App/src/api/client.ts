/**
 * Unified AIFM API client.
 *
 * Every fetch call goes through apiFetch — base URL, Content-Type, and error
 * unpacking all live here.  WebSocket URL is exported as WS_BASE.
 */
import type {
  AnalysisRunResponse,
  ListResponse,
  MutationResponse,
  PreviewResponse,
  ProviderView,
  SearchResponse,
  StatResponse,
  UndoStateResponse,
} from './types';

export const API_BASE: string =
  (typeof import.meta !== 'undefined' &&
    (import.meta as Record<string, Record<string, string>>).env?.VITE_API_BASE) ||
  'http://127.0.0.1:8000';

/** WebSocket base URL derived from the API base URL. */
export const WS_BASE = API_BASE.replace(/^http/, 'ws');

const enc = encodeURIComponent;

export async function apiFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const hasBody = options?.body !== undefined;
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
      ...(options?.headers as Record<string, string> | undefined),
    },
  });
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body?.detail ?? JSON.stringify(body);
    } catch {
      detail = await res.text().catch(() => '');
    }
    throw new Error(`HTTP ${res.status}${detail ? ': ' + detail : ''}`);
  }
  return res.json();
}

// ── File system — read ────────────────────────────────────────────────────────

export const listFolder = (path: string) =>
  apiFetch<ListResponse>(`/api/fs/list?path=${enc(path)}`);

export const statPath = (path: string) =>
  apiFetch<StatResponse>(`/api/fs/stat?path=${enc(path)}`);

export const previewFile = (path: string) =>
  apiFetch<PreviewResponse>(`/api/fs/preview?path=${enc(path)}`);

/** URL for inline image display — use as <img src={imageUrl(path)} /> */
export const imageUrl = (path: string) =>
  `${API_BASE}/api/fs/image?path=${enc(path)}`;

// ── File system — write ───────────────────────────────────────────────────────

export const pasteFiles = (sources: string[], destination: string, move: boolean) =>
  apiFetch<MutationResponse>('/api/fs/paste', {
    method: 'POST',
    body: JSON.stringify({ sources, destination, move }),
  });

export const deleteFiles = (paths: string[]) =>
  apiFetch<MutationResponse>('/api/fs/delete', {
    method: 'POST',
    body: JSON.stringify({ paths }),
  });

export const undoOperation = () =>
  apiFetch<MutationResponse>('/api/fs/undo', { method: 'POST', body: '{}' });

export const redoOperation = () =>
  apiFetch<MutationResponse>('/api/fs/redo', { method: 'POST', body: '{}' });

export const getUndoState = () =>
  apiFetch<UndoStateResponse>('/api/fs/undo-state');

// ── Search ────────────────────────────────────────────────────────────────────

export const searchFiles = (q: string, limit = 500) =>
  apiFetch<SearchResponse>(`/api/search?q=${enc(q)}&limit=${limit}`);

// ── Analysis ──────────────────────────────────────────────────────────────────

export const runAnalysis = (path: string) =>
  apiFetch<AnalysisRunResponse>('/api/analysis/run', {
    method: 'POST',
    body: JSON.stringify({ path }),
  });

// ── AI Folders ────────────────────────────────────────────────────────────────

export const createAIFolder = (
  parent_path: string,
  name: string,
  authorization_mode: string,
) =>
  apiFetch<Record<string, unknown>>('/api/ai-folders', {
    method: 'POST',
    body: JSON.stringify({ parent_path, name, authorization_mode }),
  });

// ── LLM config ────────────────────────────────────────────────────────────────

export const getProviders = () =>
  apiFetch<ProviderView[]>('/api/config/providers');

export const getActiveProvider = () =>
  apiFetch<ProviderView>('/api/config/active-provider');

export const setActiveProvider = (provider: string) =>
  apiFetch<{ ok: boolean }>('/api/config/active-provider', {
    method: 'PUT',
    body: JSON.stringify({ provider }),
  });

export const updateProvider = (
  name: string,
  data: { nickname: string; api_key?: string; model: string; base_url?: string },
) =>
  apiFetch<{ ok: boolean }>(`/api/config/providers/${enc(name)}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });

export const addProvider = (data: {
  provider: string;
  nickname: string;
  api_key: string;
  model: string;
  base_url?: string;
}) =>
  apiFetch<{ ok: boolean; name: string }>('/api/config/providers', {
    method: 'POST',
    body: JSON.stringify(data),
  });

export const deleteProvider = (name: string) =>
  apiFetch<{ ok: boolean }>(`/api/config/providers/${enc(name)}`, {
    method: 'DELETE',
  });

export const fetchProviderModels = (
  name: string,
  api_key?: string,
  base_url?: string,
) => {
  const params = new URLSearchParams();
  if (api_key)  params.set('api_key', api_key);
  if (base_url) params.set('base_url', base_url);
  const qs = params.toString() ? `?${params}` : '';
  return apiFetch<{ models: string[] }>(
    `/api/config/providers/${enc(name)}/fetch-models${qs}`,
  );
};
