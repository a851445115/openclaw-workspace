"""Workflow-scoped paper context injector.

Loads paper-related context (raw text, method mappings, etc.) from a
workflow run directory and injects it into the dispatch prompt for
applicable stages.  The injector is driven by the ``contextInjectors``
block in the workflow config JSON — it is NOT global and only activates
for stages listed in ``applyToStages``.
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 8000


def _read_source(run_dir: str, source_pattern: str) -> str:
    """Read a single context source file, resolving {run_dir} placeholder."""
    path = source_pattern.replace("{run_dir}", run_dir)
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        logger.warning("paper_context_injector: failed to read %s: %s", path, exc)
        return ""


def load_paper_context(
    run_dir: str,
    injector_cfg: Dict[str, Any],
    stage_id: str,
) -> Optional[str]:
    """Return assembled paper context if the current stage is eligible.

    Returns ``None`` when the injector is disabled, the stage is not in
    ``applyToStages``, or no source files contain data.
    """
    if not injector_cfg.get("enabled", False):
        return None

    apply_to = set(injector_cfg.get("applyToStages", []))
    if stage_id not in apply_to:
        return None

    sources: List[str] = injector_cfg.get("sources", [])
    if not sources:
        return None

    parts: List[str] = []
    total = 0
    for src in sources:
        text = _read_source(run_dir, src)
        if not text:
            continue
        remaining = MAX_CONTEXT_CHARS - total
        if remaining <= 0:
            break
        chunk = text[:remaining]
        parts.append(chunk)
        total += len(chunk)

    if not parts:
        return None

    return "\n\n---\n\n".join(parts)


def inject_into_prompt(
    prompt: str,
    run_dir: str,
    context_injectors: Dict[str, Any],
    stage_id: str,
) -> str:
    """Append paper context sections to the prompt for eligible stages."""
    if not context_injectors:
        return prompt

    additions: List[str] = []
    for name, cfg in context_injectors.items():
        if not isinstance(cfg, dict):
            continue
        ctx = load_paper_context(run_dir, cfg, stage_id)
        if ctx:
            header = f"INJECTED_CONTEXT ({name}):"
            additions.append(f"{header}\n{ctx}")

    if not additions:
        return prompt

    return prompt + "\n\n" + "\n\n".join(additions)
