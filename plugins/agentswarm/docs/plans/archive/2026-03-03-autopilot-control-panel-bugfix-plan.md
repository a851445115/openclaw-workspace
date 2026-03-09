# Autopilot + Control Panel Bugfix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix two production issues: (1) autopilot stops after one task, (2) control-panel card actions fail in Feishu with callback errors.

**Architecture:** Keep the existing orchestrator/task-board architecture, but harden two integration seams. Inside the plugin, make "auto progress" actually drive the scheduler kernel and make control-panel button commands map to real executable intents. Inside OpenClaw Feishu channel code, add explicit card-action event handling with fast acknowledgment and background dispatch to avoid callback timeout/error in Feishu UI.

**Tech Stack:** Python (plugin scripts/tests), TypeScript (OpenClaw Feishu extension), unittest, vitest.

---

### Task 1: Reproduce and lock down regressions with failing tests (plugin)

**Files:**
- Modify: `tests/test_orchestrator_runtime.py`
- Test target: `tests/test_orchestrator_runtime.py`

**Step 1: Write failing test for auto-progress scheduler linkage**

- Add a test that calls `feishu-router` with `@orchestrator 自动推进 开 2`.
- Assert expected behavior:
  - `intent == "auto_progress"`
  - auto-progress state is enabled
  - scheduler kernel state is also enabled (regression lock)

**Step 2: Run targeted test to verify it fails before fix**

Run: `python3 -m unittest tests.test_orchestrator_runtime.OrchestratorRuntimeTests.test_user_friendly_autopilot_toggle_commands -v`
Expected: FAIL because scheduler state stays disabled today.

**Step 3: Write failing test for control-panel "推进一次" callback command**

- Add a callback-driven feishu-router test using `@orchestrator 推进一次` command path.
- Assert command is handled as "advance once" (not wakeup fallback).

**Step 4: Run targeted test to verify it fails before fix**

Run: `python3 -m unittest tests.test_feishu_card_callback.FeishuCardCallbackTests -v`
Expected: FAIL on new advance-once expectation.

**Step 5: Commit (tests only, optional split commit)**

```bash
git add tests/test_orchestrator_runtime.py tests/test_feishu_card_callback.py
git commit -m "test: lock regressions for auto-progress and control-panel callbacks"
```

### Task 2: Fix plugin runtime behavior (auto-progress + control panel command mapping)

**Files:**
- Modify: `scripts/lib/milestones.py`
- Test: `tests/test_orchestrator_runtime.py`
- Test: `tests/test_feishu_card_callback.py`

**Step 1: Implement scheduler linkage for auto-progress**

- In `feishu-router` `自动推进` branch:
  - On `on`, persist auto-progress state and enable scheduler kernel with `maxSteps` synchronization.
  - Keep one immediate kickoff tick.
  - Return scheduler/daemon status in JSON payload for observability.
  - On `off`, disable scheduler kernel as well.

**Step 2: Add daemon bootstrap helper for send mode**

- Add helper in `milestones.py` to ensure `scheduler-daemon` is running in background when auto-progress turns on (`mode=send`).
- Reuse `state/scheduler.daemon.json` and PID checks to avoid duplicate daemons.
- Fail-soft: if daemon bootstrap fails, return warning fields and keep command response stable.

**Step 3: Add explicit "推进一次" command support**

- Add parser branch that maps `@orchestrator 推进一次` to one-step autopilot execution (`max_steps=1`).
- Return a clear intent and structured result, avoiding wakeup fallback.

**Step 4: Re-run targeted tests**

Run:
- `python3 -m unittest tests.test_orchestrator_runtime -v`
- `python3 -m unittest tests.test_feishu_card_callback -v`

Expected: PASS.

**Step 5: Commit plugin fix**

```bash
git add scripts/lib/milestones.py tests/test_orchestrator_runtime.py tests/test_feishu_card_callback.py
git commit -m "fix: make auto-progress continuous and harden control-panel callbacks"
```

### Task 3: Fix Feishu channel card callback handling (OpenClaw extension)

**Files:**
- Modify: `/opt/homebrew/lib/node_modules/openclaw/extensions/feishu/src/monitor.ts`
- Add/Modify tests under: `/opt/homebrew/lib/node_modules/openclaw/extensions/feishu/src/`

**Step 1: Add card action event registration**

- Register `card.action.trigger` in `registerEventHandlers`.
- Ensure callback handler:
  - Extracts `command` from action payload.
  - Builds a synthetic message event compatible with existing message pipeline.
  - Dispatches in fire-and-forget mode (non-blocking).
  - Returns success quickly to avoid Feishu callback timeout.

**Step 2: Add unit tests for callback command extraction and dispatch trigger**

- Cover cases:
  - command in object value
  - command in stringified value
  - missing command (ignored gracefully)

**Step 3: Run extension tests**

Run: `pnpm -C /opt/homebrew/lib/node_modules/openclaw test --filter feishu`
Expected: PASS for changed scope.

**Step 4: Commit extension fix**

```bash
cd /opt/homebrew/lib/node_modules/openclaw
git checkout -b codex/feishu-card-callback-fix
git add extensions/feishu/src/monitor.ts extensions/feishu/src/*.test.ts
git commit -m "fix(feishu): handle card action callbacks with fast ack and dispatch"
```

### Task 4: End-to-end validation and rollout notes

**Files:**
- Modify: `docs/reliability.md`
- Modify: `README.md`

**Step 1: Validate plugin + extension integration locally**

- Send `@orchestrator 控制台` in Feishu group.
- Click buttons:
  - `推进一次`
  - `自动推进开`
  - `自动推进关`
  - `查看阻塞`
  - `验收摘要`
- Confirm no `code 200672`, and commands are executed.

**Step 2: Validate autopilot continuity**

- Create 3+ pending tasks.
- Enable auto-progress.
- Confirm scheduler daemon state transitions and multiple tasks advance over time.

**Step 3: Document operational behavior**

- Update docs with:
  - auto-progress now auto-links to scheduler
  - daemon bootstrap behavior
  - callback prerequisites and troubleshooting checklist

**Step 4: Commit docs**

```bash
git add README.md docs/reliability.md docs/plans/2026-03-03-autopilot-control-panel-bugfix-plan.md
git commit -m "docs: document autopilot and control-panel callback reliability"
```

