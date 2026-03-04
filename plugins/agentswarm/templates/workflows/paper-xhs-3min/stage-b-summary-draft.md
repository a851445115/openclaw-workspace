# Stage B - Draft XHS Summary

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/workspace/a-ingest/normalized-paper.md
- {run_dir}/workspace/a-ingest/claims.jsonl

## Required Outputs
- {run_dir}/outputs/b-summary/xhs-draft.md
- {run_dir}/outputs/b-summary/key-points.json
- {run_dir}/outputs/b-summary/style-notes.md

## Acceptance Criteria
- xhs-draft.md includes opening hook, 3-5 core points, and a closing takeaway.
- key-points.json maps each point to source claim IDs.
- style-notes.md states target audience and tone constraints.

## Verification And Evidence Requirements
- Include a word-count check for xhs-draft.md.
- Evidence must reference key-points.json and show at least one mapped claim ID.

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
