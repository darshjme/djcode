# Installation, updates and recovery

DJcode needs Python 3.12+ and Git. Choose a hosted provider with an existing account/API key, an already-running Ollama server with an installed model, or an existing MLX server. Installation and onboarding do not download inference or embedding models. Model availability and tool support depend on the selected provider and account.

## Install and update

The website installer creates a versioned directory under `~/.local/share/djcode/release.*`, with source in `source/` and the runtime in `venv/`. The launcher at `~/.local/bin/djcode` points to that release. The installer keeps earlier successful releases for rollback and does not modify shell startup files.

```sh
curl -fsSL https://cli.darshj.ai/install.sh | bash
```

Add `~/.local/bin` to your PATH if necessary. Run `djcode --version` and `djcode --help` after installation.

The release checker queries `darshjme/djcode` on GitHub at most once per day and displays an explicit update command when it finds a newer stable release. It never executes an update. For managed installs, the suggestion uses the existing release's installer and preserves its install/bin directories; a new environment is created before the launcher changes. Ordinary Python/source environments receive a command targeting their current interpreter and the official Git repository. The checker does not depend on a separate `djcode-cli` package.

Set `DJCODE_NO_UPDATE_CHECK=1` to disable release-check network requests. You can also rerun the installer yourself, retaining any `DJCODE_INSTALL_DIR` and `DJCODE_BIN_DIR` overrides used for the original installation. Earlier release directories are retained; to roll back, repoint your launcher to the desired earlier release's `venv/bin/djcode` after checking that release's version. User configuration and memory live separately from releases.

## Provider setup

The first-run wizard lists the provider registry, including Featherless and custom OpenAI-compatible endpoints. Hosted providers accept a key or the provider's documented environment variable. Featherless and custom setup require the exact model ID available to your account; custom setup also requires the HTTP(S) base URL, including `/v1` when the server uses that prefix. Setup does not claim a model is available before a real inference call succeeds.

Ollama discovery reads the existing server's `/api/tags` list. An empty list means no installed models were detected at that endpoint; it is not proof that Ollama is unreachable. Setup never calls `/api/pull`. Cancelling provider selection saves no configuration. Tool auto-accept starts disabled.

## Durable facts and conversations

Facts use atomic local JSON writes and a file lock so two open sessions do not overwrite one another's changes. Malformed facts cause a visible read error and are preserved for repair. Lexical retrieval works without a model or network. Semantic retrieval requires explicitly supplied embeddings; Chroma's implicit embedding function is disabled. Updating a fact without an embedding invalidates its previous semantic entry.

SQLite session schema migrations retain existing conversation rows and preserve tool-call IDs and tool names. Compaction treats an assistant tool request and its contiguous results as one group in all four strategies. Pinned results keep the associated request, and recent-message boundaries do not split tool groups. Compression may remain above its target if protected system, pinned, or recent context alone exceeds that target; a model's context size still imposes a real limit.

## Verification

`tests/test_updates.py` covers managed/source update commands, the actual GitHub repository, opt-out, stable versions, package-argument validation, cancellation, Featherless/custom setup, and empty Ollama discovery. These tests do not install software, download models or call live inference.

`tests/test_memory_recovery.py` covers all compaction strategies, pinned tool results, SQLite reopen/schema upgrades/search, atomic failure recovery, concurrent fact writers, lexical/vector fallback, invalid conversation paths, stale embedding invalidation and expiring context budgets.

```sh
uv run --with pytest pytest tests/test_updates.py tests/test_memory_recovery.py tests/test_v4.py -q
```

These deterministic checks complement a real installation and provider inference smoke test; they do not replace them.
