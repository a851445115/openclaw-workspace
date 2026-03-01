#!/usr/bin/env python3
import re
from typing import Any, Dict, List, Optional, Set


URL_RE = re.compile(r"\bhttps?://[^\s<>\"]+", flags=re.IGNORECASE)
PATH_RE = re.compile(
    r"(?:^|[\s'\"`(])((?:[A-Za-z]:[\\/]|~?/|\.?/|\.{2}/)[^\s'\"`<>]+|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})"
)
TEST_CMD_RE = re.compile(
    r"\b(pytest|python\s+-m\s+unittest|unittest|go\s+test|cargo\s+test|npm\s+test|pnpm\s+test|yarn\s+test)\b",
    flags=re.IGNORECASE,
)
TEST_OUTPUT_RE = re.compile(
    r"(\b\d+\s+passed\b|ran\s+\d+\s+tests?.*\bok\b|exit\s*=\s*0)",
    flags=re.IGNORECASE,
)
UNITTEST_OK_RE = re.compile(r"^ran\s+\d+\s+tests?.*\n?ok$", flags=re.IGNORECASE)
TEST_PASSED_WORD_RE = re.compile(r"\bpassed\b", flags=re.IGNORECASE)
SOFT_HINTS = (
    "evidence",
    "proof",
    "log",
    "output",
    "result",
    "验证",
    "证据",
    "截图",
    "报告",
    "summary",
)
FILE_EXTENSIONS = {
    "py",
    "md",
    "json",
    "yaml",
    "yml",
    "txt",
    "log",
    "csv",
    "xml",
    "html",
    "css",
    "js",
    "ts",
    "tsx",
    "jsx",
    "go",
    "rs",
    "java",
    "sh",
    "sql",
    "ini",
    "toml",
    "lock",
}


def _clip(text: str, limit: int = 220) -> str:
    one_line = " ".join((text or "").strip().split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "..."


def _append_unique(target: List[str], seen: Set[str], value: str, limit: int = 220) -> None:
    clipped = _clip(value, limit=limit)
    if not clipped:
        return
    if clipped in seen:
        return
    seen.add(clipped)
    target.append(clipped)


def _looks_file_like(token: str) -> bool:
    s = token.strip().strip("()[]{}<>,;:'\"")
    if not s:
        return False
    if s.lower().startswith("http://") or s.lower().startswith("https://"):
        return False
    if "/" in s or "\\" in s:
        normalized = s.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
        if not parts:
            return False
        if normalized.startswith(("/", "./", "../", "~/")) or re.match(r"^[A-Za-z]:[\\/]", s):
            return bool(re.search(r"[A-Za-z0-9]", normalized))
        if all(len(part) <= 2 for part in parts):
            return False
        if not any(re.search(r"[A-Za-z]", part) for part in parts):
            return False
        return True
    if "." not in s:
        return False
    stem, _, ext = s.rpartition(".")
    if not stem or not ext:
        return False
    return ext.lower() in FILE_EXTENSIONS


def _collect_chunks(structured: Optional[Dict[str, Any]], text: str) -> List[str]:
    chunks: List[str] = []
    seen: Set[str] = set()

    if text.strip():
        _append_unique(chunks, seen, text, limit=500)

    base = structured or {}
    for key in ("summary", "message", "result", "output", "text"):
        if isinstance(base.get(key), str):
            _append_unique(chunks, seen, base.get(key), limit=500)

    evidence = base.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, str):
                _append_unique(chunks, seen, item, limit=400)

    changes = base.get("changes")
    if isinstance(changes, list):
        for item in changes:
            if isinstance(item, dict):
                path = str(item.get("path") or item.get("file") or "").strip()
                summary = str(item.get("summary") or item.get("change") or "").strip()
                if path or summary:
                    _append_unique(chunks, seen, f"{path}: {summary}".strip(": "), limit=400)
            elif isinstance(item, str):
                _append_unique(chunks, seen, item, limit=400)

    return chunks


def _extract_hard_evidence(normalized_text: str) -> List[str]:
    hard: List[str] = []
    seen: Set[str] = set()

    for match in URL_RE.findall(normalized_text):
        _append_unique(hard, seen, match, limit=260)

    for match in PATH_RE.finditer(normalized_text):
        token = str(match.group(1) or "").strip().strip("()[]{}<>,;:'\"")
        if _looks_file_like(token):
            _append_unique(hard, seen, token, limit=240)

    for raw_line in normalized_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        has_cmd = bool(TEST_CMD_RE.search(lower))
        has_output = bool(TEST_OUTPUT_RE.search(lower))
        has_passed_word = bool(TEST_PASSED_WORD_RE.search(lower))
        if has_output or UNITTEST_OK_RE.search(lower) or (has_cmd and has_passed_word):
            _append_unique(hard, seen, f"test:{line}", limit=240)

    return hard


def _extract_soft_evidence(normalized_text: str, hard: List[str]) -> List[str]:
    hard_joined = "\n".join(hard).lower()
    soft: List[str] = []
    seen: Set[str] = set()

    for raw_line in normalized_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(hint in lower for hint in SOFT_HINTS):
            if line.lower() in hard_joined:
                continue
            _append_unique(soft, seen, line, limit=220)

    return soft


def normalize_evidence(structured: Optional[Dict[str, Any]] = None, text: str = "") -> Dict[str, Any]:
    chunks = _collect_chunks(structured, text)
    normalized_text = "\n".join(chunks).strip()
    hard = _extract_hard_evidence(normalized_text)
    soft = _extract_soft_evidence(normalized_text, hard)
    return {
        "hardEvidence": hard,
        "softEvidence": soft,
        "normalizedText": normalized_text,
    }
