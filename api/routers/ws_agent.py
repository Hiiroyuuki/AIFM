"""WebSocket endpoint for the AI file-manager agent — with per-step streaming.

Protocol (JSON frames):

  Client → Server:
    { "type": "start",   "request": "...", "context": { ... } }
    { "type": "confirm", "confirmed": true | false }

  Server → Client (streaming):
    { "type": "thinking" }                        – model API call in progress
    { "type": "step",     "preview": "..." }      – first 300 chars of raw output
    { "type": "tool_call","name":"...","arguments":{...} }
    { "type": "observation","name":"...","result":{...} }
    { "type": "pending",  "reply":"...","actions":[...],"observations":[...] }
    { "type": "done",     "reply":"...","observations":[...] }
    { "type": "cancelled" }
    { "type": "error",    "message":"..." }

Streaming is achieved by subclassing FileManagerAgent to push events into a
SimpleQueue that an async drain-loop forwards to the WebSocket in real time.
"""
from __future__ import annotations

import asyncio
import json
from queue import Empty, SimpleQueue
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.state import state

router = APIRouter(tags=["agent"])


# ── Streaming mixin ──────────────────────────────────────────────────────────

class _StreamingMixin:
    """Override ask_model / execute_tool_call to emit events into self._eq."""

    _eq: SimpleQueue  # set on the instance before use

    def ask_model(self, messages: list) -> str:  # type: ignore[override]
        self._eq.put({"type": "thinking"})
        raw: str = super().ask_model(messages)  # type: ignore[misc]
        self._eq.put({"type": "step", "preview": raw[:300]})
        return raw

    def execute_tool_call(
        self,
        tool_call: Any,
        context: Any,
        allow_confirmation_tools: bool = False,
    ) -> Any:
        self._eq.put({
            "type": "tool_call",
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        })
        obs = super().execute_tool_call(  # type: ignore[misc]
            tool_call, context, allow_confirmation_tools
        )
        # Trim large text fields so the WS frame stays compact.
        result: Any = obs.result
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            if len(result["content"]) > 500:
                result = {**result, "content": result["content"][:500] + "…[truncated]"}
        self._eq.put({"type": "observation", "name": obs.name, "result": result})
        return obs


# ── Async drain helper ───────────────────────────────────────────────────────

async def _drain_until_done(
    task: asyncio.Task,
    eq: SimpleQueue,
    ws: WebSocket,
) -> Any:
    """Forward all queued events to the WebSocket until the task completes."""
    while True:
        # Eagerly drain the queue.
        while not eq.empty():
            try:
                await ws.send_json(eq.get_nowait())
            except Empty:
                break
        if task.done():
            # One final drain for any events emitted right before completion.
            while not eq.empty():
                try:
                    await ws.send_json(eq.get_nowait())
                except Empty:
                    break
            break
        await asyncio.sleep(0.02)   # 20 ms polling interval

    exc = task.exception()
    if exc:
        raise exc
    return task.result()


# ── Helper ───────────────────────────────────────────────────────────────────

def _obs_list(observations: Any) -> list[dict]:
    return [
        {"name": o.name, "arguments": o.arguments, "result": o.result}
        for o in observations
    ]


# ── WebSocket handler ─────────────────────────────────────────────────────────

@router.websocket("/ws/agent")
async def agent_ws(websocket: WebSocket) -> None:
    await websocket.accept()

    # Lazy import so a broken agent module only fails this endpoint.
    try:
        from agent import AgentContext, FileManagerAgent
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": f"Agent import failed: {exc}"})
        await websocket.close()
        return

    # Build a per-connection streaming subclass.
    eq: SimpleQueue = SimpleQueue()
    StreamingAgent = type("StreamingAgent", (_StreamingMixin, FileManagerAgent), {})
    agent = StreamingAgent(
        config=state.config,
        search_engine=state.search_engine,
        analysis_store=state.analysis_store,
        file_operations=state.file_operations,
    )
    agent._eq = eq

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON frame."})
                continue

            if msg.get("type") != "start":
                await websocket.send_json({
                    "type": "error",
                    "message": f"Expected 'start', got {msg.get('type')!r}.",
                })
                continue

            request: str = str(msg.get("request") or "")
            ctx_raw: dict = msg.get("context") or {}
            context = AgentContext(
                current_folder=str(ctx_raw.get("current_folder") or ""),
                selected_paths=tuple(ctx_raw.get("selected_paths") or []),
                active_path=str(ctx_raw.get("active_path") or ""),
                search_query=str(ctx_raw.get("search_query") or ""),
                extra=dict(ctx_raw.get("extra") or {}),
            )

            # ── Run agent with streaming ─────────────────────────────────────
            agent_task = asyncio.create_task(
                asyncio.to_thread(agent.run, request, context)
            )
            try:
                result = await _drain_until_done(agent_task, eq, websocket)
            except Exception as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
                continue

            if result.needs_confirmation:
                await websocket.send_json({
                    "type": "pending",
                    "reply": result.reply,
                    "actions": [
                        {"tool": a.tool, "arguments": a.arguments, "reason": a.reason}
                        for a in result.pending_actions
                    ],
                    "observations": _obs_list(result.observations),
                })

                # Wait for the confirm/cancel frame.
                confirm_raw = await websocket.receive_text()
                try:
                    confirm = json.loads(confirm_raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid confirm frame."})
                    continue

                if confirm.get("type") == "confirm" and confirm.get("confirmed"):
                    exec_task = asyncio.create_task(
                        asyncio.to_thread(
                            agent.execute_pending_actions,
                            result.pending_actions,
                            context,
                        )
                    )
                    try:
                        exec_obs = await _drain_until_done(exec_task, eq, websocket)
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                        continue

                    await websocket.send_json({
                        "type": "done",
                        "reply": result.reply,
                        "observations": _obs_list(exec_obs),
                    })
                else:
                    await websocket.send_json({"type": "cancelled"})

            else:
                await websocket.send_json({
                    "type": "done",
                    "reply": result.reply,
                    "observations": _obs_list(result.observations),
                })

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
