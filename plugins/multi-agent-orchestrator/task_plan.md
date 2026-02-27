# Task Plan - Simple Wake-up v1 Feishu wiring

## Goal
Make orchestrator react to Feishu group @mentions in real chats by routing messages through `scripts/orchestrator-router`, updating `state/tasks.jsonl`, and posting concise Chinese milestones (`[DONE]` / `[BLOCKED]` / `[TASK]`).

## Phases
| Phase | Status | Notes |
|---|---|---|
| 1. Inspect current implementation/docs | complete | Existing router/milestones already implemented; missing inbound runtime wiring |
| 2. Design minimal-risk integration | complete | Use orchestrator workspace instructions + lightweight inbound wrapper parser script |
| 3. Implement code/config/doc updates | complete | Added inbound parser script and updated orchestrator workspace/docs |
| 4. Validate with local checks | complete | Verified create-project and wake-up parse with dry-run fixtures |
| 5. Document Feishu group test checklist | complete | Added checklist to README and wiring notes |

## Constraints
- No V2 background wake-up implementation.
- Keep changes minimal, reversible, and documented.
- Reuse existing message tool path for outbound milestones.

## Errors Encountered
- `--milestones off` does not suppress all sends for every intent in existing `milestones.py`; used `dry-run` for validation to avoid live posts.
