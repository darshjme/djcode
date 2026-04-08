"""MCP Server Registry — known MCP servers for one-click installation.

Like Goose and Claude Code's extension discovery, but with a curated registry
of tested MCP servers. Users can /extension add <name> and DJcode knows
the command, args, and what tools it provides.

Usage from REPL:
    /mcp list                  — show all 20 registered servers
    /mcp search <query>        — search by name, description, or category
    /mcp info <name>           — full details + install instructions
    /mcp install <name>        — add to extensions.json and optionally install deps
    /mcp categories            — list available categories

Integration with ExtensionManager:
    from djcode.mcp_registry import MCP_REGISTRY, install_from_registry
    ext = install_from_registry("github", manager, env={"GITHUB_TOKEN": "ghp_..."})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djcode.extensions import Extension, ExtensionManager


@dataclass(frozen=True)
class MCPServerInfo:
    """Metadata for a known MCP server in the registry."""

    name: str
    description: str
    cmd: str
    args: list[str]
    env_keys: list[str]  # Required env vars (API keys etc)
    tools_preview: list[str]  # Known tools it provides
    category: str  # filesystem, git, database, api, ai, dev, cloud, productivity
    install_cmd: str | None  # How to install (npm install -g, pip install, etc)
    homepage: str

    def requires_env(self) -> bool:
        """True if the server needs environment variables to function."""
        return len(self.env_keys) > 0

    def short_desc(self) -> str:
        """One-line summary: name — description [category]."""
        return f"{self.name} — {self.description} [{self.category}]"


# ---------------------------------------------------------------------------
# Registry: 20 curated MCP servers
# ---------------------------------------------------------------------------

MCP_REGISTRY: dict[str, MCPServerInfo] = {
    # ---- Filesystem ----
    "filesystem": MCPServerInfo(
        name="filesystem",
        description="Read/write files, create directories, search",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "."],
        env_keys=[],
        tools_preview=["read_file", "write_file", "list_directory", "search_files"],
        category="filesystem",
        install_cmd="npm install -g @modelcontextprotocol/server-filesystem",
        homepage="https://github.com/modelcontextprotocol/servers",
    ),

    # ---- Git ----
    "github": MCPServerInfo(
        name="github",
        description="GitHub repos, issues, PRs, actions",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env_keys=["GITHUB_TOKEN"],
        tools_preview=["create_issue", "list_repos", "create_pr", "search_code"],
        category="git",
        install_cmd="npm install -g @modelcontextprotocol/server-github",
        homepage="https://github.com/modelcontextprotocol/servers",
    ),

    # ---- Databases ----
    "postgres": MCPServerInfo(
        name="postgres",
        description="PostgreSQL database queries and schema",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-postgres"],
        env_keys=["POSTGRES_URL"],
        tools_preview=["query", "list_tables", "describe_table"],
        category="database",
        install_cmd="npm install -g @modelcontextprotocol/server-postgres",
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "sqlite": MCPServerInfo(
        name="sqlite",
        description="SQLite database operations",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-sqlite"],
        env_keys=[],
        tools_preview=["query", "create_table", "insert"],
        category="database",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "redis": MCPServerInfo(
        name="redis",
        description="Redis key-value store operations",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-redis"],
        env_keys=["REDIS_URL"],
        tools_preview=["get", "set", "del", "keys", "hget", "hset"],
        category="database",
        install_cmd="npm install -g @modelcontextprotocol/server-redis",
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "mongodb": MCPServerInfo(
        name="mongodb",
        description="MongoDB document database queries and aggregation",
        cmd="npx",
        args=["-y", "mcp-server-mongodb"],
        env_keys=["MONGODB_URI"],
        tools_preview=["find", "insert_one", "update_one", "aggregate", "list_collections"],
        category="database",
        install_cmd="npm install -g mcp-server-mongodb",
        homepage="https://github.com/kiliczsh/mcp-mongo-server",
    ),

    # ---- API / Web ----
    "brave-search": MCPServerInfo(
        name="brave-search",
        description="Web search via Brave Search API",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        env_keys=["BRAVE_API_KEY"],
        tools_preview=["web_search", "local_search"],
        category="api",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "fetch": MCPServerInfo(
        name="fetch",
        description="HTTP fetch for APIs and web pages",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-fetch"],
        env_keys=[],
        tools_preview=["fetch_url", "fetch_json"],
        category="api",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "slack": MCPServerInfo(
        name="slack",
        description="Slack messaging and channels",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        env_keys=["SLACK_BOT_TOKEN"],
        tools_preview=["send_message", "list_channels", "search_messages"],
        category="api",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),

    # ---- Dev Tools ----
    "puppeteer": MCPServerInfo(
        name="puppeteer",
        description="Browser automation and screenshots",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-puppeteer"],
        env_keys=[],
        tools_preview=["navigate", "screenshot", "click", "fill"],
        category="dev",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "docker": MCPServerInfo(
        name="docker",
        description="Docker container and image management",
        cmd="npx",
        args=["-y", "mcp-server-docker"],
        env_keys=[],
        tools_preview=["list_containers", "run_container", "stop_container", "list_images", "build_image", "container_logs"],
        category="dev",
        install_cmd="npm install -g mcp-server-docker",
        homepage="https://github.com/ckreiling/mcp-server-docker",
    ),
    "kubernetes": MCPServerInfo(
        name="kubernetes",
        description="Kubernetes cluster management and kubectl operations",
        cmd="npx",
        args=["-y", "mcp-server-kubernetes"],
        env_keys=["KUBECONFIG"],
        tools_preview=["get_pods", "get_services", "get_deployments", "apply_manifest", "get_logs", "describe_resource"],
        category="dev",
        install_cmd="npm install -g mcp-server-kubernetes",
        homepage="https://github.com/Flux159/mcp-server-kubernetes",
    ),

    # ---- AI ----
    "memory": MCPServerInfo(
        name="memory",
        description="Persistent knowledge graph memory",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        env_keys=[],
        tools_preview=["store", "retrieve", "search", "relate"],
        category="ai",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
    "sequential-thinking": MCPServerInfo(
        name="sequential-thinking",
        description="Chain-of-thought reasoning tool",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
        env_keys=[],
        tools_preview=["think_step", "reason", "conclude"],
        category="ai",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),

    # ---- Cloud Platforms ----
    "supabase": MCPServerInfo(
        name="supabase",
        description="Supabase database, auth, storage, and edge functions",
        cmd="npx",
        args=["-y", "mcp-server-supabase"],
        env_keys=["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"],
        tools_preview=["query", "insert", "list_tables", "auth_create_user", "storage_upload", "invoke_function"],
        category="cloud",
        install_cmd="npm install -g mcp-server-supabase",
        homepage="https://github.com/supabase-community/supabase-mcp",
    ),
    "vercel": MCPServerInfo(
        name="vercel",
        description="Vercel deployments, projects, and domains",
        cmd="npx",
        args=["-y", "mcp-server-vercel"],
        env_keys=["VERCEL_TOKEN"],
        tools_preview=["list_projects", "list_deployments", "get_deployment", "list_domains", "create_deployment"],
        category="cloud",
        install_cmd="npm install -g mcp-server-vercel",
        homepage="https://github.com/nicepkg/mcp-server-vercel",
    ),
    "cloudflare": MCPServerInfo(
        name="cloudflare",
        description="Cloudflare Workers, KV, R2, D1, and DNS",
        cmd="npx",
        args=["-y", "@cloudflare/mcp-server-cloudflare"],
        env_keys=["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
        tools_preview=["list_workers", "deploy_worker", "kv_get", "kv_put", "r2_list", "dns_list_records"],
        category="cloud",
        install_cmd="npm install -g @cloudflare/mcp-server-cloudflare",
        homepage="https://github.com/cloudflare/mcp-server-cloudflare",
    ),
    "aws": MCPServerInfo(
        name="aws",
        description="AWS services — S3, Lambda, DynamoDB, EC2, and more",
        cmd="npx",
        args=["-y", "mcp-server-aws"],
        env_keys=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
        tools_preview=["s3_list", "s3_get", "lambda_invoke", "dynamodb_query", "ec2_describe"],
        category="cloud",
        install_cmd="npm install -g mcp-server-aws",
        homepage="https://github.com/aws/mcp-server-aws",
    ),

    # ---- Productivity ----
    "notion": MCPServerInfo(
        name="notion",
        description="Notion pages, databases, and blocks",
        cmd="npx",
        args=["-y", "mcp-server-notion"],
        env_keys=["NOTION_API_KEY"],
        tools_preview=["search", "get_page", "create_page", "update_page", "query_database", "append_block"],
        category="productivity",
        install_cmd="npm install -g mcp-server-notion",
        homepage="https://github.com/makenotion/notion-mcp-server",
    ),
    "google-drive": MCPServerInfo(
        name="google-drive",
        description="Google Drive file management and search",
        cmd="npx",
        args=["-y", "@modelcontextprotocol/server-google-drive"],
        env_keys=["GOOGLE_APPLICATION_CREDENTIALS"],
        tools_preview=["list_files", "read_file", "search_files", "create_file"],
        category="productivity",
        install_cmd=None,
        homepage="https://github.com/modelcontextprotocol/servers",
    ),
}

# Category descriptions for display
CATEGORIES: dict[str, str] = {
    "filesystem": "Local file and directory operations",
    "git": "Version control and repository management",
    "database": "SQL and NoSQL database access",
    "api": "Web APIs, search, and HTTP",
    "dev": "Developer tools, containers, browsers",
    "ai": "AI reasoning and memory tools",
    "cloud": "Cloud platform management",
    "productivity": "Docs, notes, and team tools",
}


# ---------------------------------------------------------------------------
# Search and discovery
# ---------------------------------------------------------------------------

def search_registry(query: str) -> list[MCPServerInfo]:
    """Search the MCP registry by name, description, or category.

    Case-insensitive substring match across name, description, and category.
    Returns all matches sorted by relevance (name match first, then description).
    """
    query_lower = query.lower().strip()
    if not query_lower:
        return list(MCP_REGISTRY.values())

    name_matches: list[MCPServerInfo] = []
    desc_matches: list[MCPServerInfo] = []

    for info in MCP_REGISTRY.values():
        if query_lower in info.name.lower():
            name_matches.append(info)
        elif query_lower in info.description.lower() or query_lower in info.category.lower():
            desc_matches.append(info)

    return name_matches + desc_matches


def list_by_category(category: str) -> list[MCPServerInfo]:
    """List MCP servers filtered by category.

    Valid categories: filesystem, git, database, api, dev, ai, cloud, productivity.
    Returns empty list for unknown categories.
    """
    cat_lower = category.lower().strip()
    return [info for info in MCP_REGISTRY.values() if info.category == cat_lower]


def get_server_info(name: str) -> MCPServerInfo | None:
    """Get a server by exact name. Returns None if not found."""
    return MCP_REGISTRY.get(name)


def get_install_instructions(name: str) -> str:
    """Get full install instructions for an MCP server.

    Returns a multi-line string with command, env setup, and usage example.
    """
    info = MCP_REGISTRY.get(name)
    if not info:
        return f"Unknown MCP server: '{name}'. Run /mcp list to see available servers."

    lines = [
        f"MCP Server: {info.name}",
        f"  {info.description}",
        "",
        f"Category: {info.category}",
        f"Homepage: {info.homepage}",
        "",
    ]

    # Install step
    if info.install_cmd:
        lines.append(f"Install (optional, npx handles this automatically):")
        lines.append(f"  $ {info.install_cmd}")
        lines.append("")

    # Command
    lines.append("Command:")
    lines.append(f"  {info.cmd} {' '.join(info.args)}")
    lines.append("")

    # Environment variables
    if info.env_keys:
        lines.append("Required environment variables:")
        for key in info.env_keys:
            lines.append(f"  {key}=<your-value>")
        lines.append("")

    # DJcode quick-add
    if info.env_keys:
        env_snippet = " ".join(f'--env {k}=...' for k in info.env_keys)
        lines.append(f"Quick add to DJcode:")
        lines.append(f"  /extension add {info.name} {env_snippet}")
    else:
        lines.append(f"Quick add to DJcode:")
        lines.append(f"  /extension add {info.name}")

    lines.append("")

    # Tools preview
    if info.tools_preview:
        lines.append(f"Known tools ({len(info.tools_preview)}):")
        for tool in info.tools_preview:
            lines.append(f"  - {tool}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------

def render_registry(console) -> None:
    """Render the full MCP registry as a Rich table.

    Args:
        console: A rich.console.Console instance.
    """
    from rich.table import Table

    table = Table(
        title="MCP Server Registry",
        title_style="bold cyan",
        show_lines=False,
        pad_edge=True,
        expand=False,
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="bold green", min_width=18)
    table.add_column("Description", min_width=30)
    table.add_column("Category", style="cyan", min_width=12)
    table.add_column("Env Keys", style="yellow", min_width=15)
    table.add_column("Tools", style="dim", min_width=20)

    for i, info in enumerate(MCP_REGISTRY.values(), 1):
        env_str = ", ".join(info.env_keys) if info.env_keys else "-"
        tools_str = ", ".join(info.tools_preview[:4])
        if len(info.tools_preview) > 4:
            tools_str += f" +{len(info.tools_preview) - 4}"
        table.add_row(
            str(i),
            info.name,
            info.description,
            info.category,
            env_str,
            tools_str,
        )

    console.print(table)
    console.print(
        f"\n  [dim]{len(MCP_REGISTRY)} servers available. "
        f"Use [bold]/mcp info <name>[/bold] for details or "
        f"[bold]/mcp install <name>[/bold] to add one.[/dim]\n"
    )


def render_categories(console) -> None:
    """Render category summary as a Rich table."""
    from rich.table import Table

    table = Table(title="MCP Categories", title_style="bold cyan", expand=False)
    table.add_column("Category", style="bold green")
    table.add_column("Description")
    table.add_column("Count", style="cyan", justify="right")

    for cat, desc in CATEGORIES.items():
        count = len(list_by_category(cat))
        if count > 0:
            table.add_row(cat, desc, str(count))

    console.print(table)


def render_search_results(console, results: list[MCPServerInfo], query: str) -> None:
    """Render search results as a compact Rich table."""
    from rich.table import Table

    if not results:
        console.print(f"  [yellow]No servers matching '{query}'.[/yellow]")
        return

    table = Table(
        title=f"Search: '{query}' ({len(results)} results)",
        title_style="bold cyan",
        expand=False,
    )
    table.add_column("Name", style="bold green")
    table.add_column("Description")
    table.add_column("Category", style="cyan")
    table.add_column("Env Keys", style="yellow")

    for info in results:
        env_str = ", ".join(info.env_keys) if info.env_keys else "-"
        table.add_row(info.name, info.description, info.category, env_str)

    console.print(table)


# ---------------------------------------------------------------------------
# Integration with ExtensionManager
# ---------------------------------------------------------------------------

def install_from_registry(
    name: str,
    manager: ExtensionManager,
    env: dict[str, str] | None = None,
) -> Extension | None:
    """Install an MCP server from the registry into the ExtensionManager.

    Looks up the server by name in MCP_REGISTRY, creates an Extension with
    the correct cmd/args, and registers it via manager.add().

    Args:
        name: Registry server name (e.g., "github", "postgres").
        manager: The active ExtensionManager instance.
        env: Environment variables (API keys, connection strings, etc.).

    Returns:
        The created Extension, or None if the name is not in the registry.

    Raises:
        ValueError: If required env keys are missing.
    """
    info = MCP_REGISTRY.get(name)
    if info is None:
        return None

    env = env or {}

    # Validate required env vars
    missing = [k for k in info.env_keys if k not in env]
    if missing:
        raise ValueError(
            f"MCP server '{name}' requires environment variables: {', '.join(missing)}. "
            f"Pass them via env={{{', '.join(repr(k) + ': ...' for k in missing)}}}"
        )

    return manager.add(
        name=info.name,
        cmd=info.cmd,
        args=list(info.args),
        env=env,
        description=info.description,
    )


def get_missing_env_keys(name: str, env: dict[str, str] | None = None) -> list[str]:
    """Check which required env keys are missing for a given server.

    Returns an empty list if all keys are present or the server needs none.
    """
    info = MCP_REGISTRY.get(name)
    if info is None:
        return []
    env = env or {}
    return [k for k in info.env_keys if k not in env]


# ---------------------------------------------------------------------------
# REPL /mcp command wiring
# ---------------------------------------------------------------------------
#
# To wire this registry into the DJcode REPL, add a handler in repl.py:
#
#     from djcode.mcp_registry import (
#         MCP_REGISTRY,
#         search_registry,
#         list_by_category,
#         get_install_instructions,
#         install_from_registry,
#         render_registry,
#         render_categories,
#         render_search_results,
#         get_missing_env_keys,
#     )
#
#     # Inside the slash-command dispatcher:
#     if cmd == "/mcp":
#         parts = rest.strip().split(maxsplit=1)
#         subcmd = parts[0] if parts else "list"
#         arg = parts[1] if len(parts) > 1 else ""
#
#         if subcmd == "list":
#             render_registry(console)
#
#         elif subcmd == "categories":
#             render_categories(console)
#
#         elif subcmd == "search" and arg:
#             results = search_registry(arg)
#             render_search_results(console, results, arg)
#
#         elif subcmd == "info" and arg:
#             console.print(get_install_instructions(arg))
#
#         elif subcmd == "install" and arg:
#             name = arg.split()[0]
#             missing = get_missing_env_keys(name)
#             if missing:
#                 console.print(f"[yellow]Required env vars: {', '.join(missing)}[/yellow]")
#                 console.print(f"Use: /extension add {name} --env KEY=VALUE ...")
#             else:
#                 ext = install_from_registry(name, extension_manager)
#                 if ext:
#                     console.print(f"[green]Installed {name}![/green]")
#                 else:
#                     console.print(f"[red]Unknown server: {name}[/red]")
#
#         else:
#             console.print("[dim]Usage: /mcp list | search <q> | info <name> | install <name> | categories[/dim]")
#
# This keeps the registry pure-data and the REPL handler thin.
# The registry has zero runtime dependencies beyond dataclasses.
