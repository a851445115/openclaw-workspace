#!/usr/bin/env python3
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Tuple


TODO_PATTERN = re.compile(r"\b(?P<tag>TODO|FIXME)\b[:\s-]*(?P<text>.*)", flags=re.IGNORECASE)
PYTEST_FAILED_WITH_DETAIL_PATTERN = re.compile(
    r"^\s*FAILED\s+(?P<nodeid>\S+)\s*-\s*(?P<detail>.+?)\s*$",
    flags=re.IGNORECASE,
)
PYTEST_FAILED_NODEID_PATTERN = re.compile(r"^\s*FAILED\s+(?P<nodeid>\S+)\s*$", flags=re.IGNORECASE)
PROGRESS_SIGNAL_PATTERNS = (
    re.compile(r"(催|尽快|加急|urgent|deadline|今天.*进度|什么时候|follow\s*up)", flags=re.IGNORECASE),
)
REQUIREMENT_CHANGE_PATTERNS = (
    re.compile(r"(需求.*变更|改成|改为|新增|调整|删掉|change request|scope change)", flags=re.IGNORECASE),
)


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _result(
    *,
    ok: bool = True,
    findings: Optional[List[Dict[str, Any]]] = None,
    degraded: bool = False,
    reason: str = "",
) -> Dict[str, Any]:
    return {
        "ok": bool(ok),
        "findings": list(findings or []),
        "degraded": bool(degraded),
        "reason": _as_text(reason),
    }


