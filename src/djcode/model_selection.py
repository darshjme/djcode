"""Nonmutating model discovery and validated provider/model selection.

The public catalog contains no endpoints, keys or inferred capabilities. Selection
returns a candidate; the caller persists it only after its runtime is ready.
"""
from __future__ import annotations

from copy import deepcopy
import re

from djcode.auth import PROVIDERS
from djcode.config import load_config
from djcode.startup import probe


class ModelSelectionError(ValueError):
    """A selection could not be verified; existing configuration is retained."""


def provider_choices(config: dict | None = None) -> list[dict]:
    config = config if config is not None else load_config()
    custom = config.get("custom_providers", {})
    custom = custom if isinstance(custom, dict) else {}
    names = list(dict.fromkeys([str(config.get("provider", "ollama")), *PROVIDERS, *custom]))
    # URLs remain valid connection inputs, but credentials embedded in a URL must
    # never appear in a catalog or option label.
    return [{"id": name, "name": PROVIDERS.get(name, {}).get("name", name)} for name in names
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
            and (name in PROVIDERS or name == "remote" or isinstance(custom.get(name), dict))]


def candidate_config(config: dict, provider: str, model: str | None = None) -> dict:
    """Pair a model with its provider, preserving unrelated auth/settings."""
    if not isinstance(provider, str) or not provider.strip():
        raise ModelSelectionError("Choose a provider.")
    candidate = deepcopy(config)
    changed = candidate.get("provider") != provider
    candidate["provider"] = provider
    custom = candidate.get("custom_providers", {})
    custom = custom if isinstance(custom, dict) else {}
    entry = custom.get(provider, {})
    entry = entry if isinstance(entry, dict) else {}
    if changed:
        candidate["base_url"] = ""
        candidate["model"] = entry.get("model", "")
    if model is not None:
        if not isinstance(model, str) or not model.strip() or len(model) > 512 or any(ord(c) < 32 for c in model):
            raise ModelSelectionError("Enter a valid exact model ID.")
        candidate["model"] = model.strip()
        if isinstance(custom.get(provider), dict):
            custom[provider]["model"] = model.strip()
    return candidate


def model_catalog(config: dict | None = None, provider: str | None = None) -> dict:
    """Discover one provider without mutating or exposing configuration secrets."""
    config = config if config is not None else load_config()
    selected = provider or config.get("provider", "ollama")
    if not any(item["id"] == selected for item in provider_choices(config)):
        return {"provider": selected if isinstance(selected, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", selected) else "unknown", "status": "missing", "source": "none",
                "error": "provider_unknown", "message": "Connect this provider first with /provider.", "models": [], "selected": None}
    candidate = candidate_config(config, selected)
    found = probe(candidate)
    metadata = found.get("model_details") or [{"id": ident, "name": ident, "capabilities": {}} for ident in found.get("models", [])]
    source = found.get("source", "live" if metadata else "unavailable")
    status = "available" if metadata and found["status"] == "missing" else found["status"]
    return {"provider": selected, "status": status, "source": source, "error": found.get("error"),
            "message": found["message"], "models": metadata,
            "selected": candidate.get("model") if found["status"] == "ready" else None}


def select_model(config: dict, reference: str, provider: str | None = None) -> dict:
    """Validate an exact ID or PROVIDER/MODEL; never save or infer a provider."""
    selected = provider or config.get("provider", "ollama")
    model = reference.strip()
    if provider is None and "/" in model:
        prefix, suffix = model.split("/", 1)
        if prefix in {item["id"] for item in provider_choices(config)}:
            selected, model = prefix, suffix
    if not any(item["id"] == selected for item in provider_choices(config)):
        raise ModelSelectionError("Unknown provider. Connect it with /provider first.")
    candidate = candidate_config(config, selected, model)
    checked = probe(candidate)
    if checked["status"] != "ready":
        raise ModelSelectionError(checked["message"] + " Selection has not been saved.")
    # Canonicalize Ollama's explicit tag while preserving all other exact IDs.
    if selected == "ollama" and model not in checked["models"] and model + ":latest" in checked["models"]:
        candidate["model"] = model + ":latest"
    recent = candidate.get("recent_model_selections", [])
    recent = recent if isinstance(recent, list) else []
    choice = {"provider": selected, "model": candidate["model"]}
    candidate["recent_model_selections"] = [choice, *[item for item in recent if isinstance(item, dict) and item != choice]][:10]
    candidate["setup_complete"] = True
    return candidate


def capability_summary(model: dict) -> str:
    """Describe provider-reported fields; absence means unknown."""
    values = model.get("capabilities", {})
    parts = []
    if values.get("context_tokens"):
        parts.append(f"context {values['context_tokens']:,}")
    if values.get("max_output_tokens"):
        parts.append(f"output {values['max_output_tokens']:,}")
    for field in ("tools", "vision", "reasoning"):
        if isinstance(values.get(field), bool):
            parts.append(f"{field} {'yes' if values[field] else 'no'}")
    return " · ".join(parts) or "Capabilities not reported by this provider"
