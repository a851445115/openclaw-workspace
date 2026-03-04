# Stage G - Quality Gate Review

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/outputs/d-publish/xhs-final.md
- {run_dir}/assets/e-images/prompts.md
- {run_dir}/qa/c-citation/fact-check.json

## Required Outputs
- {run_dir}/qa/g-gate/gate-report.md
- {run_dir}/qa/g-gate/gate-score.json
- {run_dir}/qa/g-gate/remediation-items.md

## Acceptance Criteria
- gate-report.md covers factual accuracy, readability, and policy compliance.
- gate-score.json provides numeric scores and pass/fail thresholds.
- remediation-items.md is empty or includes actionable fixes with owners.

## Verification And Evidence Requirements
- Provide command output proving gate-score.json includes overallScore.
- Evidence must include remediation-items.md and final pass/fail decision.

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
