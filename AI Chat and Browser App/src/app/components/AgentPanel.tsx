/**
 * AgentPanel — streaming agent chat with per-step visibility and pending-action
 * confirmation UI.
 *
 * WebSocket protocol (frames from server):
 *   thinking     → spinner while the model API is being called
 *   step         → raw model output preview (shown collapsed)
 *   tool_call    → tool invocation card
 *   observation  → tool result card
 *   pending      → write-action confirmation card (user must approve/cancel)
 *   done         → final agent reply bubble; triggers onRefresh()
 *   cancelled    → shows "cancelled" status
 *   error        → shows error bubble
 *
 * The WebSocket is opened on mount and kept alive; reconnect after 2 s on drop.
 */
import {
  useState, useEffect, useRef, useCallback,
  type KeyboardEvent,
} from 'react';
import {
  Sparkles, Send, Loader2, CheckCircle2, XCircle,
  ChevronDown, ChevronRight, Wrench, Eye,
} from 'lucide-react';
import { WS_BASE } from '../../api/client';
import type { WsServerFrame, PendingAction, Observation } from '../../api/types';

// ── Message types ─────────────────────────────────────────────────────────────

type MsgRole = 'user' | 'agent' | 'tool_call' | 'observation' | 'system' | 'error';

interface ChatMsg {
  id: string;
  role: MsgRole;
  content?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown>;
  ts: number;
}

let _id = 0;
const uid = () => String(++_id);

// ── Sub-components ────────────────────────────────────────────────────────────

function ToolCallCard({ tool, args }: { tool: string; args: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-blue-200 bg-blue-50 rounded-lg overflow-hidden text-xs">
      <button
        className="w-full flex items-center gap-1.5 px-3 py-1.5 text-blue-700 hover:bg-blue-100 transition-colors"
        onClick={() => setOpen(o => !o)}
      >
        <Wrench className="w-3 h-3 shrink-0" />
        <span className="font-mono font-medium">{tool}</span>
        {open ? <ChevronDown className="w-3 h-3 ml-auto" /> : <ChevronRight className="w-3 h-3 ml-auto" />}
      </button>
      {open && (
        <pre className="px-3 pb-2 text-blue-800 whitespace-pre-wrap break-all overflow-auto max-h-40">
          {JSON.stringify(args, null, 2)}
        </pre>
      )}
    </div>
  );
}

