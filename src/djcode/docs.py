"""Built-in documentation viewer for DJcode.

Renders comprehensive docs in the terminal using Rich.
Access via /docs command in the REPL.
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

GOLD = "#FFD700"

DOCS_SECTIONS = {
    "overview": """
# DJcode v2.0.1

**The last coding CLI you'll ever need.**

Local-first AI coding agent with 22 specialist agents, semantic routing,
infinite context engine, and tool extraction for any model.

Built by Darshankumar Joshi · github.com/darshjme/djcode · cli.darshj.ai

## Install
```
curl -fsSL https://cli.darshj.ai/install.sh | bash
```

## Quick Start
```
djcode                          # Interactive REPL
djcode "write a REST API"      # One-shot mode
djcode --model gemma4           # Specific model
djcode --auto-accept            # Skip tool confirmations
```
""",

    "commands": """
# Slash Commands

## Build & Ship
| Command | Agent | Description |
|---------|-------|-------------|
| /orchestra <task> | Vyasa | Multi-agent orchestration |
| /review <code> | Dharma | Code review (security, perf, style) |
| /debug <issue> | Sherlock | Root cause analysis |
| /test <target> | Agni | Write tests |
| /refactor <code> | Shiva | Restructure (zero behavior change) |
| /devops <task> | Vayu | Docker, CI/CD, deploy |
| /docs <target> | Saraswati | Generate documentation |

## Content & Marketing
| Command | Agent | Description |
|---------|-------|-------------|
| /launch <product> | All | Build → Ship → Campaign pipeline |
| /campaign <brief> | Narada | Content campaign (12 agents) |
| /image <concept> | Maya | Image prompts |
| /video <concept> | Kubera | Cinematic video prompts |
| /social <topic> | Chitragupta | Social media content |

## Extensions & Recipes
| Command | Description |
|---------|-------------|
| /extension add <name> <cmd> | Add MCP extension |
| /extension list | List extensions |
| /extension remove <name> | Remove extension |
| /recipe list | List available recipes |
| /recipe run <name> | Execute a recipe |
| /resume | Resume a past session |
| /history <query> | Search past conversations |

## Session & Config
| Command | Description |
|---------|-------------|
| /model [name] | Switch model (interactive picker) |
| /provider [name] | Switch provider |
| /auth | Configure API keys |
| /auto | Toggle auto-accept |
| /stats [7d|30d] | Usage dashboard with heatmap |
| /agents | Show all 22 agents |
| /memory | Memory tier stats |
| /buddy | Show dharmic ASCII buddy |
| /skill list|add|remove | Manage teachable skills |
| /shortcuts | Keyboard shortcuts reference |
| /config | Show configuration |
| /clear | Clear conversation |
| /save | Save conversation |
| /exit | Exit DJcode |
""",

    "keyboard": """
# Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Ctrl+O | Toggle thinking/verbose mode |
| Ctrl+P | Toggle Plan/Act mode |
| Ctrl+L | Clear screen |
| Ctrl+T | Toggle auto-accept tools |
| Ctrl+R | Rerun last command |
| Ctrl+K | Kill current generation |
| / | Interactive command picker |
| Escape | Cancel current input |
""",

    "agents": """
# Agent Registry — 22 Specialists

## Dev Agents (10)
| Name | Role | Temp | Tools |
|------|------|------|-------|
| Vyasa | Orchestrator | 0.3 | All |
| Prometheus | Coder | 0.4 | All |
| Sherlock | Debugger | 0.2 | All |
| Vishwakarma | Architect | 0.5 | Read-only |
| Dharma | Reviewer | 0.3 | Read-only |
| Agni | Tester | 0.3 | All |
| Garuda | Scout | 0.3 | Read-only |
| Vayu | DevOps | 0.3 | All |
| Saraswati | Docs | 0.6 | All |
| Shiva | Refactorer | 0.3 | All |

## Content Agents (12)
| Name | Role |
|------|------|
| Narada | Campaign Director |
| Valmiki | Script Writer |
| Chitragupta | Social Strategist |
| Maya | Image Prompter |
| Kubera | Video Director |
| Tvastar | ComfyUI Expert |
| Gandharva | Audio Prompter |
| Brihaspati | SEO Analyst |
| Saraswati | Brand Voice |
| Vishvakarma | Thumbnail Designer |
| Hanuman | Content Repurposer |
| Garuda | Trend Scout |
""",

    "providers": """
