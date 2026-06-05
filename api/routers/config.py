"""LLM provider configuration endpoints.

GET  /api/config/providers                        – list all configured providers
GET  /api/config/active-provider                  – get the current active provider
PUT  /api/config/active-provider                  – switch active provider
GET  /api/config/providers/{name}                 – get one provider's settings
PUT  /api/config/providers/{name}                 – update a provider's settings
POST /api/config/providers                        – add a new provider profile
GET  /api/config/providers/{name}/fetch-models    – fetch model list from provider API
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Optional

import requests as req_lib
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.state import state

router = APIRouter(prefix="/api/config", tags=["config"])


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------

class ActiveProviderRequest(BaseModel):
    provider: str


class ProviderWriteRequest(BaseModel):
    nickname: str
    api_key: str
    model: str
    base_url: Optional[str] = None


class AddProviderRequest(BaseModel):
    provider: str            # key name used in config.json models section
    nickname: str
    api_key: str
    model: str
    base_url: Optional[str] = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _write_config(config_data: dict) -> None:
    """Atomically write config.json and reload the in-memory Config."""
    path = state.config.path
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config_data, fh, ensure_ascii=False, indent=4)
            fh.write("\n")
    except OSError as exc:
        raise HTTPException(500, f"Could not save config.json: {exc}")
    state.reload_config()


def _provider_view(spec, config_data: dict) -> dict:
    """Build a safe (no raw API key) provider summary dict."""
    provider_cfg = (config_data.get("models") or {}).get(spec.name, {})
    nickname = str(provider_cfg.get("nickname") or "").strip()
    return {
        "name": spec.name,
        "aliases": list(spec.aliases),
        "base_url": spec.base_url,
        "models": list(spec.models),
        "default_model": spec.default_model,
        "api_key_configured": bool(spec.api_key),
        "nickname": nickname,
    }


# --------------------------------------------------------------------------
# GET /api/config/providers
# --------------------------------------------------------------------------

@router.get("/providers")
async def list_providers():
    """Return all providers configured in config.json (no API keys)."""
    cfg = state.config.get_config()
    return [_provider_view(spec, cfg) for spec in state.config.providers]


# --------------------------------------------------------------------------
# GET /api/config/active-provider
# --------------------------------------------------------------------------

@router.get("/active-provider")
async def get_active_provider():
    """Return the currently selected provider and model."""
    try:
        spec = state.config.get_provider_spec()
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return _provider_view(spec, state.config.get_config())


# --------------------------------------------------------------------------
# PUT /api/config/active-provider
# --------------------------------------------------------------------------

@router.put("/active-provider")
async def set_active_provider(req: ActiveProviderRequest):
    """Switch the active provider (writes config.json)."""
    config_data = copy.deepcopy(state.config.get_config())
    config_data["provider"] = req.provider.strip().lower()
    _write_config(config_data)
    return {"ok": True}


# --------------------------------------------------------------------------
# GET /api/config/providers/{name}
# --------------------------------------------------------------------------

@router.get("/providers/{name}")
async def get_provider(name: str):
    """Return settings for one named provider (no API key value)."""
    try:
        spec = state.config.get_provider_spec(name)
    except Exception as exc:
        raise HTTPException(404, str(exc))
    return _provider_view(spec, state.config.get_config())


# --------------------------------------------------------------------------
# PUT /api/config/providers/{name}
# --------------------------------------------------------------------------

@router.put("/providers/{name}")
async def update_provider(name: str, req: ProviderWriteRequest):
    """Update nickname, API key, and default model for an existing provider."""
    provider_key = name.strip().lower()
    config_data = copy.deepcopy(state.config.get_config())
    model_configs = config_data.setdefault("models", {})

    if provider_key not in model_configs:
        raise HTTPException(404, f"Provider not found: {name}")

    provider_cfg = model_configs[provider_key]
    if not isinstance(provider_cfg, dict):
        raise HTTPException(400, f"Malformed provider config for: {name}")

    provider_cfg["nickname"] = req.nickname
    provider_cfg["api_key"] = req.api_key
    provider_cfg["default_model"] = req.model
    if req.base_url:
        provider_cfg["base_url"] = req.base_url

    # Keep model in model_envs list (preserving existing entries).
    existing: list[str] = []
    for key in ("model_envs", "models"):
        val = provider_cfg.get(key)
        if isinstance(val, list):
            existing.extend(str(m).strip() for m in val if str(m).strip())
        elif isinstance(val, str) and val.strip():
            existing.append(val.strip())
    if req.model and req.model not in existing:
        existing.insert(0, req.model)
    provider_cfg["model_envs"] = existing

    _write_config(config_data)
    return {"ok": True}


# --------------------------------------------------------------------------
# POST /api/config/providers
# --------------------------------------------------------------------------

@router.post("/providers")
async def add_provider(req: AddProviderRequest):
    """Add a new provider profile to config.json."""
    provider_key = req.provider.strip().lower()
    config_data = copy.deepcopy(state.config.get_config())
    model_configs = config_data.setdefault("models", {})

    provider_cfg: dict = {
        "nickname": req.nickname,
        "api_key": req.api_key,
        "default_model": req.model,
        "model_envs": [req.model] if req.model else [],
    }
    if req.base_url:
        provider_cfg["base_url"] = req.base_url

    model_configs[provider_key] = provider_cfg
    _write_config(config_data)
    return {"ok": True, "name": provider_key}


# --------------------------------------------------------------------------
# GET /api/config/providers/{name}/fetch-models
# --------------------------------------------------------------------------

def _fetch_models_sync(base_url: str, api_key: str) -> list[str]:
    """Blocking HTTP call to retrieve the provider's available model list."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        response = req_lib.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
    except req_lib.RequestException as exc:
        raise ValueError(str(exc))

    if response.status_code >= 400:
        raise ValueError(f"HTTP {response.status_code}: {response.text[:200]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Provider returned non-JSON response.") from exc

    if isinstance(payload, dict):
        items = (
            payload.get("data")
            or payload.get("models")
            or payload.get("items")
            or []
        )
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ("id", "name", "model"):
                value = str(item.get(key) or "").strip()
                if value:
                    names.append(value)
                    break

    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        n = n.strip()
        if n and n not in seen:
            unique.append(n)
            seen.add(n)
    return sorted(unique, key=str.casefold)


