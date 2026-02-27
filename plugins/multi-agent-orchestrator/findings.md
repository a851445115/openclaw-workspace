# Findings

## 2026-02-28
- Session started; pending repository inspection.
- `scripts/lib/milestones.py` already supports command parsing + wake-up + Chinese milestones; runtime gap was inbound wrapper wiring at orchestrator workspace level.
- Added `scripts/feishu-inbound-router` to extract group/sender/message from OpenClaw Feishu wrapper and forward to `scripts/orchestrator-router`.
- Updated orchestrator workspace `AGENTS.md` + `BOOTSTRAP.md` so runtime behavior routes mentions before free-form replies.
- Existing behavior note: `create_project` / `status` paths in `milestones.py` still call `send_group_message` even when `--milestones off`; use `dry-run` (not `off`) for safe testing.

