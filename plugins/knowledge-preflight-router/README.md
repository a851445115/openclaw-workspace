# knowledge-preflight-router plugin

User-space plugin scaffold for hard preflight routing.

This plugin is designed to live under `~/.openclaw/workspace/plugins/knowledge-preflight-router` so OpenClaw upgrades do not overwrite it.

## What it enforces

- Preflight before main execution for `claude-code` / `codex` / `openclaw`
- Configurable skip behavior (`onFailure`: `block|warn|fallback`)
- Auto-mark `used` events from retrieved items (minimal runnable implementation)

## Config

See `config/defaults.json`.

## Runtime linkage

`bin/preflight-router.sh` delegates to the repository script:
`/Users/chengren17/cross-platform-evolution/scripts/run_preflight.sh`

