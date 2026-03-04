# Stage I - Weekly Review Synthesis

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/deliverables/h-conversion/post-package.json
- {run_dir}/qa/g-gate/gate-report.md

## Required Outputs
- {run_dir}/reviews/i-weekly/weekly-review.md
- {run_dir}/reviews/i-weekly/metric-trends.json
- {run_dir}/reviews/i-weekly/follow-ups.md

## Acceptance Criteria
- weekly-review.md summarizes wins, misses, and hypotheses for next run.
- metric-trends.json contains at least quality score trend and engagement proxy.
- follow-ups.md lists concrete next actions and owners.

## Verification And Evidence Requirements
- Provide command output proving metric-trends.json is parseable JSON.
- Evidence must include at least one follow-up action from follow-ups.md.

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
