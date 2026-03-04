# Stage A - Ingest Extracted Material Into Run Workspace

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/artifacts/a0-extract/raw-text.md
- {run_dir}/artifacts/a0-extract/metadata.json

## Required Outputs
- {run_dir}/workspace/a-ingest/normalized-paper.md
- {run_dir}/workspace/a-ingest/claims.jsonl
- {run_dir}/workspace/a-ingest/ingest-checklist.md

## Acceptance Criteria
- normalized-paper.md removes duplicated headers/footers and preserves section order.
- claims.jsonl contains one atomic claim per line with a source section reference.
- ingest-checklist.md marks all required ingest checks as pass/fail.

## Verification And Evidence Requirements
- Provide command output proving claims.jsonl has at least 5 lines.
- Evidence must include paths for normalized-paper.md and ingest-checklist.md.

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
