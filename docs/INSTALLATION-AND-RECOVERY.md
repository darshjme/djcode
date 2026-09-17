# Installation, updates and recovery

DJcode 4.4 requires an existing Python 3.12+ installation. Git is needed for source installation and Git workflows. Choose an existing local model server or a configured hosted provider. Installation and onboarding do not download inference or embedding models.

project by Darshan Kumar Joshi

## Managed installation and updates

```sh
curl -fsSL https://cli.darshj.ai/install.sh | bash
djcode --version
djcode --check
```

The default installer verifies the canonical `darshjme/djcode` build manifest, source commit tag, wheel URL and SHA-256. GitHub Actions is not required. It stages an isolated release, validates the CLI, and then switches the `current` symlink; `previous` retains the preceding release. Launchers in `~/.local/bin` follow `current`. Add that directory to PATH if needed; the installer does not modify shell startup files.

This verifies repository provenance and integrity, not an independent signature or a guarantee of correctness. The default DAF engine requires Cargo for its first local build, or an existing binary configured through `DJCODE_DAF_ENGINE`.

Managed installs check and apply validated updates at startup by default. Updates stage a separate environment and switch `current` atomically after validation; a failed activation attempts to restore the old pointer. Previous releases and user configuration are retained. Network metadata, downloads and staging commands have time and size budgets; dependency installation can still take several minutes. The startup launcher restarts into a successfully updated build. An update requested from the TUI asks you to restart after your current work.

```sh
djcode --update                 # explicit check and managed update
djcode --update-mode manual     # only update when requested
djcode --update-mode disabled   # disable managed updates
djcode --update-mode auto       # restore startup updates
djcode --no-update              # skip updates for this invocation
djcode --rollback              # restore previous release; set mode to manual
```

`DJCODE_NO_UPDATE_CHECK=1` also disables update network activity. Updates and rollback share a lock. Rollback supports POSIX managed installations and does not delete release directories. User facts, sessions and account credentials live separately from releases.

Developer/source environments are preserved; automatic updates do not replace their checkout or environment. Explicit `DJCODE_REPO_URL` or `DJCODE_REF` installer overrides select a manual source installation rather than the canonical managed channel. Retain any custom `DJCODE_INSTALL_DIR` and `DJCODE_BIN_DIR` values when reinstalling. A missing or invalid managed receipt requires manual installation repair; do not fabricate one to enable automatic switching.

## Provider setup

Startup probes the configured provider with bounded model discovery. Missing credentials or a missing model lead to interactive selection when a terminal is available. Offline discovery preserves existing configuration rather than silently changing providers. `djcode --setup` explicitly opens setup. A successful model-list response establishes discovery, not successful inference or tool compatibility.

Setup lists the provider registry, authentication choices and available model IDs where discovery is supported. API keys may come from the provider's documented environment variable. Custom endpoints require an HTTP(S) base URL and an exact model ID. Ollama discovery uses `/api/tags`; setup never calls `/api/pull`. Cancelled setup preserves existing configuration. Tool auto-accept starts disabled.

Account sign-in is limited to supported integrations and provider eligibility. xAI device authorization requires a separately approved client registration; ordinary installs offer API-key mode. DJcode does not import credentials from other coding agents or provide direct ChatGPT/Claude subscription login. See [account authentication](ACCOUNT-AUTH.md) for protocol, storage, cancellation and provider restrictions.

## Installation checks

`djcode --check` and `djcode --lint` check runtime syntax, registries and fatal source lint. They do not run model inference or replace the test suite. The TUI exposes `/check`, `/lint` and `/update` in its command palette. Review a failed check before continuing to rely on the installation.

## Durable facts and conversations

Facts use atomic local JSON writes and a file lock so two open sessions do not overwrite one another's changes. Malformed facts cause a visible read error and are preserved for repair. Lexical retrieval works without a model or network. Semantic retrieval requires explicitly supplied embeddings; Chroma's implicit embedding function is disabled. Updating a fact without an embedding invalidates its previous semantic entry.

SQLite session schema migrations retain existing conversation rows and preserve tool-call IDs and tool names. Compaction treats an assistant tool request and its contiguous results as one group in all four strategies. Pinned results keep the associated request, and recent-message boundaries do not split tool groups. Compression may remain above its target if protected system, pinned, or recent context alone exceeds that target; a model's context size still imposes a real limit.

## Verification

`tests/test_managed_update.py` uses real temporary release directories and symlinks with mocked network/staging to cover atomic activation, failed staging, rollback, locks, metadata validation, offline behavior and preservation of unmanaged installs.

`tests/test_updates.py` covers managed/source update commands, the actual GitHub repository, opt-out, stable versions, package-argument validation, cancellation, Featherless/custom setup, and empty Ollama discovery. These tests do not install software, download models or call live inference.

`tests/test_memory_recovery.py` covers all compaction strategies, pinned tool results, SQLite reopen/schema upgrades/search, atomic failure recovery, concurrent fact writers, lexical/vector fallback, invalid conversation paths, stale embedding invalidation and expiring context budgets.

```sh
uv run --with pytest pytest tests/test_updates.py tests/test_memory_recovery.py tests/test_v4.py -q
```

These deterministic checks complement a real installation and provider inference smoke test; they do not replace them.

`djcode --revision` reports the running managed build commit without contacting GitHub. Source and custom installations are labeled unmanaged.

## Scheduler worker recovery

Stop old scheduler workers before upgrading. New workers hold an OS lock for each claimed job. A lost worker's job becomes `interrupted` on the next list/claim/cancel operation and is never automatically replayed. Review effects before creating a replacement. Interrupted jobs can be cancelled.

Jobs claimed by older lockless workers are preserved as `running` because their liveness cannot be established. After stopping that worker, use `/schedule {"action":"recover","schedule_id":"ID"}` to mark a legacy run interrupted. This does not run the command again. Keep the database and its adjacent `.locks` directory on a local filesystem; do not delete lock files while workers are running.
