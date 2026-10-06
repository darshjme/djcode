# 01 — Authentication management and account faults

Owner: auth specialist. Date: 2026-10-06. Source frozen for root integration.

## Done

- Added `src/djcode/auth_management.py` with credential-free list/status and explicit API-key, OpenRouter browser, xAI account, method selection and provider-scoped logout operations.
- Unified credential selection in `auth.get_key_source(provider_id, cfg=None)`. Valid saved credentials take precedence over environment credentials. Invalid saved values no longer suppress a valid environment fallback. Named custom providers resolve their own saved key, empty custom definitions remain custom, and the legacy remote key has an explicit source.
- Preserved malformed settings during mutations: auth operations refuse invalid JSON, nonobject settings, unreadable text or invalid custom-provider mappings instead of saving recovered defaults over them.
- Made API-key configuration writes atomic through the existing private settings writer. Account configuration failures restore the previous raw account credential bytes; failed account deletion restores the previous settings. Successful writes remain private (`0600`).
- Added local account readiness without exposing tokens; malformed expiry metadata becomes login-required rather than raising a type error. Refresh and device responses that arrive after logout in the active event loop cannot re-save the discarded account session.
- Bounded and validated OpenRouter PKCE inputs/key responses and xAI device verification URLs/codes before network exchange or terminal display. Unexpected response bodies and filesystem errors produce sanitized messages.
- Added `tests/test_auth_management.py` and `tests/test_auth_faults.py`. Existing account/auth selection/classic tests remain compatible.

## Found

- Credential selection differed between runtime and readiness views. Truthy nonstring saved values blocked environment fallback; named custom keys and legacy remote keys had inconsistent resolution.
- Read-only recovery of malformed settings was appropriate at startup but unsafe as the basis for an auth write. A login/logout operation could otherwise replace the original malformed file with defaults.
- Provider response fields were previously insufficiently bounded for terminal display and key storage; malformed ports/expiry values could escape the intended sanitized failure path.
- Logout must explain environment credentials still available after deletion of a saved key. It must preserve other providers, model selections and custom endpoint metadata.
- The supported account methods are limited by provider authorization. xAI's documented device flow still requires an approved DJcode public-client registration. DJcode does not have an implemented approved OpenAI subscription backend; Anthropic prohibits third-party use of Claude subscription OAuth credentials. OpenRouter's documented headless PKCE flow is usable and intentionally omits `callback_url`.

## Decisions

- Match existing runtime precedence: saved key first, valid environment key second. Report the source explicitly; never silently substitute an API key when account mode was selected.
- Default auth mutations to `activate=False`; acquiring credentials does not alter the selected provider/model. Root CLI uses hidden input or `--key-stdin`, never an argv key.
- Keep list/status strictly local: `connected` means local credential/method readiness, not a successful provider request. Every message reflects that limit. Custom display names and arbitrary config/endpoint fields are excluded from reports.
- Restrict account tokens to the exact official xAI API origin. A global saved base override applies to the selected provider; an environment base override remains an explicit global override.
- Reject unavailable methods before a device grant or browser exchange. Do not borrow another application's OAuth registration or credential storage.
- Preserve prior content on failed writes; provide honest failure if rollback itself fails. Two private files are individually atomic, not one transaction across process crashes or power loss. Separate processes can still contend over rotating refresh tokens; these changes do not claim cross-process serialization.

## Root integration API

```python
from djcode.auth_management import (
    AuthManagementError, list_auth, auth_status, login_api_key,
    select_auth_method, logout, login_account, login_openrouter,
)
```

- `list_auth(config=None) -> list[dict]`: registry and valid custom providers, plus configured legacy remote.
- `auth_status(provider=None, config=None) -> dict`: selected provider by default.
- `login_api_key(provider, key, *, activate=False) -> dict`: key supplied through hidden input/stdin.
- `select_auth_method(provider, method, *, activate=False) -> dict`: choose a ready existing `api_key`, `account` or `local` method without starting login.
- `logout(provider) -> dict`: delete only this provider's saved credentials, retain environment values and neighboring configuration.
- `await login_account(provider, *, on_status=print, activate=False) -> dict`: explicit supported xAI device sign-in; safe URL/code status text only.
- `await login_openrouter(provider, code, verifier, *, activate=False) -> dict`: exchange and save without returning the issued key. Obtain `verifier, url = openrouter_auth.begin()` first, then prompt for the single-use code.
- `auth.get_key_source(provider_id, cfg=None) -> (key, source, env_name)` is a **private credential-bearing runtime resolver**. Startup/provider code may consume the first value; CLI/JSON must never display it. `get_api_key` delegates to this resolver.
- `account_auth.account_status(provider) -> dict` returns `stored`, `connected`, `expired`, `refreshable` only; `has_account` retains its existing boolean contract.
- `finish_xai_login(..., store=True) -> dict` retains default save behavior; `store=False` permits staging for the management operation. Existing callers can continue ignoring its return value.

