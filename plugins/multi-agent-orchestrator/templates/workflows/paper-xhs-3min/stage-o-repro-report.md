# Stage O - Reproduction Report And Artifact Package

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/repro/n-audit/repro-audit.md
- {run_dir}/repro/n-audit/metric-diff.json
- {run_dir}/repro/m-run/run-manifest.json

## Required Outputs
- {run_dir}/repro/o-report/reproduction-report.md
- {run_dir}/repro/o-report/artifact-index.json
- {run_dir}/repro/o-report/handoff-notes.md

## Acceptance Criteria
- reproduction-report.md summarizes implemented scope, reproducibility status, and remaining gaps.
- artifact-index.json lists all reproducibility artifacts with path, type, and purpose.
- handoff-notes.md gives deterministic rerun instructions.

## Verification And Evidence Requirements
- Provide command output proving artifact-index.json is parseable JSON.
- Evidence must include at least one rerun command from handoff-notes.md.

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
