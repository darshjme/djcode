"""The provider/auth/model onboarding state machine — async, headless, transactional.

W8-3. Two duplicate implementations of this flow shipped side by side before
this module existed: ``connect.ConnectScreen`` (Textual) and ``startup.setup``
(questionary). They agreed on the states and disagreed on everything else --
only ``setup`` asked for a server URL, only ``setup`` offered "save it anyway"
when the endpoint could not be verified, only ``ConnectScreen`` was
transactional. What lives here is the **union** of the two, and both are now
views over it.

Three rules the flow keeps, all inherited from ``ConnectScreen``, which
``SSOT.md`` section 3.1 is right to call the best-engineered user-facing code in
the repo:

* **Nothing is mutated until it is committed.** Every edit lands on a deepcopy
  of the live config; the real config is written once, at the end, by
  :meth:`OnboardingFlow.commit`.
* **Cancel preserves the existing configuration.** :meth:`OnboardingFlow.cancel`
  drops the candidate and nothing is saved. There is no half-applied state to
  roll back because nothing was applied.
* **Probe before commit.** The endpoint and model are checked, and only a
  ``ready`` -- or an explicitly accepted unverified -- result reaches disk.

Two rules this module adds:

* **Async throughout; ``asyncio.run`` never.** A flow that calls ``asyncio.run``
  cannot be driven from a GUI's event loop, and a flow that hides a blocking
  call behind ``asyncio.to_thread`` has moved the problem rather than solved it.
  ``ConnectScreen`` did exactly that -- ``await asyncio.to_thread(probe, ...)``
  around a ``probe`` that itself calls ``asyncio.run`` -- a double hop this
  module removes.
* **No terminal, anywhere.** ``tests/test_headless_purity.py`` enforces it. The
  flow never prompts: it *returns a* :class:`Prompt` *describing* the question
  and waits to be told the answer. That is what lets one state machine serve
  questionary, Textual and a GUI without any of them knowing about the others.

The network half (``connection``, ``discover_async``, ``probe_async``) lives
here rather than in ``djcode.startup`` for one non-negotiable reason:
``startup.py`` imports click, questionary and rich at module level, so core may
not import it at any depth, lazily or otherwise. ``startup`` keeps the
*blocking* wrappers and their ``_refuse_inside_loop`` contract (W1-7) and
delegates the implementations here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import httpx

from djcode.config import load_config as _default_load
from djcode.config import save_config as _default_save

# Discovery budgets. ``startup`` keeps module-level copies under the same names
# so a caller (and the regression suite) can still tighten them there; they are
# passed down per call rather than read from a global here.
DEFAULT_DISCOVERY_DEADLINE = 5.0
DEFAULT_DISCOVERY_SIZE_BUDGET = 2 * 1024 * 1024

#: Providers that ask for an explicit endpoint before anything else: the
#: self-hosted ones whose default URL is a guess about the user's machine.
URL_FIRST_PROVIDERS = {"ollama", "mlx", "colibri", "custom"}

# ── Provider registry ──────────────────────────────────────────────────────
#
# Moved here from ``djcode.auth`` in W8. It is pure data with no behaviour, and
# ``auth.py`` -- which owns the interactive pickers and imports questionary and
# rich at module level -- is a module core may never import. ``djcode.auth``
# re-exports this name, so every existing ``from djcode.auth import PROVIDERS``
# keeps working.

PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "name": "Ollama (Local)",
        "needs_key": False,
        "base_url": "http://localhost:11434",
        "description": "Local inference, no API key needed",
    },
    "openai": {
        "name": "OpenAI",
        "needs_key": True,
        "env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "description": "GPT-4o, o1, o3 models",
    },
    "xai": {
        "name": "xAI (Grok)",
        "needs_key": True,
        "env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "description": "Grok via xAI API; distinct from Groq",
    },
    "anthropic": {
        "name": "Anthropic",
        "needs_key": True,
        "env": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com",
        "description": "Sonnet, Opus, Haiku models",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "needs_key": True,
        "env": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "description": "DeepSeek, Kimik2, GLM models via NIM",
    },
    "google": {
        "name": "Google AI",
        "needs_key": True,
        "env": "GOOGLE_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "description": "Gemini models",
    },
    "groq": {
        "name": "Groq",
        "needs_key": True,
        "env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "description": "Ultra-fast inference",
    },
    "together": {
        "name": "Together AI",
        "needs_key": True,
        "env": "TOGETHER_API_KEY",
        "base_url": "https://api.together.xyz/v1",
        "description": "Open-source model hosting",
    },
    "openrouter": {
        "name": "OpenRouter",
        "needs_key": True,
        "env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "description": "Multi-provider router",
    },
    "mlx": {
        "name": "MLX-LM (Local)",
        "needs_key": False,
        "base_url": "http://localhost:8899",
        "description": "Apple Silicon native inference",
    },
    "colibri": {
        "name": "Colibri (Local)",
        "needs_key": False,
        "optional_key": True,
        "env": "COLI_API_KEY",
        "base_url": "http://127.0.0.1:8000/v1",
        "description": "Opt-in existing Colibri server; no model downloads",
    },
    "featherless": {
        "name": "Featherless AI",
        "needs_key": True,
        "env": "FEATHERLESS_API_KEY",
        "base_url": "https://api.featherless.ai/v1",
        "description": "Hosted open models via OpenAI-compatible API",
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "needs_key": True,
        "env": "DJCODE_API_KEY",
        "base_url": "",
        "description": "Any OpenAI-compatible endpoint",
    },
}


class OnboardingCancelled(Exception):  # noqa: N818 - a cancellation is not an error
    """The user abandoned setup. Nothing was written; the old config stands.

    Deliberately not ``click.ClickException``: ``djcode.core``'s contract is that
    a GUI can consume every error this package raises, and a CLI framework's
    exception type is meaningless to one. ``startup.setup`` translates this back
    into the ``KeyboardInterrupt`` its callers have always seen.
    """


class OnboardingError(Exception):
    """Setup cannot proceed with the answer given; nothing was written."""


# ── The network half ───────────────────────────────────────────────────────


async def _request(
    endpoint: str, headers: dict, *, size_budget: int = DEFAULT_DISCOVERY_SIZE_BUDGET
) -> httpx.Response:
    """Stream the discovery response under a hard body-size budget."""
    chunks = []
    size = 0
    async with httpx.AsyncClient(timeout=2.0, follow_redirects=False) as client:
        async with client.stream("GET", endpoint, headers=headers) as response:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > size_budget:
                    raise ValueError("Provider discovery exceeded its size budget")
                chunks.append(chunk)
            return httpx.Response(
                response.status_code, content=b"".join(chunks), request=response.request
            )


async def discover_async(
    endpoint: str,
    headers: dict,
    *,
    deadline: float = DEFAULT_DISCOVERY_DEADLINE,
    size_budget: int = DEFAULT_DISCOVERY_SIZE_BUDGET,
) -> httpx.Response:
    """The real implementation: bounded, cancellable, loop-native."""
    try:
        return await asyncio.wait_for(
            _request(endpoint, headers, size_budget=size_budget), timeout=deadline
        )
    except TimeoutError:
        raise ValueError("Provider discovery exceeded its time budget") from None


def connection(config: dict, provider: str | None = None, model: str | None = None) -> dict:
    """Resolve provider, endpoint, key, model and auth method out of a config dict."""
    selected = provider or config.get("provider", "ollama")
    info = PROVIDERS.get(selected, {})
    custom = config.get("custom_providers", {}).get(selected, {})
    url_provider = selected.startswith(("http://", "https://"))
    base = (
        selected
        if url_provider
        else custom.get("base_url") or config.get(f"{selected}_url") or info.get("base_url", "")
    )
    base = os.environ.get("DJCODE_BASE_URL") or config.get("base_url") or base
    key = (
        custom.get("api_key")
        or config.get(f"{selected}_api_key")
        or os.environ.get(info.get("env", ""), "")
    )
    if url_provider or selected in {"custom", "remote"}:
        key = (
            key
            or os.environ.get("DJCODE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or config.get("remote_api_key", "")
        )
    method = config.get(f"{selected}_auth_method", "api_key")
    selected_model = model or custom.get("model") or config.get("model", "")
    if selected == "colibri":
        selected_model = (
            model
            or (config.get("model") if config.get("provider") == "colibri" else None)
            or "djcode-colibri"
        )
    selected_model = selected_model.strip() if isinstance(selected_model, str) else ""
    return {
        "provider": selected,
        "base": (base or "").rstrip("/"),
        "key": key or "",
        "model": selected_model,
        "method": method,
        "needs_key": bool(info.get("needs_key")),
    }


def catalogue(name: str, payload: dict) -> list[dict]:
    """Normalise a provider's model listing to ``[{"name": str, "size": int}, ...]``.

    ``size`` is Ollama's on-disk byte count, and ``0`` wherever the provider does
    not report one. Discarding it here forced every consumer that wants a model
    table to re-query the provider.
    """
    items = payload.get("models" if name in {"ollama", "google"} else "data", [])
    if not isinstance(items, list):
        return []
    key = "name" if name in {"ollama", "google"} else "id"
    sizes: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key, "")
        if not isinstance(value, str) or not value:
            continue
        if name == "google":
            value = value.removeprefix("models/")
        raw = item.get("size", 0)
        size = int(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0
        sizes[value] = max(sizes.get(value, 0), max(size, 0))
    return [{"name": value, "size": sizes[value]} for value in sorted(sizes)]


async def probe_async(
    config: dict,
    provider: str | None = None,
    model: str | None = None,
    *,
    discover: Callable[[str, dict], Awaitable[httpx.Response]] | None = None,
) -> dict:
    """Check the configured provider/model and return the discovered catalogue.

    ``discover`` exists so ``startup.probe_async`` can hand down its own
    module-level ``discover_async`` -- the seam seven regression tests patch --
    without core needing to know that ``djcode.startup`` exists.
    """
    fetch = discover or discover_async
    details = connection(config, provider, model)
    name, base, key = details["provider"], details["base"], details["key"]

    def outcome(status, message, models=None):
        return {"status": status, "message": message, "models": models or [], "provider": name}

    if not base.startswith(("http://", "https://")):
        return outcome("missing", "Choose a provider endpoint.")
    if details["method"] == "account":
        from djcode.account_auth import AccountAuthError, account_token, has_account

        if name != "xai" or base != "https://api.x.ai/v1":
            return outcome(
                "missing", "Account authentication requires the supported provider endpoint."
            )
        if not has_account(name):
            return outcome("missing", "Account sign-in is required.")
        try:
            # W8: `account_token` is the coroutine that `get_account_token`
            # wrapped in `asyncio.run`, and `startup.probe_async` then wrapped
            # THAT in `asyncio.to_thread`. Awaiting it directly removes both
            # hops; this function is already inside the caller's loop.
            key = await account_token(name, "https://api.x.ai/v1")
        except AccountAuthError:
            if not details["model"]:
                return outcome("missing", "Select an explicit model ID.")
            return outcome("offline", "Account refresh unavailable; saved setup retained.")
    elif details["needs_key"] and not key:
        return outcome("missing", "An API key or supported account sign-in is required.")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if name == "ollama":
        endpoint = base + "/api/tags"
    elif name == "anthropic":
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif name == "google":
        endpoint = base + "/models"
        headers = {"x-goog-api-key": key}
    else:
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
    try:
        response = await fetch(endpoint, headers)
        if response.status_code in {401, 403}:
            return outcome(
                "missing", "The provider rejected authentication; choose or reconnect an account."
            )
        if response.status_code in {404, 405, 501}:
            if not details["model"]:
                return outcome("missing", "Select an explicit model ID.")
            return outcome(
                "unverified",
                "This endpoint does not expose model discovery; the explicit model will be checked "
                "during use.",
            )
        response.raise_for_status()
        models = catalogue(name, response.json())
        available = {item["name"] for item in models}
        selected = details["model"]
        matched = selected in available or (name == "ollama" and selected + ":latest" in available)
        if not selected or not matched:
            return outcome("missing", "Select a model available at this provider.", models)
        return outcome("ready", f"Connected to {name} · {selected}", models)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        if not details["model"]:
            return outcome("missing", "Select an explicit model ID.")
        return outcome("offline", "Provider check unavailable; existing configuration retained.")


# ── The state machine ──────────────────────────────────────────────────────


@dataclass(slots=True)
class Prompt:
    """One question, described rather than asked.

    A view turns this into a questionary widget, a Textual ``OptionList`` or a
    GUI panel. ``stage`` is the state the flow is now in and names the method
    the answer goes back through:

    ==================  ==========================================
    ``stage``           answer with
    ==================  ==========================================
    ``provider``        :meth:`OnboardingFlow.choose_provider`
    ``url``             :meth:`OnboardingFlow.submit_url`
    ``auth``            :meth:`OnboardingFlow.choose_auth`
    ``key``             :meth:`OnboardingFlow.submit_key`
    ``browser``         :meth:`OnboardingFlow.submit_browser_code`
    ``account``         :meth:`OnboardingFlow.device_login`
    ``model``           :meth:`OnboardingFlow.choose_model`
    ``unverified``      :meth:`OnboardingFlow.confirm_unverified`
    ``done``            nothing; :attr:`OnboardingFlow.candidate` is saved
    ==================  ==========================================
    """

    stage: str
    detail: str = ""
    placeholder: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    default: str = ""
    password: bool = False
    free_text: bool = True
    #: Auth methods that exist but are not usable, with the reason, so a view can
    #: say why rather than silently hiding them.
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    #: The URL a browser-auth stage wants opened.
    url: str = ""


class OnboardingFlow:
    """``provider -> auth -> (key | url | browser | account) -> model -> finish``.

    Every transition is a coroutine returning the next :class:`Prompt`. The flow
    owns no I/O of its own beyond the four injected ports, so a test can drive
    the whole machine with four lambdas and never touch a socket.

    Ports (all optional; the defaults are the real thing):

    ``load``    zero-arg, returns the current config dict.
    ``save``    one-arg, persists the committed config.
    ``probe``   ``(config) -> dict`` or an awaitable of one. Sync callables are
                accepted and awaited only if they return an awaitable, because
                a view may legitimately have a synchronous probe (a test double,
                or a cached result) and forcing it to be async would buy nothing.
    ``methods`` ``(provider) -> list[dict]``, defaults to
                ``account_auth.auth_methods``.
    """

    def __init__(
        self,
        existing: dict | None = None,
        *,
        load: Callable[[], dict] | None = None,
        save: Callable[[dict], Any] | None = None,
        probe: Callable[[dict], Any] | None = None,
        methods: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self._load = load or _default_load
        self._save = save or _default_save
        self._probe = probe
        self._methods = methods
        # The transactional heart: every edit lands here, the live config is
        # untouched until `commit`.
        self.candidate: dict = deepcopy(existing if existing is not None else self._load())
        self.stage = "provider"
        self.provider: str = str(self.candidate.get("provider", "") or "")
        self.models: list[dict] = []
        self.message: str = ""
        self.verifier: str = ""
        self.cancelled = False

    # -- helpers ----------------------------------------------------------

    async def _run_probe(self, config: dict) -> dict:
        if self._probe is None:
            return await probe_async(config)
        result = self._probe(config)
        if asyncio.isfuture(result) or asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            result = await result
        return result

    def _auth_methods(self, provider: str) -> list[dict]:
        if self._methods is not None:
            return self._methods(provider)
        from djcode.account_auth import auth_methods

        return auth_methods(provider)

    def _guard(self) -> None:
        if self.cancelled:
            raise OnboardingCancelled("Setup cancelled; existing configuration retained")

    # -- states -----------------------------------------------------------

    def start(self) -> Prompt:
        """The first question: which provider."""
        self.stage = "provider"
        names = list(
            dict.fromkeys(
                [
                    "openrouter",
                    "openai",
                    "anthropic",
                    *PROVIDERS,
                    *self.candidate.get("custom_providers", {}),
                ]
            )
        )
        return Prompt(
            stage="provider",
            detail="1 Provider  →  2 Sign in  →  3 Model",
            placeholder="Search providers",
            options=[(name, PROVIDERS.get(name, {}).get("name", name)) for name in names],
            default=self.provider,
        )

    async def choose_provider(self, name: str) -> Prompt:
        """Select a provider. Switching providers clears the stale model and base URL."""
        self._guard()
        name = str(name or "").strip()
        if not name:
            raise OnboardingError("Choose a provider.")
        info = PROVIDERS.get(name, {})
        if self.candidate.get("provider") != name:
            self.candidate["base_url"] = ""
            self.candidate["model"] = "djcode-colibri" if name == "colibri" else ""
        self.candidate["provider"] = name
        self.provider = name
        self.candidate[f"{name}_url"] = self.candidate.get(f"{name}_url") or info.get(
            "base_url", ""
        )
        if name in URL_FIRST_PROVIDERS:
            # Only `startup.setup` ever asked this; `ConnectScreen` asked only
            # when the base came out empty, so a user pointing DJcode at a
            # Colibri server on a non-default port had no way to say so in the
            # TUI. The union asks.
            return self._url_prompt()
        return await self._after_endpoint()

    def _url_prompt(self) -> Prompt:
        self.stage = "url"
        return Prompt(
            stage="url",
            detail="Enter the server URL for this provider.",
            placeholder="http://localhost:8000/v1",
            default=self.candidate.get(f"{self.provider}_url", ""),
        )

    async def submit_url(self, value: str) -> Prompt:
        self._guard()
        endpoint = str(value or "").strip()
        if not endpoint.startswith(("http://", "https://")):
            raise OnboardingError("Enter an HTTP(S) endpoint; configuration was not saved.")
        self.candidate[f"{self.provider}_url"] = endpoint.rstrip("/")
        return await self._after_endpoint()

    async def _after_endpoint(self) -> Prompt:
        info = PROVIDERS.get(self.provider, {})
        if info.get("needs_key", True):
            methods = self._auth_methods(self.provider)
            available = [item for item in methods if item["available"]]
            current = self.candidate.get(f"{self.provider}_auth_method", "api_key")
            default = current if current in {item["id"] for item in available} else "api_key"
            self.stage = "auth"
            return Prompt(
                stage="auth",
                detail=f"Connect {self.provider}: choose how to authenticate.",
                placeholder="Choose an option above",
                options=[(item["id"], item["label"]) for item in available],
                default=default,
                free_text=False,
                unavailable=[
                    (item["label"], item.get("reason", ""))
                    for item in methods
                    if not item["available"]
                ],
            )
        self.candidate[f"{self.provider}_auth_method"] = "api_key"
        return await self.discover()

    async def choose_auth(self, method: str) -> Prompt:
        """Select an authentication method. ``browser`` ends up stored as ``api_key``
        because what the browser flow produces IS an API key."""
        self._guard()
        method = str(method or "").strip() or "api_key"
        self.candidate[f"{self.provider}_auth_method"] = (
            "api_key" if method == "browser" else method
        )
        if method == "browser":
            from djcode.openrouter_auth import begin

            self.verifier, url = begin()
            self.stage = "browser"
            return Prompt(
                stage="browser",
                detail=(
                    "Authorize DJcode in your browser, then paste the one-time code.\n" + url
                ),
                placeholder="One-time authorization code",
                password=True,
                url=url,
            )
        if method == "account":
            self.stage = "account"
            return Prompt(
                stage="account",
                detail="Sign in to your provider account.",
                free_text=False,
            )
        self.stage = "key"
        return Prompt(
            stage="key",
            detail=(
                "API key stays in your local DJcode configuration. Blank keeps an existing key."
            ),
            placeholder="API key",
            password=True,
        )

    async def submit_browser_code(self, code: str) -> Prompt:
        self._guard()
        from djcode.openrouter_auth import exchange

        self.candidate[f"{self.provider}_api_key"] = await exchange(code, self.verifier)
        self.verifier = ""
        return await self.discover()

    async def device_login(self, on_status: Callable[[Any], Any] | None = None) -> Prompt:
        """RFC 8628 device sign-in, awaited rather than threaded.

        An account already connected is reused as-is: re-running the device flow
        for a signed-in user is a second browser round trip for nothing, and the
        old questionary path's ``authenticate_account`` did exactly that from a
        worker thread while prompt_toolkit owned stdin.
        """
        self._guard()
        from djcode.account_auth import begin_xai_login, finish_xai_login, has_account

        if not has_account(self.provider):
            device = await begin_xai_login()
            if on_status is not None:
                result = on_status(device)
                if asyncio.iscoroutine(result):
                    await result
            await finish_xai_login(device)
        return await self.discover()

    async def submit_key(self, value: str) -> Prompt:
        self._guard()
        value = str(value or "").strip()
        if value:
            self.candidate[f"{self.provider}_api_key"] = value
        elif not connection(self.candidate)["key"]:
            raise OnboardingError("An API key is required; configuration was not saved.")
        if not connection(self.candidate)["base"]:
            return self._url_prompt()
        return await self.discover()

    async def discover(self) -> Prompt:
        """Probe the endpoint and offer whatever models it reports."""
        self._guard()
        found = await self._run_probe(self.candidate)
        self._guard()
        self.models = list(found.get("models") or [])
        self.message = str(found.get("message", "") or "")
        names = [item["name"] for item in self.models if isinstance(item, dict) and "name" in item]
        default = self.candidate.get("model", "") or ""
        if names and default not in names:
            default = names[0]
        self.stage = "model"
        return Prompt(
            stage="model",
            detail=(
                f"Choose a model from {self.provider}, or enter its exact ID.\n{self.message}"
            ),
            placeholder="Search or enter model ID",
            options=[(name, name) for name in names],
            default=default,
        )

    async def choose_model(self, name: str) -> Prompt:
        """Set the model and verify it. Commits when the check comes back ``ready``."""
        self._guard()
        name = str(name or "").strip()
        if not name:
            raise OnboardingError("A model ID is required; configuration was not saved.")
        self.candidate["model"] = name
        checked = await self._run_probe(self.candidate)
        self._guard()
        self.message = str(checked.get("message", "") or "")
        status = checked.get("status")
        if status == "missing":
            raise OnboardingError(self.message + " Configuration was not saved.")
        if status != "ready":
            # `startup.setup` offered this escape hatch and `ConnectScreen` did
            # not, which meant the TUI could not configure an endpoint that is
            # simply offline right now. The union offers it; a view that does
            # not want it answers `confirm_unverified(False)`.
            self.stage = "unverified"
            return Prompt(
                stage="unverified",
                detail="Connection could not be fully verified. Save this setup for later?",
                free_text=False,
            )
        return self.commit()

    def confirm_unverified(self, accept: bool) -> Prompt:
        self._guard()
        if not accept:
            raise OnboardingCancelled("Setup cancelled; existing configuration retained")
        return self.commit()

    def commit(self) -> Prompt:
        """Write the candidate. The single point at which anything reaches disk."""
        self._guard()
        self.candidate["setup_complete"] = True
        self.candidate.setdefault("update_mode", "auto")
        self._save(self.candidate)
        self.stage = "done"
        return Prompt(stage="done", detail=self.message or "Setup saved.")

    def cancel(self) -> None:
        """Abandon setup. Idempotent, and there is nothing to undo."""
        self.cancelled = True
        self.verifier = ""