Report fields are `provider`, `name`, `active`, `method`, `source`, `status`, `connected`, `stored_key`, `environment_present`, `env_var`, `account_stored`, `methods` and `message`. Sources: `stored`, `env`, `account`, `local`, `none`. Statuses: `configured`, `connected`, `local`, `login_required`, `unavailable`, `invalid`. Methods expose only `id`, `label`, `available`, `reason`.

## Tests and actual results

Commands run from the DJcode repository with isolated fake config/account storage and mocked HTTP transports. No live login, provider inference or paid request was run.

1. Baseline compatibility: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_account_auth.py tests/test_auth_selection.py tests/test_classic_auth.py -q` — **31 passed**.
2. Complete auth scope before the final two regressions: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_auth_management.py tests/test_auth_faults.py tests/test_account_auth.py tests/test_auth_selection.py tests/test_classic_auth.py -q` — **97 passed**.
3. Broader capability scope before the final two scoped regressions: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_auth_management.py tests/test_auth_faults.py tests/test_account_auth.py tests/test_auth_selection.py tests/test_classic_auth.py tests/test_capabilities.py -k 'not real' -q` — **108 passed, 2 deselected**. The excluded tests exercise real DAF hosts and belong to root's combined acceptance.
4. **Final current-source auth acceptance** after environment reporting and account-delete rollback regressions: `PYTHONPATH=src .venv/bin/python -m pytest tests/test_auth_management.py tests/test_auth_faults.py tests/test_account_auth.py tests/test_auth_selection.py tests/test_classic_auth.py tests/test_capabilities.py -k 'auth or pkce or signin or account or provider_switch' -q` — **100 passed, 12 deselected**.
5. **Final full configured Ruff**, including all new owned files: `.venv/bin/python -m ruff check src/djcode/auth.py src/djcode/account_auth.py src/djcode/openrouter_auth.py src/djcode/auth_management.py tests/test_auth_management.py tests/test_auth_faults.py` — **All checks passed**. Fatal Ruff also passed earlier. Initial line-length/import-order findings were corrected before this result.
6. `git diff --check -- src/djcode/auth.py src/djcode/account_auth.py src/djcode/openrouter_auth.py` — **passed**. This scoped Git check covers tracked owned edits; full Ruff covers the new module/tests.

Acceptance covers credential privacy/source reporting, `0600` storage, invalid saved values, custom/remote precedence, preserving malformed config bytes, mocked atomic replacement failure, provider-scoped logout, environment retention, account-delete rollback, staged-account config rollback, malformed response bounds, official-origin restrictions, and delayed success after logout.

## Primary evidence reviewed

- [OpenCode CLI authentication](https://opencode.ai/docs/cli/): auth login/list/logout informs the requested CLI shape.
- [OpenRouter OAuth/PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth): documented headless single-use code and S256 exchange create a user-controlled API key.
- [xAI official discovery](https://auth.x.ai/.well-known/openid-configuration): device/token endpoints and public-client protocol support; this does not grant a DJcode registration.
- [Official Codex authentication](https://developers.openai.com/codex/auth/), which redirects to [ChatGPT developer authentication](https://learn.chatgpt.com/docs/auth): official subscription login is distinct from an approved DJcode integration.
- [Claude Code legal/compliance](https://code.claude.com/docs/en/legal-and-compliance): third-party products cannot route Claude subscription OAuth credentials; API-key auth remains the supported DJcode method.
- Existing `docs/ACCOUNT-AUTH.md` and `.shadow-kb/swarm-20261006/08-config-recovery.md` read; current `03-lint-updates.md` and `05-cli-review.md` handoffs reviewed for integration.

## Open questions

- Has xAI approved a DJcode public-client registration and account entitlement for the requested device scope? No evidence of approval/live entitlement was supplied or inferred.
- Root owns the combined CLI/model tests and distributable package verification; those outcomes are not claimed here.

## Next steps

- Root run combined acceptance once all current owners freeze, including CLI entry points and distribution checks.
- Include actual auth command examples, source precedence and account limitations in the root-owned auth/model maintenance documentation; do not describe local readiness as live access verification.
- A future authorized account acceptance needs an approved xAI client and a consenting account. Do not reuse another application's client ID or claim subscription access based on protocol discovery.

No GitHub Actions, commits, live account login, release or third-party credential reads were performed. No test process remains running.
