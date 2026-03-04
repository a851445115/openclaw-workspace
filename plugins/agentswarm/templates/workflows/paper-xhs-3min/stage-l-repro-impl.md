# Stage L - Core Model And Algorithm Implementation

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/repro/j-scope/method-mapping.md
- {run_dir}/repro/k-data/dataset-manifest.json

## Required Outputs
- {run_dir}/repro/l-impl/src/model.py
- {run_dir}/repro/l-impl/src/train.py
- {run_dir}/repro/l-impl/configs/repro-config.yaml
- {run_dir}/repro/l-impl/tests/test_algorithm_contract.py

## Acceptance Criteria
- model.py and train.py implement the paper's core modules and training loop with one-to-one component mapping.
- repro-config.yaml exposes key hyperparameters described in the paper.
- test_algorithm_contract.py validates critical algorithm behavior (not only smoke import).

## Verification And Evidence Requirements
- Provide command output for running test_algorithm_contract.py successfully.
- Evidence must include at least one explicit mapping: paper component -> code symbol/function/class.

## Non-Negotiable Integrity Rules
- No hardcoded final metrics in code.
- No placeholder algorithm branches that bypass core logic.

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
