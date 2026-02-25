# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

## OpenClaw Cron 配置规则

**⚠️ 重要：不要在 openclaw.json 里添加 jobs 字段！**

正确的配置方式：
- `openclaw.json` 里只需要 `"cron": { "enabled": true }`
- cron 任务定义在独立文件：`~/.openclaw/cron/jobs.json`
- 在 openclaw.json 里添加 `jobs` 字段会导致配置校验失败，影响整个配置加载

使用 `openclaw cron add` 命令来添加定时任务，不要手动编辑 openclaw.json。

---

Add whatever helps you do your job. This is your cheat sheet.
