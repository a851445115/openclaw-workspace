# Stage N - Reproduction Integrity Audit

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/repro/j-scope/method-mapping.md
- {run_dir}/repro/m-run/metrics/metrics.json
- {run_dir}/repro/m-run/logs/train.log
- {run_dir}/repro/m-run/run-manifest.json

## Required Outputs
- {run_dir}/repro/n-audit/repro-audit.md
- {run_dir}/repro/n-audit/metric-diff.json
- {run_dir}/repro/n-audit/no-shortcut-checklist.md

## Acceptance Criteria
- repro-audit.md verifies each paper core component has executed evidence.
- metric-diff.json compares reproduced metrics vs paper target metrics with explicit gaps.
- no-shortcut-checklist.md confirms no fabricated metrics/logs/hardcoded shortcuts.

## Verification And Evidence Requirements
- Provide command output proving metric-diff.json is valid JSON.
- Evidence must include at least one direct reference from audit finding to log line or artifact file.

## JSON Output Schema (MUST return exactly one JSON object)
```json
{
  "taskId": "<task-id>",
  "agent": "<agent-role>",
  "status": "done|blocked|progress",
  "summary": "one-line outcome",
  "changes": [
    {
      "path": "<file-or-dir-path>",
      "summary": "what changed"
    }
  ],
  "evidence": [
    "<verification command output / file path / link>"
  ],
  "risks": [
    "<risk or empty string>"
  ],
  "nextActions": [
    "<next action or empty string>"
  ]
}
```
