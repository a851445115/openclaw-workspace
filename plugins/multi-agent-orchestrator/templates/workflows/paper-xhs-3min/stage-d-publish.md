# Stage D - Publish-Ready Post Assembly

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/outputs/b-summary/xhs-draft.md
- {run_dir}/qa/c-citation/fact-check.json

## Required Outputs
- {run_dir}/outputs/d-publish/xhs-final.md
- {run_dir}/outputs/d-publish/publish-checklist.md
- {run_dir}/outputs/d-publish/post-metadata.json

## Acceptance Criteria
- xhs-final.md resolves all failed checks or clearly marks unresolved risks.
- publish-checklist.md includes title quality, readability, and compliance checks.
- post-metadata.json includes paper_id, run_dir, and publish timestamp field.

## Verification And Evidence Requirements
- Provide a markdown lint or basic format check output for xhs-final.md.
- Evidence must reference publish-checklist.md and post-metadata.json.

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
