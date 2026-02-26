# CLAUDE.md - OpenClaw Workspace

This file provides guidance to Claude Code when working with this OpenClaw workspace.

## Project Overview

OpenClaw is a personal AI assistant framework with the following capabilities:
- **Identity System**: Configurable AI persona (IDENTITY.md, SOUL.md)
- **Memory System**: Daily notes (memory/) and long-term memory (MEMORY.md)
- **Skills System**: Modular skills in SKILLS/ directory
- **Plugins System**: Extensible plugins in plugins/ directory
- **Multi-Agent Orchestration**: Support for spawning and coordinating sub-agents

## Key Files

| File | Purpose |
|------|---------|
| `IDENTITY.md` | AI persona definition (name: 钢镚儿, Abyssinian cat) |
| `SOUL.md` | Core behavioral principles and boundaries |
| `USER.md` | User profile and context |
| `AGENTS.md` | Workspace rules, memory protocols, heartbeat guidelines |
| `MEMORY.md` | Long-term curated memories and important rules |
| `TOOLS.md` | Environment-specific configuration notes |

## Directory Structure

```
.openclaw/workspace/
├── .claude/          # Claude Code configuration
├── .clawhub/         # ClawHub package registry
├── memory/           # Daily memory files (YYYY-MM-DD.md)
├── plugins/          # Plugin modules
│   ├── feishu-local/ # Feishu integration skills
│   └── memory-lancedb-pro/ # LanceDB memory storage
├── projects/         # User projects
├── SKILLS/           # Skill modules
│   ├── browser-use/  # Browser automation
│   └── fast-browser-use/ # Rust-based browser automation
└── system-prompts/   # System prompt templates
```

## Development Guidelines

### Memory Rules
1. **Double-layer storage**: Store both technical and principle memories to LanceDB
2. **Recall before retry**: Always recall memories before retrying failed operations
3. **Atomic entries**: Keep memory entries short (<500 chars) and structured

### Plugin Development
- After modifying `.ts` files under `plugins/`, MUST clear jiti cache:
  ```bash
  rm -rf /tmp/jiti/
  ```
- Then restart gateway: `openclaw gateway restart`

### Cron Configuration
- Do NOT add `jobs` field in `openclaw.json`
- Use `openclaw cron add` command to add scheduled tasks
- Cron jobs are defined in `~/.openclaw/cron/jobs.json`

## User Context

- **Name**: 锐
- **Location**: Fuzhou University, Graduate Student (Year 3)
- **Major**: Electrical Engineering
- **Interests**: Fitness (Bench Press PR: 150kg)
- **Side Business**: Academic paper replication services

## AI Persona

- **Name**: 钢镚儿
- **Creature**: Abyssinian Cat
- **Personality**: Cheerful, lively, cute, skilled in project design and agent orchestration
- **Emoji**: 😼

## Session Protocol

At the start of each session:
1. Read `SOUL.md` - behavioral principles
2. Read `USER.md` - user context
3. Read recent `memory/YYYY-MM-DD.md` files
4. In main session: also read `MEMORY.md`
