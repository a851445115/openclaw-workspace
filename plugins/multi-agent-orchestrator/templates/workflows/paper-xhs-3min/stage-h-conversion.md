# Stage H - Conversion Package Export

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/outputs/d-publish/xhs-final.md
- {run_dir}/assets/e-images/prompts.md
- {run_dir}/qa/g-gate/gate-score.json

## Required Outputs
- {run_dir}/deliverables/h-conversion/post-package.json
- {run_dir}/deliverables/h-conversion/channel-adapters.md
- {run_dir}/deliverables/h-conversion/release-checklist.md

## Acceptance Criteria
- post-package.json includes final text, image prompt references, and metadata.
- channel-adapters.md explains any platform-specific wording changes.
- release-checklist.md confirms all required deliverables are present.

## Verification And Evidence Requirements
- Provide command output proving post-package.json is valid JSON.
- Evidence must reference release-checklist.md and channel-adapters.md.

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
