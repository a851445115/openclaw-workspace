# Stage M - Experiment Execution And Metric Collection

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- {run_dir}/repro/l-impl/src/model.py
- {run_dir}/repro/l-impl/src/train.py
- {run_dir}/repro/l-impl/configs/repro-config.yaml
- {run_dir}/repro/k-data/dataset-manifest.json

## Required Outputs
- {run_dir}/repro/m-run/logs/train.log
- {run_dir}/repro/m-run/metrics/metrics.json
- {run_dir}/repro/m-run/checkpoints/last.ckpt
- {run_dir}/repro/m-run/run-manifest.json

## Acceptance Criteria
- Training and evaluation commands run successfully and produce artifacts.
- metrics.json values are generated from executed runs, not manually edited.
- run-manifest.json records command, git/version info, random seed, and runtime environment.

## Verification And Evidence Requirements
- Provide executed command lines and key output snippets from train.log.
- Evidence must include metrics.json and checkpoint file paths.

## Non-Negotiable Integrity Rules
- Do not fabricate run logs or metric files.
- Do not mark done if core training/evaluation command was not executed.

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
