# Stage E - Image Prompt Package

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/outputs/d-publish/xhs-final.md
- {run_dir}/outputs/d-publish/post-metadata.json

## Required Outputs
- {run_dir}/assets/e-images/prompts.md
- {run_dir}/assets/e-images/shot-list.json
- {run_dir}/assets/e-images/style-guardrails.md

## Acceptance Criteria
- prompts.md includes at least 3 image prompts aligned with key points.
- shot-list.json maps prompt IDs to post sections.
- style-guardrails.md defines forbidden visual tropes and brand-safe constraints.

## Verification And Evidence Requirements
- Provide command output proving shot-list.json parses successfully.
- Evidence must mention one prompt ID and its mapped section.

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
