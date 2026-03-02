# Stage C - Citation And Factual Check

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/outputs/b-summary/xhs-draft.md
- {run_dir}/workspace/a-ingest/claims.jsonl

## Required Outputs
- {run_dir}/qa/c-citation/citation-audit.md
- {run_dir}/qa/c-citation/fact-check.json
- {run_dir}/qa/c-citation/blockers.md

## Acceptance Criteria
- citation-audit.md lists every factual statement and its source section.
- fact-check.json records pass/fail and confidence for each statement.
- blockers.md is empty or lists unresolved factual issues with owner + next step.

## Verification And Evidence Requirements
- Provide command output proving fact-check.json is valid JSON.
- Evidence must include blockers.md and mention whether blockers exist.

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