def _dedupe_findings(findings: List[Dict[str, Any]], key_fields: List[str]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen: set = set()
    for item in findings:
        token = tuple(_as_text(item.get(field)) for field in key_fields)
        if token in seen:
            continue
        seen.add(token)
        deduped.append(item)
    return deduped


def _looks_like_path(text: str) -> bool:
    if not text or "\n" in text or "\r" in text:
        return False
    token = text.strip()
    if os.path.sep in token:
        return True
    if token.endswith((".log", ".txt", ".out")):
        return True
    return False


def _iter_text_files(paths: Iterable[str]) -> Tuple[List[str], List[str]]:
    files: List[str] = []
    degraded_reasons: List[str] = []
    seen: set = set()
    for raw in paths:
        candidate = _as_text(raw)
        if not candidate:
            continue
        path = os.path.realpath(candidate)
        if os.path.isfile(path):
            if path not in seen:
                seen.add(path)
                files.append(path)
            continue
        if os.path.isdir(path):
            for dir_path, dir_names, file_names in os.walk(path):
                dir_names[:] = [d for d in dir_names if d not in {".git", ".hg", ".svn", "__pycache__"}]
                for file_name in file_names:
                    file_path = os.path.realpath(os.path.join(dir_path, file_name))
                    if file_path in seen:
                        continue
                    seen.add(file_path)
                    files.append(file_path)
            continue
        degraded_reasons.append(f"path_not_found:{candidate}")
    return files, degraded_reasons


def _extract_message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return _as_text(message)
    for key in ("text", "content", "message", "body", "title"):
        text = _as_text(message.get(key))
        if text:
            return text
    return _as_text(message)


class ProactiveScanner:
    def __init__(self, policy: Optional[Dict[str, Any]] = None):
        self.policy = dict(policy or {})

    def scan_todo_comments(self, paths: Iterable[str]) -> Dict[str, Any]:
        files, path_errors = _iter_text_files(paths if isinstance(paths, (list, tuple, set)) else [str(paths or "")])
        findings: List[Dict[str, Any]] = []
        degraded_reasons: List[str] = list(path_errors)
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for idx, line in enumerate(f, start=1):
                        match = TODO_PATTERN.search(line)
                        if not match:
                            continue
                        findings.append(
                            {
                                "source": "todo_comment",
                                "tag": str(match.group("tag") or "").upper(),
                                "text": _as_text(match.group("text")),
                                "path": file_path,
                                "line": idx,
                            }
                        )
            except UnicodeDecodeError:
                continue
            except Exception as err:
                degraded_reasons.append(f"read_failed:{file_path}:{err.__class__.__name__}")
        deduped = _dedupe_findings(findings, ["path", "line", "tag", "text"])
        reason = "; ".join(degraded_reasons)
        return _result(ok=True, findings=deduped, degraded=bool(degraded_reasons), reason=reason)

    def scan_pytest_failures(self, log_text_or_path: Any) -> Dict[str, Any]:
        text = ""
        degraded = False
        reason = ""
        try:
            if isinstance(log_text_or_path, (str, os.PathLike)):
                payload = str(log_text_or_path)
                if os.path.isfile(payload):
                    with open(payload, "r", encoding="utf-8") as f:
                        text = f.read()
                else:
                    if _looks_like_path(payload) and not os.path.exists(payload):
                        degraded = True
                        reason = f"log_path_not_found:{payload}"
                    text = payload
            else:
                text = _as_text(log_text_or_path)
        except Exception as err:
            degraded = True
            reason = f"log_read_failed:{err.__class__.__name__}"
            text = ""

        findings: List[Dict[str, Any]] = []
        for line in text.splitlines():
            with_detail = PYTEST_FAILED_WITH_DETAIL_PATTERN.match(line)
            if with_detail:
                findings.append(
                    {
                        "source": "pytest_failure",
                        "nodeid": _as_text(with_detail.group("nodeid")),
                        "detail": _as_text(with_detail.group("detail")),
                    }
                )
                continue
            plain = PYTEST_FAILED_NODEID_PATTERN.match(line)
            if plain:
                findings.append(
                    {
                        "source": "pytest_failure",
                        "nodeid": _as_text(plain.group("nodeid")),
                        "detail": "",
                    }
                )
        deduped = _dedupe_findings(findings, ["nodeid", "detail"])
        return _result(ok=True, findings=deduped, degraded=degraded, reason=reason)

    def scan_feishu_messages(self, messages: Any) -> Dict[str, Any]:
        if messages is None:
            return _result(ok=True, findings=[], degraded=False, reason="")
        if not isinstance(messages, list):
            return _result(ok=True, findings=[], degraded=True, reason="messages_not_list")

        findings: List[Dict[str, Any]] = []
        for idx, message in enumerate(messages):
            text = _extract_message_text(message)
            if not text:
                continue
            if any(pattern.search(text) for pattern in PROGRESS_SIGNAL_PATTERNS):
                findings.append(
                    {
                        "source": "feishu_message",
                        "signal": "progress_push",
                        "text": text,
                        "messageIndex": idx,
                    }
                )
            if any(pattern.search(text) for pattern in REQUIREMENT_CHANGE_PATTERNS):
                findings.append(
                    {
                        "source": "feishu_message",
                        "signal": "requirement_change",
                        "text": text,
                        "messageIndex": idx,
                    }
                )
        deduped = _dedupe_findings(findings, ["messageIndex", "signal", "text"])
        return _result(ok=True, findings=deduped, degraded=False, reason="")

    def scan_arxiv_rss(self, feed_url: str = "https://export.arxiv.org/rss/cs.AI", timeout_sec: float = 2.0) -> Dict[str, Any]:
        findings: List[Dict[str, Any]] = []
        try:
            with urllib.request.urlopen(feed_url, timeout=max(0.1, float(timeout_sec))) as response:
                payload = response.read()
            root = ET.fromstring(payload)
            for item in root.findall(".//item"):
                title = _as_text(item.findtext("title"))
                link = _as_text(item.findtext("link"))
                if not title and not link:
                    continue
                findings.append({"source": "arxiv_rss", "title": title, "link": link})
            return _result(ok=True, findings=findings, degraded=False, reason="")
        except (urllib.error.URLError, TimeoutError) as err:
            return _result(
                ok=True,
                findings=[],
                degraded=True,
                reason=f"arxiv_rss_unavailable:{err.__class__.__name__}",
            )
        except Exception as err:
            return _result(
                ok=True,
                findings=[],
                degraded=True,
                reason=f"arxiv_rss_degraded:{err.__class__.__name__}",
            )
