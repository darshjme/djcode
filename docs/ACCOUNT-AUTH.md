# Account authentication

DJcode supports API-key providers and local endpoints. Its account selector shows
which methods are available and explains restrictions before attempting login.
It does not read Codex, Claude Code, OpenCode, or Grok credential files.

xAI account sign-in is an opt-in integration for an **xAI-approved public client
registration**. Set `DJCODE_XAI_OAUTH_CLIENT_ID` only to a client ID that xAI has
approved for your DJcode integration and the requested scopes. DJcode does not
ship or borrow the Grok CLI or OpenCode client identity. No DJcode registration
has been obtained or verified by this release; ordinary installs show this
method as unavailable and offer `XAI_API_KEY` instead. Having a client ID alone
does not guarantee subscription eligibility, model access, or successful inference.

The implemented protocol uses device authorization, a maximum ten-minute poll
window, pending/slow-down handling, cancellation, token refresh, and an atomic
0600 credential file at `$DJCODE_CONFIG_DIR/accounts/xai.json` (normally
`~/.djcode/accounts/xai.json`). Tokens are sent only to `https://api.x.ai/v1` and
the official xAI token endpoint; account mode rejects custom API gateways.
Tokens are separate from configuration and must never appear in config dumps.
Refresh is serialized across subagents in one process; separate simultaneous
DJcode processes can still contend for rotating refresh tokens and require a
new login. Logout removes only DJcode's local credential file; it does not revoke
the upstream account grant. API-key mode is independent of account mode.

ChatGPT/Codex subscription login is supported by the official Codex clients.
DJcode currently has neither its own approved direct ChatGPT OAuth registration
nor a Codex Responses account backend. Its OpenAI integration uses API keys.
Anthropic explicitly prohibits Claude.ai subscription OAuth in third-party
products, so DJcode offers Anthropic API keys, not Claude subscription login.

Reviewed primary sources, 2026-09-09:

- [OpenAI authentication](https://learn.chatgpt.com/docs/auth) documents official
  Codex browser/device login. It does not establish a DJcode OAuth registration.
- [Anthropic legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
  states the third-party subscription-token restriction.
- [xAI's Kilo Code announcement](https://x.ai/news/grok-kilocode) establishes that
  provider-supported third-party account integrations exist; it does not grant
  DJcode a registration.
- [xAI discovery](https://auth.x.ai/.well-known/openid-configuration) advertises
  device authorization, public-client authentication, refresh, and scopes.
- [OpenCode providers](https://opencode.ai/docs/providers/) and MIT-licensed
  [xAI plugin](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/plugin/xai.ts)
  (blob `a455121d6aedfac1ee9f68bd4f6d455ce5a86eba`) were inspected for protocol
  behavior. Its reused Grok client identity was deliberately not adopted.
  DJcode's implementation is original Python; no OpenCode code was copied.

Mock-transport tests exercise grants and backend routing without browser consent,
live account login, paid inference, or model downloads. A successful real account
sign-in and inference remain unverified until an approved client registration
and a consenting eligible account are available.
