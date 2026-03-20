# DJcode (Python)

Local-first AI coding CLI by [DarshJ.AI](https://darshj.ai).

DJcode is a standalone AI coding assistant that runs entirely on your machine. It connects to local LLM backends (Ollama, MLX) and provides a full coding toolkit: file operations, shell execution, grep, glob, git, and a 3-tier memory system.

Zero telemetry. Zero cloud dependencies. Your code stays on your machine.

## Install

```bash
# With uv (recommended)
uv tool install .

# Or with pip
pip install .

# Or run directly
uv run python -m djcode
```

## Usage

```bash
# Interactive REPL
djcode

# One-shot mode
djcode "write a Python function that calculates fibonacci"

# Use a different provider/model
djcode --provider mlx
djcode --model qwen3:32b
djcode --model llama3.2:latest

# Unrestricted mode
djcode --bypass-rlhf

# Raw output (no Rich formatting)
djcode --raw "explain this code"

# Show config
djcode --config

# Show version
djcode --version
```

## REPL Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/model <name>` | Switch model |
| `/provider <p>` | Switch provider (ollama, mlx, remote) |
| `/memory` | Show memory stats |
| `/remember k=v` | Store a persistent fact |
| `/recall <key>` | Recall a fact |
| `/forget <key>` | Remove a fact |
| `/clear` | Clear conversation |
| `/save` | Save conversation to disk |
| `/config` | Show configuration |
| `/set k=v` | Set a config value |
| `/raw` | Toggle raw output mode |
| `/exit` | Exit |

## Tools

DJcode has direct access to your filesystem and shell:

- **bash** — Execute shell commands
- **file_read** — Read files with line numbers
- **file_write** — Create or overwrite files
- **file_edit** — Surgical string replacement
- **grep** — Search with regex (uses ripgrep)
- **glob** — Find files by pattern
- **git** — Git operations (with safety guards)

## Architecture

```
src/djcode/
  cli.py          Click CLI entry point
  repl.py         Interactive REPL with streaming
  provider.py     Ollama/MLX/Remote abstraction
  prompt.py       System prompt
  config.py       ~/.djcode/config.json
  tools/          bash, file ops, grep, glob, git
  memory/         3-tier memory (session, persistent, vector)
  agents/         Operator, Scout, Architect
```

## Requirements

- Python 3.12+
- Ollama running locally (or MLX/compatible endpoint)
- A downloaded model (`ollama pull qwen3:32b`)

## Development

```bash
# Install dev dependencies
uv add --dev pytest ruff

# Run tests
uv run pytest

# Lint
uv run ruff check src/

# Format
uv run ruff format src/
```

## License

MIT
