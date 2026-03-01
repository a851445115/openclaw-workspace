#!/usr/bin/env python3
import json
import os
import re
from typing import Any, Dict, List


DEFAULT_PROJECT_BOOTSTRAP_TASKS = (
    "梳理目标与验收标准（来自项目文档）",
    "拆解可执行里程碑并标注负责人建议",
    "启动首个最小可交付任务并回传证据",
)

DEFAULT_DECOMPOSITION_POLICY: Dict[str, Any] = {
    "maxTasks": 8,
    "minConfidence": 0.45,
    "requireHumanConfirm": False,
    "ownerRules": {
        "调研": "invest-analyst",
        "分析": "invest-analyst",
        "research": "invest-analyst",
        "发布": "broadcaster",
        "公告": "broadcaster",
        "summary": "broadcaster",
        "review": "debugger",
        "测试": "debugger",
        "验证": "debugger",
        "排查": "debugger",
        "修复": "debugger",
        "异常": "debugger",
        "bug": "debugger",
    },
}

TASK_SECTION_PATTERN = re.compile(r"里程碑|milestone|任务|todo|计划|plan|roadmap", flags=re.IGNORECASE)
MILESTONE_BULLET_PATTERN = re.compile(r"^(?:[-*]\s*)?(?:M|m)\d+\s*[：:]\s*(.+)$")
GENERAL_BULLET_PATTERN = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+)(.+)$")
ACTION_WORD_PATTERN = re.compile(
    r"(实现|编写|搭建|部署|测试|发布|修复|优化|调研|分析|定义|设计|验证|集成|上线|review|test|deploy|release|fix|implement|design|plan)",
    flags=re.IGNORECASE,
)


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clip(text: str, limit: int = 120) -> str:
    s = " ".join(_to_text(text).split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "..."


def _normalize_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(default)
    return n


def _normalize_float(value: Any, default: float) -> float:
    try:
        n = float(value)
    except Exception:
        n = float(default)
    return n


def _default_policy() -> Dict[str, Any]:
    return {
        "maxTasks": int(DEFAULT_DECOMPOSITION_POLICY["maxTasks"]),
        "minConfidence": float(DEFAULT_DECOMPOSITION_POLICY["minConfidence"]),
        "requireHumanConfirm": bool(DEFAULT_DECOMPOSITION_POLICY["requireHumanConfirm"]),
        "ownerRules": dict(DEFAULT_DECOMPOSITION_POLICY["ownerRules"]),
    }


def _normalize_policy(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = _default_policy()
    if not isinstance(raw, dict):
        return out

    max_tasks = _normalize_int(raw.get("maxTasks"), out["maxTasks"])
    out["maxTasks"] = max(1, min(32, max_tasks))

    min_conf = _normalize_float(raw.get("minConfidence"), out["minConfidence"])
    out["minConfidence"] = max(0.0, min(1.0, min_conf))
    out["requireHumanConfirm"] = bool(raw.get("requireHumanConfirm", out["requireHumanConfirm"]))

    owner_rules = raw.get("ownerRules")
    if isinstance(owner_rules, dict):
        cleaned: Dict[str, str] = {}
        for k, v in owner_rules.items():
            key = _to_text(k).lower()
            val = _to_text(v).lower()
            if not key or not val:
                continue
            cleaned[key] = val
        if cleaned:
            out["ownerRules"] = cleaned

    return out


def load_decomposition_policy(root: str) -> Dict[str, Any]:
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    roots = []
    for candidate in [root, script_root]:
        path = os.path.abspath(os.path.expanduser(_to_text(candidate)))
        if path and path not in roots:
            roots.append(path)

    rel_candidates = (
        os.path.join("config", "decomposition-policy.json"),
        os.path.join("state", "decomposition-policy.json"),
    )

    for base in roots:
        for rel in rel_candidates:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return _normalize_policy(json.load(f))
            except Exception:
                continue

    return _default_policy()


def normalize_task_title(title: str) -> str:
    text = _to_text(title)
    text = re.sub(r"^\[(.*?)\]\s*", "", text)
    text = re.sub(r"^(?:m|milestone|阶段)\s*\d+\s*[：:\-]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s`~!@#$%^&*()+=\[\]{}|\\:;\"'<>,.?/，。！？；：、（）【】《》“”‘’·]+", "", text.lower())
    return text


def _canonical_owner(agent: str) -> str:
    v = _to_text(agent).lower()
    if v in {"coder", "debugger", "invest-analyst", "broadcaster"}:
        return v
    if v in {"invest", "analyst", "invest_analyst", "researcher"}:
        return "invest-analyst"
    if v in {"broadcast", "reporter", "announcer"}:
        return "broadcaster"
    if v in {"dev", "developer", "engineer"}:
        return "coder"
    return "coder"


def _infer_owner_hint(title: str, owner_rules: Dict[str, str]) -> str:
    s = _to_text(title).lower()
    for keyword, agent in owner_rules.items():
        if keyword and keyword in s:
            return _canonical_owner(agent)
    if re.search(r"debug|bug|异常|故障|排查|修复|测试|验证|回归", s, flags=re.IGNORECASE):
        return "debugger"
    if re.search(r"调研|分析|research|invest|评估", s, flags=re.IGNORECASE):
        return "invest-analyst"
    if re.search(r"发布|公告|summary|播报|同步", s, flags=re.IGNORECASE):
        return "broadcaster"
    return "coder"


def _score_candidate(title: str, in_task_section: bool, milestone_line: bool) -> float:
    score = 0.30
    if in_task_section:
        score += 0.25
    if milestone_line:
        score += 0.25
    if ACTION_WORD_PATTERN.search(title):
        score += 0.15
    if 8 <= len(title) <= 120:
        score += 0.10
    return max(0.0, min(0.99, score))


def _extract_candidates(doc_text: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    in_task_section = False
    for raw in (doc_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            in_task_section = bool(TASK_SECTION_PATTERN.search(line))
            continue

        matched = MILESTONE_BULLET_PATTERN.match(line)
        milestone_line = bool(matched)
        if matched:
            title = _clip(matched.group(1))
            if title:
                candidates.append(
                    {
                        "title": title,
                        "confidence": _score_candidate(title, in_task_section, milestone_line),
                    }
                )
            continue

        general = GENERAL_BULLET_PATTERN.match(line)
        if general:
            title = _clip(general.group(1))
            if title:
                candidates.append(
                    {
                        "title": title,
                        "confidence": _score_candidate(title, in_task_section, False),
                    }
                )

    return candidates


def _fallback_tasks(policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    max_tasks = int(policy.get("maxTasks") or len(DEFAULT_PROJECT_BOOTSTRAP_TASKS))
    owner_rules = policy.get("ownerRules") if isinstance(policy.get("ownerRules"), dict) else {}
    out: List[Dict[str, Any]] = []
    prev_title = ""
    for title in list(DEFAULT_PROJECT_BOOTSTRAP_TASKS)[:max_tasks]:
        depends = [prev_title] if prev_title else []
        out.append(
            {
                "title": title,
                "ownerHint": _infer_owner_hint(title, owner_rules),
                "dependsOn": depends,
                "confidence": 0.55,
            }
        )
        prev_title = title
    return out


def decompose_project(project_path: str, project_name: str, doc_text: str) -> List[Dict[str, Any]]:
    del project_name  # reserved for future strategy branching
    policy = load_decomposition_policy(project_path)
    max_tasks = int(policy.get("maxTasks") or 8)
    min_confidence = float(policy.get("minConfidence") or 0.0)
    owner_rules = policy.get("ownerRules") if isinstance(policy.get("ownerRules"), dict) else {}

    selected: List[Dict[str, Any]] = []
    seen = set()
    prev_title = ""
    for item in _extract_candidates(doc_text):
        title = _clip(item.get("title") or "")
        if not title:
            continue
        key = normalize_task_title(title)
        if not key or key in seen:
            continue
        confidence = float(item.get("confidence") or 0.0)
        if confidence < min_confidence:
            continue
        seen.add(key)
        selected.append(
            {
                "title": title,
                "ownerHint": _infer_owner_hint(title, owner_rules),
                "dependsOn": [prev_title] if prev_title else [],
                "confidence": round(confidence, 2),
            }
        )
        prev_title = title
        if len(selected) >= max_tasks:
            break

    if selected:
        return selected
    return _fallback_tasks(policy)