function ObservationCard({ tool, result }: { tool: string; result: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const preview = (() => {
    if (typeof result.content === 'string') return result.content.slice(0, 80);
    if (Array.isArray(result.entries)) return `${result.entries.length} entries`;
    if (typeof result.size === 'string') return result.size;
    return '';
  })();

  return (
    <div className="border border-gray-200 bg-gray-50 rounded-lg overflow-hidden text-xs">
      <button
        className="w-full flex items-center gap-1.5 px-3 py-1.5 text-gray-600 hover:bg-gray-100 transition-colors"
        onClick={() => setOpen(o => !o)}
      >
        <Eye className="w-3 h-3 shrink-0 text-gray-400" />
        <span className="font-mono text-gray-500">{tool}</span>
        {preview && <span className="ml-2 truncate text-gray-400">{preview}</span>}
        {open ? <ChevronDown className="w-3 h-3 ml-auto shrink-0" /> : <ChevronRight className="w-3 h-3 ml-auto shrink-0" />}
      </button>
      {open && (
        <pre className="px-3 pb-2 text-gray-700 whitespace-pre-wrap break-all overflow-auto max-h-48">
          {JSON.stringify(result, null, 2)}
        </pre>
      )}
    </div>
  );
}

function PendingCard({
  reply,
  actions,
  onConfirm,
  onCancel,
  disabled,
}: {
  reply: string;
  actions: PendingAction[];
  onConfirm: () => void;
  onCancel: () => void;
  disabled: boolean;
}) {
  return (
    <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 space-y-2">
      <div className="flex items-start gap-2">
        <Sparkles className="w-4 h-4 text-amber-600 mt-0.5 shrink-0" />
        <p className="text-xs text-amber-900">{reply}</p>
      </div>
      <div className="space-y-1">
        {actions.map((a, i) => (
          <div key={i} className="flex items-center gap-2 bg-white rounded px-2 py-1 border border-amber-200 text-xs">
            <Wrench className="w-3 h-3 text-amber-600 shrink-0" />
            <span className="font-mono font-medium text-amber-800">{a.tool}</span>
            <span className="text-gray-500 truncate">{JSON.stringify(a.arguments).slice(0, 60)}</span>
          </div>
        ))}
      </div>
      <div className="flex gap-2 pt-1">
        <button
          onClick={onConfirm}
          disabled={disabled}
          className="flex items-center gap-1 px-3 py-1 bg-green-600 text-white rounded text-xs hover:bg-green-700 transition-colors disabled:opacity-50"
        >
          <CheckCircle2 className="w-3 h-3" /> Approve
        </button>
        <button
          onClick={onCancel}
          disabled={disabled}
          className="flex items-center gap-1 px-3 py-1 bg-gray-200 text-gray-700 rounded text-xs hover:bg-gray-300 transition-colors disabled:opacity-50"
        >
          <XCircle className="w-3 h-3" /> Cancel
        </button>
      </div>
    </div>
  );
}

// ── AgentPanel ────────────────────────────────────────────────────────────────

interface AgentPanelProps {
  currentPath: string;
  selectedPaths: Set<string>;
  focusedPath: string | null;
  searchQuery?: string;
  onRefresh: () => void;
}

type Status = 'idle' | 'running' | 'pending';

export function AgentPanel({
  currentPath,
  selectedPaths,
  focusedPath,
  searchQuery = '',
  onRefresh,
}: AgentPanelProps) {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput]       = useState('');
  const [status, setStatus]     = useState<Status>('idle');
  const [isConnected, setConnected] = useState(false);
  const [pendingData, setPendingData] = useState<{ reply: string; actions: PendingAction[]; obs: Observation[] } | null>(null);

  const wsRef       = useRef<WebSocket | null>(null);
  const retryRef    = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bottomRef   = useRef<HTMLDivElement>(null);

  // Stable refs for callbacks that need latest state without re-attaching WS.
  const onRefreshRef = useRef(onRefresh);
  useEffect(() => { onRefreshRef.current = onRefresh; });

  const addMsg = useCallback((msg: Omit<ChatMsg, 'id' | 'ts'>) => {
    setMessages(prev => [...prev, { ...msg, id: uid(), ts: Date.now() }]);
  }, []);

  // Auto-scroll.
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  // ── WebSocket connect / reconnect ─────────────────────────────────────────

  const connect = useCallback(() => {
    if (retryRef.current) clearTimeout(retryRef.current);
    const ws = new WebSocket(`${WS_BASE}/ws/agent`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);

    ws.onclose = () => {
      setConnected(false);
      retryRef.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (ev: MessageEvent) => {
      let frame: WsServerFrame;
      try { frame = JSON.parse(ev.data as string); }
      catch { return; }

      switch (frame.type) {
        case 'thinking':
          // Status bar spinner is enough — no chat bubble needed.
          break;

        case 'step':
          // Ignore raw model output preview in normal UI.
          break;

        case 'tool_call':
          addMsg({ role: 'tool_call', tool: frame.name, args: frame.arguments });
          break;

        case 'observation':
          addMsg({ role: 'observation', tool: frame.name, result: frame.result });
          break;

        case 'pending':
          setPendingData({ reply: frame.reply, actions: frame.actions, obs: frame.observations });
          setStatus('pending');
          break;

        case 'done':
          setStatus('idle');
          setPendingData(null);
          addMsg({ role: 'agent', content: frame.reply });
          if (frame.observations.length > 0) onRefreshRef.current();
          break;

        case 'cancelled':
          setStatus('idle');
          setPendingData(null);
          addMsg({ role: 'system', content: 'Action cancelled.' });
          break;

        case 'error':
          setStatus('idle');
          setPendingData(null);
          addMsg({ role: 'error', content: frame.message });
          break;
      }
    };
  }, [addMsg]);

  useEffect(() => {
    connect();
    return () => {
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  // ── Send request ──────────────────────────────────────────────────────────

  const sendRequest = useCallback(() => {
    const req = input.trim();
    if (!req || status !== 'idle' || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

    addMsg({ role: 'user', content: req });
    setInput('');
    setStatus('running');

    wsRef.current.send(JSON.stringify({
      type: 'start',
      request: req,
      context: {
        current_folder: currentPath,
        selected_paths: Array.from(selectedPaths),
        active_path: focusedPath ?? '',
        search_query: searchQuery,
      },
    }));
  }, [input, status, currentPath, selectedPaths, focusedPath, searchQuery, addMsg]);

  const sendConfirm = useCallback((confirmed: boolean) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    setStatus('running');
    wsRef.current.send(JSON.stringify({ type: 'confirm', confirmed }));
  }, []);

  // ── Render ────────────────────────────────────────────────────────────────

  const statusDot = isConnected
    ? 'bg-green-400'
    : 'bg-gray-300 animate-pulse';

  return (
    <div className="h-full flex flex-col">
      {/* Connection status strip */}
      <div className="shrink-0 flex items-center gap-1.5 px-3 py-1 border-b border-gray-100">
        <span className={`w-1.5 h-1.5 rounded-full ${statusDot}`} />
        <span className="text-[10px] text-gray-500">
          {isConnected ? 'Connected' : 'Reconnecting…'}
        </span>
        {status === 'running' && (
          <Loader2 className="w-3 h-3 animate-spin text-blue-500 ml-auto" />
        )}
      </div>

      {/* Message history */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && (
          <p className="text-xs text-gray-400 text-center mt-4">
            Ask the AI to manage your files…
          </p>
        )}

        {messages.map(msg => (
          <div key={msg.id}>
            {/* User bubble */}
            {msg.role === 'user' && (
              <div className="flex justify-end">
                <div className="bg-blue-600 text-white rounded-2xl rounded-tr-sm px-3 py-2 max-w-[85%] text-sm">
                  {msg.content}
                </div>
              </div>
            )}

            {/* Agent reply */}
            {msg.role === 'agent' && (
              <div className="flex justify-start gap-2">
                <Sparkles className="w-4 h-4 text-blue-500 mt-1 shrink-0" />
                <div className="bg-gray-100 rounded-2xl rounded-tl-sm px-3 py-2 max-w-[85%] text-sm text-gray-900 whitespace-pre-wrap">
                  {msg.content}
                </div>
              </div>
            )}

            {/* Tool call */}
            {msg.role === 'tool_call' && msg.tool && (
              <ToolCallCard tool={msg.tool} args={msg.args ?? {}} />
            )}

            {/* Observation */}
            {msg.role === 'observation' && msg.tool && (
              <ObservationCard tool={msg.tool} result={msg.result ?? {}} />
            )}

            {/* System / cancelled */}
            {msg.role === 'system' && (
              <p className="text-center text-xs text-gray-400 italic">{msg.content}</p>
            )}

            {/* Error */}
            {msg.role === 'error' && (
              <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700">
                {msg.content}
              </div>
            )}
          </div>
        ))}

        {/* Pending confirmation card */}
        {status === 'pending' && pendingData && (
          <PendingCard
            reply={pendingData.reply}
            actions={pendingData.actions}
            onConfirm={() => sendConfirm(true)}
            onCancel={() => sendConfirm(false)}
            disabled={false}
          />
        )}

        {/* Running indicator */}
        {status === 'running' && (
          <div className="flex items-center gap-2 text-xs text-gray-400">
            <Loader2 className="w-3 h-3 animate-spin" />
            <span>Thinking…</span>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="shrink-0 border-t border-gray-200 p-2 flex gap-2">
        <textarea
          rows={2}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendRequest(); }
          }}
          placeholder={status === 'idle' ? 'Ask the AI…' : 'Waiting for agent…'}
          disabled={status !== 'idle'}
          className="flex-1 text-sm px-2 py-1 border border-gray-300 rounded resize-none focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
        />
        <button
          onClick={sendRequest}
          disabled={!input.trim() || status !== 'idle' || !isConnected}
          className="self-end p-2 bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors disabled:opacity-40"
          title="Send (Enter)"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
