# Stage J - Reproduction Scope And Hypothesis Mapping

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {pdf_path}
- {run_dir}/outputs/d-publish/xhs-final.md
- {run_dir}/reviews/i-weekly/weekly-review.md

## Required Outputs
- {run_dir}/repro/j-scope/repro-plan.md
- {run_dir}/repro/j-scope/hypothesis-matrix.json
- {run_dir}/repro/j-scope/method-mapping.md

## Acceptance Criteria
- repro-plan.md lists the full reproduction sequence with measurable milestones.
- hypothesis-matrix.json includes each core claim and its expected observable metric.
- method-mapping.md maps paper sections (model/algorithm/training/inference) to concrete implementation modules.

## Verification And Evidence Requirements
- Provide command output proving hypothesis-matrix.json is valid JSON.
- Evidence must include at least one paper section -> code module mapping entry.

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
