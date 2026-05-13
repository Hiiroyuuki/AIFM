"""Configuration loading and provider selection for AIFM."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"


class ConfigError(Exception):
    """Base exception for configuration failures."""


class ConfigFileError(ConfigError):
    """Raised when config.json cannot be loaded or parsed."""


class ModelProviderError(ConfigError):
    """Base exception for model provider configuration failures."""


class UnknownProviderError(ModelProviderError):
    """Raised when a requested model provider is not configured."""


class MissingApiKeyError(ModelProviderError):
    """Raised when a provider has no usable API key."""


@dataclass(frozen=True)
class ProviderSpec:
    """
    Normalized connection settings for one model provider.

    Config stores one API key string per model provider. Model names may still
    be provided as a string or list, and may be direct values or environment
    variable names.
    """

    name: str
    aliases: tuple[str, ...]
    base_url: str
    api_key: str
    models: tuple[str, ...]
    default_model: str

    @property
    def default_or_first_model(self) -> str:
        """Return default_model, falling back to the first configured model."""
        return self.default_model or (self.models[0] if self.models else "")


class Config:
    """
    Owns config.json loading and typed accessors used by the app.

    The rest of the project should not need to know the raw layout of
    config.json. This class keeps provider lookup, Everything settings, system
    prompt access, and analysis database settings in one place.
    """

    def __init__(self, path: str | Path | None = None):
        """Load config.json and build provider lookup indexes."""
        self.path = Path(path).resolve() if path else DEFAULT_CONFIG_PATH
        self.config = self._load_json(self.path)
        self.providers = self._build_providers()
        self.provider_by_alias = self._build_provider_alias_map(self.providers)

    @staticmethod
    def _load_json(path: Path) -> JsonDict:
        """Read a JSON file as a dictionary."""
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except OSError as error:
            raise ConfigFileError(f"Cannot read config file: {path}") from error
        except json.JSONDecodeError as error:
            raise ConfigFileError(f"Invalid JSON in config file: {path}") from error

        if not isinstance(data, dict):
            raise ConfigFileError(f"Config root must be a JSON object: {path}")
        return data

    def _build_providers(self) -> tuple[ProviderSpec, ...]:
        """Build provider specs from the `models` section of config.json."""
        model_configs = self.config.get("models") or {}
        if not isinstance(model_configs, dict):
            return ()

        specs = []
        for name, values in model_configs.items():
            if not isinstance(values, dict):
                continue

            canonical_name = self.normalize_provider_key(name)
            specs.append(
                ProviderSpec(
                    name=canonical_name,
                    aliases=self._read_values(values, "aliases", normalize=True),
                    base_url=self._read_string(values.get("base_url")),
                    api_key=self._read_api_key(values),
                    models=self._unique_values(
                        self._read_values(values, "models")
                        + self._read_values(values, "model_envs", resolve_env=True)
                    ),
                    default_model=self._read_string(values.get("default_model")),
                )
            )

        return tuple(specs)

    @classmethod
    def _read_api_key(cls, source: JsonDict) -> str:
        """Read the configured API key string for one provider."""
        return cls._read_string(source.get("api_key"))

    @classmethod
    def _build_provider_alias_map(
        cls,
        providers: tuple[ProviderSpec, ...],
    ) -> dict[str, ProviderSpec]:
        """Return a lookup map for canonical provider names and aliases."""
        aliases = {}
        for spec in providers:
            for alias in (spec.name, *spec.aliases):
                if alias:
                    aliases[cls.normalize_provider_key(alias)] = spec
        return aliases

    @staticmethod
    def _read_string(value: Any, default: str = "") -> str:
        """Read a single config value as a stripped string."""
        if value is None:
            return default
        return str(value).strip()

    @classmethod
    def _read_values(
        cls,
        source: JsonDict,
        key: str,
        normalize: bool = False,
        resolve_env: bool = False,
    ) -> tuple[str, ...]:
        """Read a config value as a tuple of strings."""
        raw_value = source.get(key)
        if raw_value is None:
            return ()

        if isinstance(raw_value, (list, tuple, set)):
            values = raw_value
        else:
            values = (raw_value,)

        result = []
        for value in values:
            text = cls._read_string(value)
            if not text:
                continue
            if resolve_env:
                text = os.getenv(text, text).strip()
            if normalize:
                text = cls.normalize_provider_key(text)
            result.append(text)

        return cls._unique_values(result)

    @staticmethod
    def _unique_values(values: Any) -> tuple[str, ...]:
        """Return strings without blanks or duplicates while preserving order."""
        result = []
        seen = set()
        for value in values:
            text = str(value).strip()
            if text and text not in seen:
                result.append(text)
                seen.add(text)
        return tuple(result)

    @staticmethod
    def normalize_provider_key(value: Any) -> str:
        """Normalize provider names and aliases for lookup."""
        return str(value).strip().lower().replace("-", "_")

    def get_config(self) -> JsonDict:
        """Return the raw config dictionary for callers that need it."""
        return self.config

    def get_system_prompt(self, default: str = "You are a file manager assistant.") -> str:
        """Return the configured system prompt."""
        return self._read_string(self.config.get("system_prompt"), default)

    def available_providers(self) -> list[str]:
        """Return canonical provider names from config.json."""
        return [spec.name for spec in self.providers]

    def preferred_provider_name(self, provider: str | None = None) -> str:
        """Return an explicit provider or the provider selected by config."""
        if provider:
            return self._read_string(provider)

        candidates = [
            self.config.get("provider"),
            self.config.get("model_provider"),
            self.config.get("default_provider"),
        ]

        preference = self.config.get("preference") or {}
        if isinstance(preference, dict):
            candidates.extend(
                [
                    preference.get("provider"),
                    preference.get("model_provider"),
                    preference.get("default_provider"),
                ]
            )

        candidates.append(self.config.get("model"))
        for candidate in candidates:
            provider_name = self._read_string(candidate)
            if provider_name:
                return provider_name

        if self.providers:
            return self.providers[0].name

        raise UnknownProviderError("No provider is configured in config.json.")

    def get_provider_spec(self, provider: str | None = None) -> ProviderSpec:
        """Return provider settings by explicit name, alias, or config default."""
        provider_name = self.preferred_provider_name(provider)
        key = self.normalize_provider_key(provider_name)
        try:
            return self.provider_by_alias[key]
        except KeyError as error:
            providers = ", ".join(self.available_providers()) or "<none>"
            raise UnknownProviderError(
                f"Unknown provider '{provider_name}'. Available: {providers}"
            ) from error

    def get_default_analysis_db(self) -> str:
        """Return the SQLite database path used for folder analysis results."""
        return self._read_string(
            self.config.get("default_analysis_db"),
            "folder_analysis.sqlite3",
        )

    def get_everything_limit(self) -> int:
        """Return the maximum Everything SDK result count."""
        return self._read_int("everything_result_limit", default=-1)

    def get_everything_timeout(self) -> int:
        """Return the Everything startup timeout in seconds."""
        return self._read_int(
            "everything_timeout",
            "everything_timeout_seconds",
            default=6,
        )
    
    def get_max_agent_steps(self) -> int:
        """Return the maximum reasoning steps for agents."""
        return self._read_int("max_agent_steps", default=4)

    def get_default_arrange(self) -> str:
        """Return the default file-browser sort mode."""
        value = self.config.get("default_arrange")
        preference = self.config.get("preference") or {}
        if not value and isinstance(preference, dict):
            value = preference.get("default_arrange")

        if isinstance(value, (list, tuple, set)):
            value = next(iter(value), "")

        return self._read_string(value, "name_asc")

    def get_file_type_names(self) -> dict[str, str]:
        """Return configured extension display names."""
        values = self.config.get("file_type_names") or self.config.get("file_type_name")
        if not isinstance(values, dict):
            return {}

        result = {}
        for extension, name in values.items():
            extension_text = self._read_string(extension).lower()
            if not extension_text.startswith(".") or not isinstance(name, str):
                continue
            result[extension_text] = name

        return result

    def _read_int(self, *keys: str, default: int) -> int:
        """Read the first available integer from config.json."""
        for key in keys:
            if key not in self.config:
                continue
            try:
                return int(self.config[key])
            except (TypeError, ValueError):
                return default
        return default
