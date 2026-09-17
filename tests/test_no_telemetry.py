"""W10-2: the telemetry audit ``SSOT.md`` section 5 non-goal 2 requires by name.

The claim being defended is narrow and absolute: **DJcode talks to the model
provider you configured, to GitHub to check for its own updates, and to whatever
a tool you explicitly invoked asks for. Nothing else. Ever.**

That claim is worth nothing as prose in a README, because the way it breaks is
not a decision anyone announces -- it is one ``httpx.post`` added to a module
nobody re-reads. So it is asserted here, statically, over the whole of
``src/djcode``:

1. Every ``http(s)`` URL literal in the tree resolves to a host on an allowlist
   whose entries each carry a reason. **The provider hosts are derived from
   ``PROVIDERS`` rather than typed**, so adding a provider does not mean editing
   this file, and a provider removed from the registry stops being allowed here
   the same day.
2. Every ``httpx`` call site is located by AST and attributed to a module whose
   purpose is named. The module list may shrink freely; it may not grow silently.
3. No analytics or crash-reporting SDK is imported anywhere.
4. ``telemetry`` defaults to ``False`` in the shipped config.

What this does NOT prove, stated rather than implied: a URL assembled entirely at
run time out of config values is invisible to a static walk. That is why (2)
exists -- the call sites are enumerated and attributed, so a new one has to be
justified in this file before the suite goes green again.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
PKG = SRC / "djcode"

URL = re.compile(r"https?://([A-Za-z0-9._-]+)")

#: Hosts DJcode may name, and why. Anything else fails the audit.
#: Provider endpoints are NOT listed here -- see ``provider_hosts()``.
ALLOWED_HOSTS: dict[str, str] = {
    # -- the product's own update channel (updater.py, managed_update.py) ----
    "api.github.com": "release manifest for djcode's own managed update",
    "raw.githubusercontent.com": "the pinned update manifest blob",
    "github.com": "repository links printed to the user; no request is made to it",
    # -- explicit, user-invoked tools ---------------------------------------
    "duckduckgo.com": "web_search: the user asked for a search",
    "html.duckduckgo.com": "web_search fallback endpoint",
    "lite.duckduckgo.com": "web_search fallback endpoint",
    "api.search.brave.com": "web_search, only when the user configured a Brave key",
    # -- provider sign-in flows the user starts themselves -------------------
    "auth.x.ai": "account_auth: the browser sign-in the user initiated",
    "openrouter.ai": "openrouter_auth: the PKCE exchange the user initiated",
    # -- local ---------------------------------------------------------------
    "localhost": "a local model server on this machine",
    "127.0.0.1": "a local model server on this machine",
    "0.0.0.0": "a local bind address",
    # -- text, not destinations ----------------------------------------------
    "cli.darshj.ai": "the documentation URL printed in --docs and the TUI",
    "darshj.ai": "the $id of the JSONL event schema; never fetched",
    "json-schema.org": "the JSON Schema dialect $schema; never fetched",
    "docs.astral.sh": "installer.py prints it as an install instruction",
    "docs.docker.com": "installer.py prints it as an install instruction",
    "go.dev": "installer.py prints it as an install instruction",
    "nodejs.org": "installer.py prints it as an install instruction",
    "ollama.com": "installer.py prints it as an install instruction",
    "rustup.rs": "installer.py prints it as an install instruction",
}

#: Every module that may speak HTTP at all, with what it speaks it for.
#: This mapping is the audit. It may shrink; a new entry is a decision someone
#: has to write down here, in this file, before the suite is green again.
HTTPX_MODULES: dict[str, str] = {
    "djcode/account_auth.py": "provider account sign-in the user initiated",
    "djcode/cli.py": "catches httpx.HTTPError from the Vyasa fleet call",
    "djcode/colibri.py": "the bundled local model server",
    "djcode/core/onboarding_flow.py": "provider discovery against the configured endpoint",
    "djcode/managed_update.py": "djcode's own managed update",
    "djcode/openrouter_auth.py": "the OpenRouter PKCE exchange the user initiated",
    "djcode/provider.py": "the configured model provider",
    "djcode/providers/anthropic.py": "the configured model provider",
    "djcode/providers/base.py": "the configured model provider",
    "djcode/providers/google.py": "the configured model provider",
    "djcode/providers/openai.py": "the configured model provider",
    "djcode/startup.py": "provider reachability probe at launch",
    "djcode/tools/web_fetch.py": "web_fetch: the URL the user or the model asked for",
    "djcode/tools/web_search.py": "web_search: the query the user asked for",
    "djcode/updater.py": "djcode's own update check",
    "djcode/voice.py": "a local or configured speech endpoint",
    "djcode/vyasa.py": "the user's own Vyasa fleet server",
}

#: Analytics, product-metrics and crash-reporting SDKs. None of these may appear.
TELEMETRY_PACKAGES = {
    "amplitude",
    "analytics",
    "appsflyer",
    "bugsnag",
    "datadog",
    "ddtrace",
    "elasticapm",
    "honeycomb",
    "mixpanel",
    "newrelic",
    "opentelemetry",
    "posthog",
    "rollbar",
    "segment",
    "sentry_sdk",
    "statsd",
}


def sources() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def provider_hosts() -> dict[str, str]:
    """Every host the shipped provider registry points at. Derived, not typed."""
    from djcode.core.onboarding_flow import PROVIDERS

    out: dict[str, str] = {}
    for pid, spec in PROVIDERS.items():
        for match in URL.finditer(str(spec.get("base_url", "") or "")):
            out[match.group(1)] = f"base_url of the '{pid}' provider in PROVIDERS"
    return out


def allowed() -> dict[str, str]:
    merged = dict(ALLOWED_HOSTS)
    merged.update(provider_hosts())
    return merged


# ── 1. every URL literal in the tree ───────────────────────────────────────


def test_the_registry_really_contributes_hosts():
    """Guard against test 2 passing because ``PROVIDERS`` came back empty."""
    hosts = provider_hosts()
    assert len(hosts) >= 5, hosts
    assert "api.openai.com" in hosts
    assert "api.anthropic.com" in hosts


def test_every_url_literal_in_the_tree_is_accounted_for():
    permitted = allowed()
    unknown: list[str] = []
    for path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            for match in URL.finditer(node.value):
                host = match.group(1).lower().rstrip(".")
                if host in permitted:
                    continue
                # A placeholder in a docstring ("https://..." with a real
                # ellipsis) is prose, not a destination.
                if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
                    continue
                unknown.append(f"{rel(path)}:{node.lineno}: {host}")
    assert not unknown, (
        "an unaccounted-for host appeared in the source tree. If it is legitimate, "
        "add it to ALLOWED_HOSTS with the reason:\n" + "\n".join(sorted(set(unknown)))
    )


def test_a_planted_analytics_url_would_be_caught():
    """The audit is only worth running if it can fail. This proves it can."""
    permitted = allowed()
    for host in ("api.segment.io", "app.posthog.com", "telemetry.djcode.example"):
        assert host not in permitted


# ── 2. every httpx call site ───────────────────────────────────────────────

#: Attribute names that make a call an HTTP request when reached through a
#: client object: ``client.post(...)``, ``self._client.stream(...)``.
REQUEST_METHODS = {"get", "post", "put", "patch", "delete", "head", "stream", "request", "send"}

#: Deliberately narrow. "session" was in this list for one run and matched
#: ``CoreSession.send()`` and the permission engine's ``_session.get()`` -- two
#: things that touch no network at all. A heuristic that cries wolf gets muted,
#: and a muted audit defends nothing.
CLIENT_HINTS = ("client", "httpx")


def imports_httpx(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "httpx" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "httpx":
            return True
    return False


def root_name(node: ast.AST) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def call_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, expression)`` for everything that looks like an HTTP request."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        base = node.func.value
        if root_name(base) == "httpx":
            found.append((node.lineno, f"httpx.{attr}"))
            continue
        if attr in REQUEST_METHODS:
            tail = base.attr if isinstance(base, ast.Attribute) else root_name(base)
            if any(hint in tail.lower() for hint in CLIENT_HINTS):
                found.append((node.lineno, f"{tail}.{attr}"))
    return found


def test_only_the_audited_modules_import_httpx():
    actual = {rel(p) for p in sources() if imports_httpx(ast.parse(p.read_text(encoding="utf-8")))}
    new = actual - set(HTTPX_MODULES)
    assert not new, (
        "a module started speaking HTTP. Name its purpose in HTTPX_MODULES, or "
        f"do not make the call: {sorted(new)}"
    )
    stale = set(HTTPX_MODULES) - actual
    assert not stale, f"HTTPX_MODULES lists modules that no longer import httpx: {sorted(stale)}"


def inventory() -> dict[str, list[str]]:
    """Every HTTP request site in the tree, by module.

    A client-method call (``client.post(...)``) only counts inside a module that
    imports httpx; elsewhere the name belongs to somebody else's ``client``. A
    call rooted at the ``httpx`` name itself counts anywhere -- though it cannot
    occur without the import the previous test already pins.
    """
    out: dict[str, list[str]] = {}
    for path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        speaks = imports_httpx(tree)
        for lineno, expression in call_sites(tree):
            if not speaks and not expression.startswith("httpx."):
                continue
            out.setdefault(rel(path), []).append(f"{lineno}: {expression}()")
    return out


def test_every_http_call_site_lives_in_an_audited_module():
    sites = inventory()
    unaudited = set(sites) - set(HTTPX_MODULES)
    assert not unaudited, (
        "an HTTP request is being made from a module that is not part of the "
        "telemetry audit:\n"
        + "\n".join(f"{name}:{line}" for name in sorted(unaudited) for line in sites[name])
    )


def test_the_call_site_walk_actually_finds_calls():
    """Guard against the audit passing because the walk found nothing at all."""
    sites = inventory()
    assert "djcode/updater.py" in sites, "the walk found no request in updater.py"
    assert "djcode/provider.py" in sites, "the walk found no request in provider.py"
    assert "djcode/tools/web_fetch.py" in sites, "the walk found no request in web_fetch.py"
    assert sum(len(found) for found in sites.values()) >= 15, sites


# ── 3. no analytics SDK, anywhere ──────────────────────────────────────────


def test_no_analytics_or_crash_reporting_sdk_is_imported():
    offenders: list[str] = []
    for path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                if mod.split(".")[0].lower() in TELEMETRY_PACKAGES:
                    offenders.append(f"{rel(path)}:{node.lineno}: imports {mod}")
    assert not offenders, "a telemetry SDK reached the tree:\n" + "\n".join(offenders)


# ── 4. the shipped default ─────────────────────────────────────────────────


def test_telemetry_is_off_in_the_shipped_default_config():
    from djcode.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["telemetry"] is False


def test_nothing_in_the_tree_turns_telemetry_on():
    """No ``set_value("telemetry", True)`` and no ``config["telemetry"] = True``."""
    offenders: list[str] = []
    for path in sources():
        text = path.read_text(encoding="utf-8")
        if "telemetry" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "telemetry"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        offenders.append(f"{rel(path)}:{node.lineno}")
            if isinstance(node, ast.Call):
                args = node.args
                if (
                    len(args) >= 2
                    and isinstance(args[0], ast.Constant)
                    and args[0].value == "telemetry"
                    and isinstance(args[1], ast.Constant)
                    and args[1].value is True
                ):
                    offenders.append(f"{rel(path)}:{node.lineno}")
    assert not offenders, "something switches telemetry on:\n" + "\n".join(offenders)
