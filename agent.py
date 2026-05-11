"""Tool-using agent layer for the AI file manager."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from config_loader import Config
from mainFunctions import (
    EverythingSdkSearch,
    FileOperationService,
    FolderAnalysisStore,
    format_bytes,
)
from models import create_model_client, system_message, user_message, config


JsonDict = dict[str, Any]
ToolHandler = Callable[[JsonDict, "AgentContext"], JsonDict]

DEFAULT_MAX_STEPS = config.get_max_agent_steps()
MAX_OBSERVATIONS_IN_PROMPT = 6
CONFIRMATION_REQUIRED_REPLY = "Do you confirm this action? Please reply with yes or no."
MAX_STEPS_REPLY = "Reach the maximum reasoning steps without a final answer. Please try again later"
FENCED_JSON_PATTERN = re.compile(
    r"```(?:json)?\s*(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


class AgentError(Exception):
    """Base exception for agent orchestration failures."""


class ToolNotFoundError(AgentError):
    """Raised when the model asks for a tool that is not registered."""


class ToolExecutionError(AgentError):
    """Raised when a tool call fails during local execution."""


@dataclass(frozen=True)
class AgentContext:
    """
    Runtime state supplied by the GUI before one agent run.

    `current_folder` is the folder shown by the browser, `selected_paths` is the
    current GUI selection, and `active_path` can point to the focused item. The
    agent only receives this compact snapshot, so callers stay in control of
    what filesystem state is exposed to the model.
    """

    current_folder: str = ""
    selected_paths: tuple[str, ...] = ()
    active_path: str = ""
    search_query: str = ""
    extra: JsonDict = field(default_factory=dict)

    def to_prompt_dict(self) -> JsonDict:
        """Return a JSON-safe context dictionary for the model prompt."""
        return {
            "current_folder": self.current_folder,
            "selected_paths": list(self.selected_paths),
            "active_path": self.active_path,
            "search_query": self.search_query,
            "extra": self.extra,
        }


@dataclass(frozen=True)
class ToolDefinition:
    """
    Local metadata and handler for one agent tool.

    The model sees only the serializable fields returned by `to_prompt_dict`.
    `requires_confirmation` is enforced by local code, not by the model, so
    write operations can be collected for user approval before execution.
    """

    name: str
    description: str
    arguments: JsonDict
    requires_confirmation: bool
    handler: ToolHandler

    def to_prompt_dict(self) -> JsonDict:
        """Return a compact tool schema for the model prompt."""
        return {
            "name": self.name,
            "description": self.description,
            "arguments": self.arguments,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True)
class ToolCall:
    """
    Parsed tool call requested by the model.

    The parser accepts both `name` and `tool` for compatibility with model
    output variations. Arguments are normalized to a dictionary before any
    handler sees them.
    """

    name: str
    arguments: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolObservation:
    """
    Result returned by one executed read-only or confirmed tool call.

    Observations are fed back into the next model turn for multi-step tasks and
    are also returned to the GUI so the UI can display what happened.
    """

    name: str
    arguments: JsonDict
    result: JsonDict


@dataclass(frozen=True)
class PendingAction:
    """
    Write operation waiting for user confirmation.

    The agent creates these for tools such as delete, move, copy, and create
    folder. They are inert data until the caller passes them to
    `execute_pending_actions`.
    """

    tool: str
    arguments: JsonDict
    reason: str = ""


@dataclass(frozen=True)
class AgentResult:
    """
    Final result of one agent run.

    `reply` is safe to show to the user. `pending_actions` tells the GUI which
    write operations need confirmation. `raw_model_output` is kept for
    debugging provider behavior without changing the normal result shape.
    """

    reply: str
    observations: tuple[ToolObservation, ...] = ()
    pending_actions: tuple[PendingAction, ...] = ()
    raw_model_output: str = ""

    @property
    def needs_confirmation(self) -> bool:
        """Return whether the run produced write actions awaiting approval."""
        return bool(self.pending_actions)


class FileManagerAgent:
    """
    Coordinates model planning with local file-manager tools.

    The model can request tools by returning JSON. Read-only tools execute
    immediately and their observations are sent back to the model. Tools marked
    with `requires_confirmation` are never executed inside `run`; they are
    returned as `PendingAction` objects so the GUI can ask the user first.
    """

    def __init__(
        self,
        model_client: Any | None = None,
        config: Config | None = None,
        search_engine: EverythingSdkSearch | None = None,
        analysis_store: FolderAnalysisStore | None = None,
        file_operations: FileOperationService | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ):
        """Create an agent with injectable dependencies for testing and UI use."""
        self.config = config or Config()
        self.model_client = model_client or create_model_client(runtime_config=self.config)
        self.search_engine = search_engine or EverythingSdkSearch()
        self.analysis_store = analysis_store or FolderAnalysisStore()
        self.file_operations = file_operations or FileOperationService()
        self.max_steps = max_steps
        self.everything_started = False
        self.tools = self.build_tools()

    def build_tools(self) -> dict[str, ToolDefinition]:
        """Create the local tool registry exposed to the model."""
        tools = (
            ToolDefinition(
                name="list_folder",
                description="List files and folders inside a folder.",
                arguments={"path": "folder path, optional", "limit": "max shown entries"},
                requires_confirmation=False,
                handler=self.tool_list_folder,
            ),
            ToolDefinition(
                name="inspect_path",
                description="Get metadata for one file or folder.",
                arguments={"path": "file or folder path"},
                requires_confirmation=False,
                handler=self.tool_inspect_path,
            ),
            ToolDefinition(
                name="read_text_file",
                description="Read a text file with a character limit.",
                arguments={"path": "file path", "max_chars": "maximum characters"},
                requires_confirmation=False,
                handler=self.tool_read_text_file,
            ),
            ToolDefinition(
                name="search_files",
                description="Search files with Everything.",
                arguments={"query": "Everything search query", "limit": "max shown results"},
                requires_confirmation=False,
                handler=self.tool_search_files,
            ),
            ToolDefinition(
                name="analyse_folder",
                description="Analyse recursive folder size and save the result.",
                arguments={"path": "folder path"},
                requires_confirmation=False,
                handler=self.tool_analyse_folder,
            ),
            ToolDefinition(
                name="get_folder_analysis",
                description="Read saved folder analysis for one folder.",
                arguments={"path": "folder path"},
                requires_confirmation=False,
                handler=self.tool_get_folder_analysis,
            ),
            ToolDefinition(
                name="copy_paths",
                description="Copy files or folders to a destination folder.",
                arguments={"paths": "list of source paths", "destination": "folder path"},
                requires_confirmation=True,
                handler=self.tool_copy_paths,
            ),
            ToolDefinition(
                name="move_paths",
                description="Move files or folders to a destination folder.",
                arguments={"paths": "list of source paths", "destination": "folder path"},
                requires_confirmation=True,
                handler=self.tool_move_paths,
            ),
            ToolDefinition(
                name="delete_paths",
                description="Move files or folders to the app trash for undo.",
                arguments={"paths": "list of paths"},
                requires_confirmation=True,
                handler=self.tool_delete_paths,
            ),
            ToolDefinition(
                name="create_folder",
                description="Create a new folder.",
                arguments={"path": "new folder path"},
                requires_confirmation=True,
                handler=self.tool_create_folder,
            ),
        )
        return {tool.name: tool for tool in tools}

    def run(self, user_request: str, context: AgentContext | None = None) -> AgentResult:
        """Run the agent loop for one user request."""
        active_context = context or AgentContext()
        messages = self.initial_messages(user_request, active_context)
        observations: list[ToolObservation] = []
        pending_actions: list[PendingAction] = []
        raw_output = ""

        for _step in range(self.max_steps):
            raw_output = self.ask_model(messages)
            decision = self.parse_model_output(raw_output)
            tool_calls = self.parse_tool_calls(decision)

            if not tool_calls:
                return self.final_result(decision, observations, pending_actions, raw_output)

            messages.append(self.assistant_message(raw_output))
            for tool_call in tool_calls:
                tool = self.get_tool(tool_call.name)
                if tool.requires_confirmation:
                    pending_actions.append(self.pending_action(tool_call, decision))
                else:
                    observations.append(self.execute_tool_call(tool_call, active_context))

            if pending_actions:
                return self.final_result(decision, observations, pending_actions, raw_output)

            messages.append(user_message(self.observations_prompt(observations)))

        return AgentResult(
            reply=MAX_STEPS_REPLY,
            observations=tuple(observations),
            pending_actions=tuple(pending_actions),
            raw_model_output=raw_output,
        )

    def initial_messages(self, user_request: str, context: AgentContext) -> list[JsonDict]:
        """Build the first system and user messages sent to the model."""
        return [
            system_message(self.system_prompt()),
            user_message(self.user_prompt(user_request, context)),
        ]

    def ask_model(self, messages: list[JsonDict]) -> str:
        """Call the model client and return only assistant content."""
        response = self.model_client.chat(messages)
        return response.content

    @staticmethod
    def assistant_message(content: str) -> JsonDict:
        """Create an assistant chat message."""
        return {"role": "assistant", "content": content}

    @staticmethod
    def pending_action(tool_call: ToolCall, decision: JsonDict) -> PendingAction:
        """Convert a confirmation-required tool call into inert action data."""
        return PendingAction(
            tool=tool_call.name,
            arguments=tool_call.arguments,
            reason=str(decision.get("reply") or ""),
        )

    @staticmethod
    def final_result(
        decision: JsonDict,
        observations: list[ToolObservation],
        pending_actions: list[PendingAction],
        raw_output: str,
    ) -> AgentResult:
        """Build a consistent AgentResult from the current loop state."""
        default_reply = CONFIRMATION_REQUIRED_REPLY if pending_actions else raw_output
        return AgentResult(
            reply=str(decision.get("reply") or default_reply),
            observations=tuple(observations),
            pending_actions=tuple(pending_actions),
            raw_model_output=raw_output,
        )

    def execute_pending_actions(
        self,
        pending_actions: tuple[PendingAction, ...] | list[PendingAction],
        context: AgentContext | None = None,
    ) -> tuple[ToolObservation, ...]:
        """Execute write actions after the caller has confirmed them."""
        active_context = context or AgentContext()
        observations = []
        for action in pending_actions:
            observations.append(
                self.execute_tool_call(
                    ToolCall(name=action.tool, arguments=action.arguments),
                    active_context,
                    allow_confirmation_tools=True,
                )
            )
        return tuple(observations)

    def execute_tool_call(
        self,
        tool_call: ToolCall,
        context: AgentContext,
        allow_confirmation_tools: bool = False,
    ) -> ToolObservation:
        """Execute one tool call and wrap the result as an observation."""
        tool = self.get_tool(tool_call.name)
        if tool.requires_confirmation and not allow_confirmation_tools:
            raise ToolExecutionError(f"Tool '{tool.name}' requires confirmation.")

        try:
            result = tool.handler(tool_call.arguments, context)
        except Exception as error:
            raise ToolExecutionError(f"{tool.name} failed: {error}") from error

        return ToolObservation(
            name=tool_call.name,
            arguments=tool_call.arguments,
            result=result,
        )

    def get_tool(self, name: str) -> ToolDefinition:
        """Return a tool definition by name."""
        try:
            return self.tools[name]
        except KeyError as error:
            raise ToolNotFoundError(f"Unknown tool: {name}") from error

    def system_prompt(self) -> str:
        """Build the system prompt used by the agent."""
        return (
            f"{self.config.get_system_prompt()}\n"
            "You can use tools to inspect and manage files.\n"
            "Return only JSON with this shape:\n"
            "{"
            "\"reply\": \"short user-facing text\", "
            "\"tool_calls\": [{\"name\": \"tool_name\", \"arguments\": {}}]"
            "}\n"
            "Use read-only tools freely. For write tools, explain the plan; the app will ask for confirmation.\n"
            "Never invent paths. Prefer paths from context, tool observations, or explicit user input."
        )

    def user_prompt(self, user_request: str, context: AgentContext) -> str:
        """Build the user prompt containing task, context, and tools."""
        payload = {
            "user_request": user_request,
            "context": context.to_prompt_dict(),
            "tools": [tool.to_prompt_dict() for tool in self.tools.values()],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def observations_prompt(self, observations: list[ToolObservation]) -> str:
        """Build a prompt containing recent tool observations."""
        payload = {
            "tool_observations": [
                {
                    "name": observation.name,
                    "arguments": observation.arguments,
                    "result": observation.result,
                }
                for observation in observations[-MAX_OBSERVATIONS_IN_PROMPT:]
            ],
            "instruction": "Use these observations to continue or produce the final reply JSON.",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def parse_model_output(self, text: str) -> JsonDict:
        """Parse model JSON output, tolerating reasoning text and fenced blocks."""
        for candidate in self.extract_json_texts(text):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue

            if isinstance(data, dict):
                return data

        return {"reply": text, "tool_calls": []}

    def extract_json_texts(self, text: str) -> list[str]:
        """Extract possible JSON object strings from model output."""
        stripped = text.strip()
        candidates = []

        for match in FENCED_JSON_PATTERN.finditer(stripped):
            block = match.group(1).strip()
            if block:
                candidates.append(block)

        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)

        candidates.extend(self.raw_decode_json_objects(stripped))
        return self.unique_candidates(candidates) or [stripped]

    @staticmethod
    def raw_decode_json_objects(text: str) -> list[str]:
        """Find JSON objects embedded inside arbitrary text."""
        decoder = json.JSONDecoder()
        objects = []
        start = text.find("{")
        while start >= 0:
            try:
                data, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                start = text.find("{", start + 1)
                continue

            if isinstance(data, dict):
                objects.append(text[start : start + end])
            start = text.find("{", start + 1)
        return objects

    @staticmethod
    def unique_candidates(candidates: list[str]) -> list[str]:
        """Remove duplicate candidate strings while preserving order."""
        result = []
        seen = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                result.append(candidate)
                seen.add(candidate)
        return result

    def parse_tool_calls(self, decision: JsonDict) -> list[ToolCall]:
        """Convert model tool call JSON into ToolCall objects."""
        tool_calls = decision.get("tool_calls") or []
        if isinstance(tool_calls, dict):
            tool_calls = [tool_calls]
        if not isinstance(tool_calls, list):
            return []

        parsed = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name") or item.get("tool") or "").strip()
            if not name:
                continue

            arguments = item.get("arguments") or item.get("args") or {}
            if not isinstance(arguments, dict):
                arguments = {}

            parsed.append(ToolCall(name=name, arguments=arguments))

        return parsed

    def resolve_path(self, path_text: str | None, context: AgentContext) -> Path:
        """Resolve a path against current_folder when it is relative."""
        raw_path = str(path_text or "").strip()
        if not raw_path:
            raw_path = context.active_path or context.current_folder or "."

        path = Path(raw_path).expanduser()
        if not path.is_absolute() and context.current_folder:
            path = Path(context.current_folder) / path

        return path.resolve(strict=False)

    def normalize_paths(self, values: Any, context: AgentContext) -> list[str]:
        """Normalize a model-supplied path argument into absolute path strings."""
        if values is None:
            values = context.selected_paths
        elif isinstance(values, str):
            values = [values]
        elif not isinstance(values, (list, tuple, set)):
            values = [values]

        return [str(self.resolve_path(value, context)) for value in values]

    def tool_list_folder(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """List files and folders inside a folder."""
        folder = self.resolve_path(arguments.get("path"), context)
        limit = max(1, int(arguments.get("limit") or 100))
        if not folder.is_dir():
            raise NotADirectoryError(str(folder))

        children = list(folder.iterdir())
        child_sizes = self.analysis_store.child_folder_size_map(folder)
        entries = [
            self.path_summary(child, child_sizes)
            for child in children[:limit]
        ]

        return {
            "path": str(folder),
            "total": len(children),
            "shown": len(entries),
            "entries": entries,
        }

    def tool_inspect_path(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Inspect one file or folder."""
        path = self.resolve_path(arguments.get("path"), context)
        return self.path_summary(path, {})

    def tool_read_text_file(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Read a text file with a character limit."""
        path = self.resolve_path(arguments.get("path"), context)
        max_chars = max(1, int(arguments.get("max_chars") or 12000))
        encoding = str(arguments.get("encoding") or "utf-8")
        if not path.is_file():
            raise FileNotFoundError(str(path))

        text = path.read_text(encoding=encoding, errors="replace")
        return {
            "path": str(path),
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
            "characters": len(text),
        }

    def tool_search_files(self, arguments: JsonDict, _context: AgentContext) -> JsonDict:
        """Search files through Everything SDK."""
        query = str(arguments.get("query") or "").strip()
        limit = max(1, int(arguments.get("limit") or 100))
        if not query:
            raise ValueError("query is required")

        startup_message = ""
        if not self.everything_started:
            startup_message = self.search_engine.start()
            self.everything_started = True

        paths, message = self.search_engine.search(query)
        return {
            "query": query,
            "startup_message": startup_message,
            "message": message,
            "total_results": self.search_engine.total_result_count(),
            "shown": min(len(paths), limit),
            "paths": paths[:limit],
        }

    def tool_analyse_folder(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Analyse recursive folder size and persist the result."""
        folder = self.resolve_path(arguments.get("path"), context)
        record = self.analysis_store.analyse_and_store(folder)
        return {
            "root_path": record.root_path,
            "folder_path": record.folder_path,
            "size_bytes": record.size_bytes,
            "size": format_bytes(record.size_bytes),
            "file_count": record.file_count,
            "folder_count": record.folder_count,
            "error_count": record.error_count,
            "analysed_at": record.analysed_at,
        }

    def tool_get_folder_analysis(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Return saved folder analysis for one folder."""
        folder = self.resolve_path(arguments.get("path"), context)
        record = self.analysis_store.folder_summary(folder)
        child_sizes = self.analysis_store.child_folder_size_map(folder)
        return {
            "path": str(folder),
            "summary": self.format_analysis_record(record) if record else None,
            "child_sizes": {
                path: {
                    "size_bytes": size,
                    "size": format_bytes(size),
                }
                for path, size in child_sizes.items()
            },
        }

    def tool_copy_paths(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Copy paths to a destination folder."""
        paths = self.normalize_paths(arguments.get("paths"), context)
        destination = self.resolve_path(arguments.get("destination"), context)
        return self.file_operations.paste(paths, destination, move=False)

    def tool_move_paths(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Move paths to a destination folder."""
        paths = self.normalize_paths(arguments.get("paths"), context)
        destination = self.resolve_path(arguments.get("destination"), context)
        return self.file_operations.paste(paths, destination, move=True)

    def tool_delete_paths(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Move paths to the app trash for undo."""
        paths = self.normalize_paths(arguments.get("paths"), context)
        return self.file_operations.delete_for_undo(paths)

    def tool_create_folder(self, arguments: JsonDict, context: AgentContext) -> JsonDict:
        """Create a folder."""
        path = self.resolve_path(arguments.get("path"), context)
        path.mkdir(parents=bool(arguments.get("parents", True)), exist_ok=False)
        return {"path": str(path), "created": True}

    def path_summary(self, path: Path, folder_size_map: dict[str, int]) -> JsonDict:
        """Return a JSON-safe metadata summary for one path."""
        exists = path.exists()
        result: JsonDict = {
            "path": str(path),
            "name": path.name or str(path),
            "exists": exists,
        }
        if not exists:
            return result

        stat = path.stat()
        is_dir = path.is_dir()
        size_bytes = folder_size_map.get(FolderAnalysisStore.normalized_path(path))
        if size_bytes is None and path.is_file():
            size_bytes = stat.st_size

        result.update(
            {
                "type": "folder" if is_dir else "file",
                "is_dir": is_dir,
                "is_file": path.is_file(),
                "suffix": path.suffix,
                "size_bytes": size_bytes,
                "size": format_bytes(size_bytes) if size_bytes is not None else "",
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
        return result

    @staticmethod
    def format_analysis_record(record: JsonDict) -> JsonDict:
        """Return a saved analysis row with a human-readable size field."""
        data = dict(record)
        data["size"] = format_bytes(int(data.get("size_bytes") or 0))
        return data


def run_agent(user_request: str, context: AgentContext | None = None) -> AgentResult:
    """Run the default file-manager agent once."""
    return FileManagerAgent().run(user_request, context)
