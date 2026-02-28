# Findings

## 2026-02-28
- `cmd_dispatch` previously only sent `[CLAIM]/[TASK]` and waited for report; no spawn result ingestion or board writeback.
- `feishu-router` only handled create/run/status + wake-up report; explicit command intents (`claim/done/block/synthesize/escalate/dispatch/clarify`) were not wired from group messages.
- `recover-stale-locks` and `rebuild-snapshot` were pure TODO stubs.
- Dry-run semantics required special handling: when spawn is skipped in dry-run, dispatch should remain manual (wait-for-report) instead of auto-blocking.
- Bot loop risk exists when orchestrator/bot milestones are re-ingested from Feishu wrappers; guard needed before routing.