# Providers

| Provider | Type | API Key |
|----------|------|---------|
| Ollama | Local | None |
| MLX | Local (Apple Silicon) | None |
| OpenAI | Cloud | Required |
| Anthropic | Cloud | Required |
| NVIDIA NIM | Cloud | Required |
| Google AI | Cloud | Required |
| Groq | Cloud | Required |
| Together AI | Cloud | Required |
| OpenRouter | Cloud | Required |

Switch: `djcode --provider openai --model gpt-4o "your prompt"`
""",

    "models": """
# Supported Models

| Model | Size | Context | Tools |
|-------|------|---------|-------|
| gemma4 | 9.6 GB | 32K | Yes |
| qwen2.5-coder:7b | 4.7 GB | 8K | Yes |
| deepseek-coder-v2:lite | 8.9 GB | 16K | Yes |
| dolphin3 | 4.9 GB | 4K | Via Router |
| gemma4:26b | 16 GB | 32K | Yes |
| qwen3:32b | 20 GB | 128K | Yes |

**RAM guide:** 8GB → 7B · 16GB → 9-12B · 32GB → 26B MoE · 64GB+ → 70B+

**Tool Router:** Models without native tool-calling (dolphin3, llama3,
mistral, phi3) still work as full agents via DJcode's text extraction router.
""",

    "extensions": """
# MCP Extensions

DJcode supports external tools via Model Context Protocol (MCP).

## Add an extension
```
/extension add github mcp-server-github
/extension add filesystem mcp-server-filesystem -- /path/to/dir
```

## List extensions
```
/extension list
```

## Remove
```
/extension remove github
```

Extensions communicate via JSON-RPC over stdio. Any MCP-compatible
server works: GitHub, filesystem, databases, APIs, custom tools.
""",

    "recipes": """
# Recipes — Reusable Workflows

## Built-in Recipes
| Recipe | Pipeline | Description |
|--------|----------|-------------|
| new-project | Prometheus | Scaffold any project type |
| code-review | Dharma + Garuda | Thorough security/perf review |
| debug-fix | Sherlock → Prometheus → Agni | Debug, fix, verify |
| launch-campaign | Narada + 12 agents | Full marketing campaign |
| refactor-safe | Shiva → Agni | Restructure + regression check |

## Run a recipe
```
/recipe run debug-fix issue="login page crashes on submit"
```

## Create a recipe
```
/recipe create
```

Recipes are stored at `~/.djcode/recipes/` as JSON files.
""",

    "privacy": """
# Privacy & Security

- **DO_NOT_TRACK=1** set by default
- Zero analytics, zero telemetry, zero phone-home
- No account required, no sign-up, no email
- All inference runs locally via Ollama/MLX
- Cloud providers are opt-in (bring your own key)
- Permission system warns before file writes and commands
- Dangerous command detection (rm -rf, sudo, curl|bash)
- Protected files: pyproject.toml, README.md, etc. can't be overwritten
- djcode.md stays in YOUR project directory
- API keys stored locally (future: keyring integration)
""",
}


def render_docs(console: Console, section: str = "overview") -> None:
    """Render a documentation section in the terminal."""
    if section == "all":
        for name, content in DOCS_SECTIONS.items():
            console.print(Markdown(content))
            console.print()
        return

    content = DOCS_SECTIONS.get(section)
    if content:
        console.print(Markdown(content))
    else:
        console.print(f"[yellow]Unknown section:[/] {section}")
        console.print(f"[dim]Available: {', '.join(DOCS_SECTIONS.keys())}[/]")


def render_docs_index(console: Console) -> None:
    """Show the docs table of contents."""
    console.print(f"\n  [bold {GOLD}]DJcode Documentation[/]\n")

    for key, content in DOCS_SECTIONS.items():
        # Extract title from first # heading
        title = key.title()
        for line in content.strip().split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break
        console.print(f"  [{GOLD}]/docs {key}[/]  [dim]— {title}[/]")

    console.print(f"\n  [dim]/docs all — show everything[/]")
    console.print(f"  [dim]Online: https://cli.darshj.ai/docs[/]\n")
