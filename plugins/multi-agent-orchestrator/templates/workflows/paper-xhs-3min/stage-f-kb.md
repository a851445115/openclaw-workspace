# Stage F - Knowledge Base Update

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/outputs/d-publish/xhs-final.md
- {run_dir}/qa/c-citation/fact-check.json

## Required Outputs
- {run_dir}/kb/f-kb/insights.md
- {run_dir}/kb/f-kb/entities.json
- {run_dir}/kb/f-kb/update-log.md

## Acceptance Criteria
- insights.md captures durable lessons and reusable patterns.
- entities.json includes normalized terms and relations extracted from the paper.
- update-log.md records what was added, updated, or skipped.

## Verification And Evidence Requirements
- Provide command output showing entities.json key count.
- Evidence must include update-log.md and one entity ID from entities.json.

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
