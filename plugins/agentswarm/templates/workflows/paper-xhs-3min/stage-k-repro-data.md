# Stage K - Reproduction Data Pipeline With Synthetic Fallback

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/repro/j-scope/repro-plan.md
- {run_dir}/repro/j-scope/method-mapping.md

## Required Outputs
- {run_dir}/repro/k-data/scripts/build_dataset.py
- {run_dir}/repro/k-data/dataset-manifest.json
- {run_dir}/repro/k-data/data/README.md

## Acceptance Criteria
- build_dataset.py can be executed end-to-end and outputs the dataset files declared in dataset-manifest.json.
- If original data is unavailable, synthetic data generation must be script-driven and documented in data/README.md.
- dataset-manifest.json records source type (original/synthetic), sample counts, and random seed.

## Verification And Evidence Requirements
- Provide command output from running build_dataset.py and reporting sample counts.
- Evidence must include dataset-manifest.json and one generated data file path.

## Non-Negotiable Integrity Rules
- Do not manually fabricate dataset rows.
- Do not claim dataset creation success without script execution evidence.

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
