# XHS Workflow Orchestrator Adaptation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Convert `/Users/chengren17/.openclaw/projects/paper-xhs-3min-workflow` into a first-class orchestrator workflow profile with fixed high-quality stage prompts, and enforce role executor routing (`coder -> claude_code`, `debugger -> codex_cli`).

**Architecture:** Add a workflow bootstrap path in orchestrator runtime that materializes stage tasks + dependencies + per-task fixed prompts from template files. Add configurable role executor routing in spawn planning so runtime chooses bridge executors by role. Preserve existing behavior for unrelated roles and manual dispatch.

**Tech Stack:** Python 3, file-backed state (`state/tasks.snapshot.json`, `state/task-context-map.json`), OpenClaw CLI bridges, Claude Code CLI (`claude -p`), Codex CLI (`codex exec`), unittest.

---

### Task 1: Add XHS workflow profile + fixed stage prompt templates

**Files:**
- Create: `templates/workflows/paper-xhs-3min/stage-a0-extract.md`
- Create: `templates/workflows/paper-xhs-3min/stage-a-ingest.md`
- Create: `templates/workflows/paper-xhs-3min/stage-b-summary-draft.md`
- Create: `templates/workflows/paper-xhs-3min/stage-c-citation-check.md`
- Create: `templates/workflows/paper-xhs-3min/stage-d-publish.md`
- Create: `templates/workflows/paper-xhs-3min/stage-e-image-prompts.md`
- Create: `templates/workflows/paper-xhs-3min/stage-f-kb.md`
- Create: `templates/workflows/paper-xhs-3min/stage-g-quality-gate.md`
- Create: `templates/workflows/paper-xhs-3min/stage-h-conversion.md`
- Create: `templates/workflows/paper-xhs-3min/stage-i-weekly-review.md`

**Step 1: Create template directory + template files**
- Write one fixed template per stage.
- Template content must include:
  - explicit input/output file paths
  - acceptance criteria
  - required evidence format
  - required JSON output shape (`taskId/agent/status/summary/changes/evidence/risks/nextActions`)

**Step 2: Ensure placeholders are explicit and render-safe**
- Use placeholders only from a known set: `{paper_id}`, `{workflow_root}`, `{run_dir}`, `{pdf_path}`.
- Avoid shell interpolation syntax inside templates.

**Step 3: Manual quality check**
- Confirm each stage template is non-empty and includes at least one concrete artifact path and one verification requirement.

**Step 4: Commit**
```bash
git add templates/workflows/paper-xhs-3min
git commit -m "feat(workflow): add fixed prompt templates for paper-xhs-3min stages"
```

### Task 2: Implement workflow bootstrap command (create tasks + dependencies + prompt binding)

**Files:**
- Modify: `scripts/lib/milestones.py`
- Modify: `scripts/lib/task_decomposer.py` (if role mapping enrichment needed)
- Test: `tests/test_orchestrator_runtime.py`

**Step 1: Add workflow profile constants and loaders**
- Add constants for default workflow root (`/Users/chengren17/.openclaw/projects/paper-xhs-3min-workflow`) and stage definitions.
- Add helper to load stage template files and render placeholders.

**Step 2: Extend task-context state model for dispatch prompt binding**
- Add helpers to bind/read per-task `dispatchPrompt` while keeping existing `projectPath/projectName` data.
- Ensure backward compatibility when old state entries lack new fields.

**Step 3: Make dispatch use bound prompt first**
- In `dispatch_once`, resolve objective order:
  1) explicit `--task`
  2) bound task `dispatchPrompt`
  3) fallback `"<taskId>: <title>"`
- Keep short clip only for group message display; do not clip prompt objective.

**Step 4: Add bootstrap command path**
- Add `cmd_xhs_bootstrap` in `milestones.py`:
  - validate workflow root + pdf path
  - create run directory marker context
  - create stage tasks with owner hints and deterministic dependsOn chain
  - bind project path + fixed dispatch prompt for each task
  - write dependency IDs to snapshot
  - optional kickoff first runnable task via existing dispatch logic

**Step 5: Add Feishu command mapping**
- In `cmd_feishu_router`, add command:
  - `@orchestrator 开始xhs流程 <paper_id> <pdf_path>`
  - alias: `@orchestrator start xhs workflow <paper_id> <pdf_path>`
- Return structured JSON with `createdTaskIds`, `dependsOnSync`, `bootstrap`.

**Step 6: Add tests**
- Add runtime tests verifying:
  - bootstrap creates expected number/order of tasks
  - bound `dispatchPrompt` is used by `run/dispatch`
  - dependencies are linked in snapshot

