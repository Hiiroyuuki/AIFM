/** TypeScript types matching the AIFM FastAPI response shapes. */

// ── Browse ────────────────────────────────────────────────────────────────────

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  is_file: boolean;
  type: string;
  size_bytes: number | null;
  size: string;
  modified: string;
  modified_ts: number;
  error?: string;
}

export interface ListResponse {
  path: string;
  entries: FileEntry[];
  total: number;
  files: number;
  folders: number;
}

export interface AnalysisRecord {
  root_path: string;
  folder_path: string;
  size_bytes: number;
  size: string;
  file_count: number;
  folder_count: number;
  error_count: number;
  analysed_at: string;
}

export interface StatResponse extends FileEntry {
  analysis: AnalysisRecord | null;
}

// ── File preview ──────────────────────────────────────────────────────────────

export type PreviewResponse =
  | { kind: 'text';   content: string; truncated: boolean; size_bytes: number; encoding: string }
  | { kind: 'image';  size_bytes: number; mime: string }
  | { kind: 'binary'; size_bytes: number; mime: string };

// ── Search ────────────────────────────────────────────────────────────────────

export interface SearchResponse {
  query: string;
  paths: string[];
  shown: number;
  total: number;
  message: string;
  startup_message: string;
}

// ── Write operations ──────────────────────────────────────────────────────────

export interface MutationResponse {
  done: string[];
  skipped: string[];
  errors: string[];
  operations?: unknown[];
  ai_folder_records?: unknown[];
  can_undo: boolean;
  can_redo: boolean;
}

export interface UndoStateResponse {
  can_undo: boolean;
  can_redo: boolean;
}

// ── Analysis ──────────────────────────────────────────────────────────────────

export interface AnalysisRunResponse {
  root_path: string;
  folder_path: string;
  size_bytes: number;
  size: string;
  file_count: number;
  folder_count: number;
  error_count: number;
  analysed_at: string;
}

// ── LLM config ────────────────────────────────────────────────────────────────

export interface ProviderView {
  name: string;
  aliases: string[];
  base_url: string;
  models: string[];
  default_model: string;
  api_key_configured: boolean;
  nickname: string;
}

// ── WebSocket agent frames ────────────────────────────────────────────────────

export type WsClientFrame =
  | { type: 'start'; request: string; context: AgentContext }
  | { type: 'confirm'; confirmed: boolean };

export interface AgentContext {
  current_folder: string;
  selected_paths: string[];
  active_path: string;
  search_query?: string;
}

export type WsServerFrame =
  | { type: 'thinking' }
  | { type: 'step'; preview: string }
  | { type: 'tool_call'; name: string; arguments: Record<string, unknown> }
  | { type: 'observation'; name: string; result: Record<string, unknown> }
  | { type: 'pending'; reply: string; actions: PendingAction[]; observations: Observation[] }
  | { type: 'done'; reply: string; observations: Observation[] }
  | { type: 'cancelled' }
  | { type: 'error'; message: string };

export interface PendingAction {
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
}

export interface Observation {
  name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
}

// ── Sort ──────────────────────────────────────────────────────────────────────

export type SortCol = 'name' | 'size' | 'type' | 'modified';
export type SortDir = 'asc' | 'desc';