# --------------------------------------------------------------------------
# DELETE /api/config/providers/{name}
# --------------------------------------------------------------------------

@router.delete("/providers/{name}")
async def delete_provider(name: str):
    """Remove a provider profile from config.json."""
    provider_key = name.strip().lower()
    config_data = copy.deepcopy(state.config.get_config())
    model_configs = config_data.setdefault("models", {})

    if provider_key not in model_configs:
        raise HTTPException(404, f"Provider not found: {name}")

    # If the deleted provider is active, clear the active setting.
    if config_data.get("provider", "").strip().lower() == provider_key:
        remaining = [k for k in model_configs if k != provider_key]
        config_data["provider"] = remaining[0] if remaining else ""

    del model_configs[provider_key]
    _write_config(config_data)
    return {"ok": True}


@router.get("/providers/{name}/fetch-models")
async def fetch_provider_models(
    name: str,
    api_key: Optional[str] = Query(None, description="Override API key (not stored)"),
    base_url: Optional[str] = Query(None, description="Override base URL"),
):
    """Fetch available model IDs from the provider's /models endpoint."""
    try:
        spec = state.config.get_provider_spec(name)
    except Exception as exc:
        raise HTTPException(404, str(exc))

    resolved_key = api_key or spec.api_key
    resolved_url = base_url or spec.base_url

    if not resolved_key:
        raise HTTPException(400, "API key is required to fetch models.")
    if not resolved_url:
        raise HTTPException(400, "base_url is not configured for this provider.")

    try:
        models = await asyncio.to_thread(_fetch_models_sync, resolved_url, resolved_key)
    except ValueError as exc:
        raise HTTPException(502, f"Failed to fetch models: {exc}")

    return {"models": models}