**Step 7: Run tests**
```bash
python3 -m unittest tests/test_orchestrator_runtime.py -v
```

**Step 8: Commit**
```bash
git add scripts/lib/milestones.py tests/test_orchestrator_runtime.py
git commit -m "feat(workflow): bootstrap paper-xhs-3min tasks with fixed prompt bindings"
```

### Task 3: Add role executor routing with Claude/Codex bridges

**Files:**
- Create: `scripts/lib/claude_worker_bridge.py`
- Modify: `scripts/lib/codex_worker_bridge.py`
- Modify: `scripts/lib/milestones.py`
- Modify: `config/runtime-policy.json`
- Modify: `config/runtime-policy.example.json`
- Modify: `docs/protocol.md`
- Test: `tests/test_orchestrator_runtime.py`

**Step 1: Add executor routing resolver**
- Add `load_executor_routing(root)` helper in `milestones.py`:
  - default: `coder -> claude_cli`, `debugger -> codex_cli`, other roles -> `openclaw_agent`
  - allow config override from runtime policy section.

**Step 2: Add Claude bridge**
- Implement `claude_worker_bridge.py` analogous to codex bridge:
  - resolve workspace from task context
  - run `claude -p --output-format json --json-schema ...`
  - normalize response into orchestrator schema
  - support `CLAUDE_WORKER_FAKE_OUTPUT` for deterministic tests
  - support timeout semantics (`0` = no timeout)

**Step 3: Update spawn plan resolver**
- In `resolve_spawn_plan`:
  - keep `--spawn-cmd` highest priority
  - apply role executor routing
  - emit planned command for `claude_cli` or `codex_cli` bridges

**Step 4: Update codex bridge fallback workspace behavior**
- Prefer mapped project path.
- Fallback to role workspace (`~/.openclaw/agents/<agent>/workspace`) before generic fallback.

**Step 5: Update tests**
- Update/extend existing routing tests:
  - `coder` planned executor is `claude_cli`
  - `debugger` planned executor is `codex_cli`
  - unrelated role remains `openclaw_agent`

**Step 6: Run tests**
```bash
python3 -m unittest tests/test_orchestrator_runtime.py -v
```

**Step 7: Commit**
```bash
git add scripts/lib/claude_worker_bridge.py scripts/lib/codex_worker_bridge.py scripts/lib/milestones.py config/runtime-policy.json config/runtime-policy.example.json docs/protocol.md tests/test_orchestrator_runtime.py
git commit -m "feat(executor): route coder to Claude Code and debugger to Codex CLI"
```

### Task 4: Documentation + final verification + push

**Files:**
- Modify: `docs/config.md`
- Modify: `docs/protocol.md`
- Modify: `README.md` (if needed)

**Step 1: Document XHS workflow entrypoint and command examples**
- Include:
  - bootstrap command syntax
  - expected per-stage behavior
  - fixed prompt source location

**Step 2: Document executor routing behavior**
- Include defaults and override mechanism.

**Step 3: Run full verification suite**
```bash
python3 -m unittest tests/test_budget_governance.py -v
python3 -m unittest tests/test_orchestrator_runtime.py -v
python3 -m unittest discover -s tests -v
```

**Step 4: Final commit + push**
```bash
git add docs/config.md docs/protocol.md README.md
git commit -m "docs: add xhs workflow bootstrap and executor routing usage"
git push
```

### Task 5: Acceptance smoke checks (dry-run + real command parse)

**Files:**
- N/A (execution checks only)

**Step 1: Dry-run bootstrap command**
```bash
python3 scripts/lib/milestones.py xhs-bootstrap --root <tmp_root> --paper-id A1 --pdf-path <pdf> --workflow-root /Users/chengren17/.openclaw/projects/paper-xhs-3min-workflow --mode dry-run --spawn
```
Expected:
- returns `createdTaskIds`
- first task has bound `dispatchPrompt`
- dispatch payload includes full fixed prompt objective

**Step 2: Feishu route parsing smoke**
```bash
python3 scripts/lib/milestones.py feishu-router --root <tmp_root> --actor orchestrator --text "@orchestrator 开始xhs流程 A1 /abs/path/to/A1.pdf" --mode dry-run
```
Expected:
- `intent="xhs_bootstrap"`
- response contains bootstrap summary and ack send payload

**Step 3: Executor routing smoke**
- Create coder/debugger tasks and dispatch dry-run with `--spawn --spawn-output`.
Expected:
- `spawn.executor` is `claude_cli` for coder and `codex_cli` for debugger.

