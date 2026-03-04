# Stage A0 - Extract Source Text and Metadata

## Context
- paper_id: {paper_id}
- workflow_root: {workflow_root}
- run_dir: {run_dir}
- pdf_path: {pdf_path}

## Inputs
- Primary source PDF: {pdf_path}

## Required Outputs
- {run_dir}/artifacts/a0-extract/raw-text.md
- {run_dir}/artifacts/a0-extract/metadata.json
- {run_dir}/artifacts/a0-extract/extract-log.txt

## Acceptance Criteria
- Raw text includes title, abstract, and all section headings from the PDF.
- metadata.json includes paper_id, title, authors, venue, and year fields.
- Extraction log records any OCR or parsing fallback used.

## Verification And Evidence Requirements
- Run and report a line-count check for raw-text.md.
- Include one evidence item that references metadata.json and one that references extract-log.txt.

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
