---
name: cc-switch-mcp
description: Import MCP servers managed by CC Switch (Claude/CC config) into a mcporter config and call them via mcporter.
homepage: https://github.com
metadata:
  {
    "openclaw": {
      "emoji": "\ud83e\udde9",
      "requires": { "bins": ["mcporter"] }
    }
  }
---

# CC Switch MCP (mcporter bridge)

This skill lets OpenClaw workflows use MCP servers that are managed by CC Switch / Claude's MCP config.

OpenClaw does not (yet) expose arbitrary MCP tools as first-class OpenClaw tools. The reliable bridge is:

- Use `mcporter` to talk to MCP servers
- Run `mcporter` via the `exec` tool

## Config location (recommended)

Use a workspace-local config so it is deterministic and does not touch system state:

- `~/.openclaw/agents/coder/workspace/config/mcporter.json`

## Import all MCP servers from Claude config

CC Switch typically writes MCP servers into `~/.claude.json`.

Run:

```bash
mkdir -p ~/.openclaw/agents/coder/workspace/config
mcporter --config ~/.openclaw/agents/coder/workspace/config/mcporter.json \
  config import claude --path ~/.claude.json --copy
```

## List servers and tools

```bash
mcporter --config ~/.openclaw/agents/coder/workspace/config/mcporter.json list
mcporter --config ~/.openclaw/agents/coder/workspace/config/mcporter.json list <server> --schema
```

## Call a tool

```bash
mcporter --config ~/.openclaw/agents/coder/workspace/config/mcporter.json \
  call <server>.<tool> key=value
```

## Notes / safety

- Do not print API keys/tokens to chat logs. Prefer redacting tool output when it includes `env` or `headers`.
- Some SSE servers require OAuth (`mcporter auth <server>`).
- If CC Switch updates servers, re-run the import command.
