"""Unified OpenAI-compatible model clients for AIFM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config_loader import (
    Config,
    MissingApiKeyError,
    ModelProviderError,
    ProviderSpec,
    UnknownProviderError,
)


Message = dict[str, Any]
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CONFIG = Config()
config = DEFAULT_CONFIG
CONFIG = DEFAULT_CONFIG.get_config()


class ModelRequestError(ModelProviderError):
    """Raised when a provider HTTP request fails or returns invalid data."""


@dataclass(frozen=True)
class ModelResponse:
    """
    Normalized chat response returned by every provider client.

    The raw provider payload is kept for debugging and future UI display, while
    `content` is the plain assistant text used by the agent and simple callers.
    """

    provider: str
    model: str
    content: str
    raw: dict[str, Any]
    usage: dict[str, Any]


class OpenAICompatibleClient:
    """
    Minimal client for providers exposing `/chat/completions`.

    MiniMax, Kimi, DeepSeek, OpenAI, and many other vendors can be reached with
    this shape as long as config.json provides base_url, API key, and model.
    The class owns request construction, error formatting, and response
    normalization; it does not decide which provider should be used.
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ):
        """Create a client from explicit connection settings."""
        if not api_key:
            raise MissingApiKeyError(f"{provider} API key is empty.")
        if not base_url:
            raise ModelProviderError(f"{provider} base_url is empty.")
        if not default_model:
            raise ModelProviderError(f"{provider} default_model is empty.")

        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    @property
    def chat_completions_url(self) -> str:
        """Return the provider chat completions endpoint."""
        return f"{self.base_url}/chat/completions"

    def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> ModelResponse:
        """Send chat messages and return a normalized response."""
        if stream:
            raise NotImplementedError("Streaming is not implemented yet.")

        selected_model = model or self.default_model
        payload = self.build_payload(
            messages=messages,
            model=selected_model,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        data = self.post_json(payload)

        return ModelResponse(
            provider=self.provider,
            model=str(data.get("model") or selected_model),
            content=self.extract_content(data),
            raw=data,
            usage=data.get("usage") or {},
        )

    @staticmethod
    def build_payload(
        messages: list[Message],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible request body."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            payload.update(extra_body)
        return payload

    def post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to the provider and parse the response as a dictionary."""
        try:
            response = self.session.post(
                self.chat_completions_url,
                headers=self.headers(),
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise ModelRequestError(f"{self.provider} request failed: {error}") from error

        if response.status_code >= 400:
            raise ModelRequestError(self.error_message(response))

        try:
            data = response.json()
        except ValueError as error:
            raise ModelRequestError(
                f"{self.provider} returned non-JSON response."
            ) from error

        if not isinstance(data, dict):
            raise ModelRequestError(f"{self.provider} returned a non-object JSON response.")
        return data

    def headers(self) -> dict[str, str]:
        """Return HTTP headers shared by supported providers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def error_message(self, response: requests.Response) -> str:
        """Build a readable error message without exposing the API key."""
        try:
            data = response.json()
        except ValueError:
            return (
                f"{self.provider} error {response.status_code}: "
                f"{response.text[:300]}"
            )

        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or error.get("type") or error
        else:
            message = data

        return f"{self.provider} error {response.status_code}: {message}"

    @staticmethod
    def extract_content(data: dict[str, Any]) -> str:
        """Extract the first assistant message content from a chat response."""
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""

        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            return ""

        content = message.get("content") or ""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    parts.append(str(part["text"]))
            return "".join(parts)

        return str(content)


def first_value(values: tuple[str, ...]) -> str:
    """Return the first configured non-empty value."""
    return values[0] if values else ""


def create_model_client(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runtime_config: Config | None = None,
) -> OpenAICompatibleClient:
    """Create a model client from config.json with optional runtime overrides."""
    active_config = runtime_config or DEFAULT_CONFIG
    spec = active_config.get_provider_spec(provider)
    resolved_api_key = api_key or spec.default_api_key
    resolved_model = model or spec.default_or_first_model

    if not resolved_api_key:
        raise MissingApiKeyError(f"No API key configured for provider '{spec.name}'.")

    return client_from_spec(
        spec=spec,
        api_key=resolved_api_key,
        model=resolved_model,
        base_url=base_url or spec.base_url,
        timeout_seconds=timeout_seconds,
    )


def client_from_spec(
    spec: ProviderSpec,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OpenAICompatibleClient:
    """Create an OpenAICompatibleClient from a normalized provider spec."""
    return OpenAICompatibleClient(
        provider=spec.name,
        api_key=api_key,
        base_url=base_url,
        default_model=model,
        timeout_seconds=timeout_seconds,
    )


def user_message(content: str) -> Message:
    """Create a user chat message."""
    return {"role": "user", "content": content}


def system_message(content: str) -> Message:
    """Create a system chat message."""
    return {"role": "system", "content": content}


def chat_text(
    prompt: str,
    provider: str | None = None,
    system: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_body: dict[str, Any] | None = None,
    runtime_config: Config | None = None,
) -> str:
    """Send a one-turn prompt and return only the assistant text."""
    messages = []
    if system:
        messages.append(system_message(system))
    messages.append(user_message(prompt))

    client = create_model_client(
        provider=provider,
        model=model,
        runtime_config=runtime_config,
    )
    response = client.chat(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )
    return response.content


__all__ = [
    "CONFIG",
    "DEFAULT_CONFIG",
    "Message",
    "MissingApiKeyError",
    "ModelProviderError",
    "ModelRequestError",
    "ModelResponse",
    "OpenAICompatibleClient",
    "UnknownProviderError",
    "chat_text",
    "client_from_spec",
    "config",
    "create_model_client",
    "first_value",
    "system_message",
    "user_message",
]
