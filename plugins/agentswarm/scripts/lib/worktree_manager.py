#!/usr/bin/env python3
import json
import logging
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional


LOGGER = logging.getLogger(__name__)
WORKTREE_POLICY_CONFIG_CANDIDATES = (
    os.path.join("config", "worktree-policy.json"),
    os.path.join("state", "worktree-policy.json"),
)
DEFAULT_WORKTREE_POLICY: Dict[str, Any] = {
    "enabled": False,
    "rootDir": "",
    "branchPrefix": "task",
    "cleanupOnDone": False,
    "bootstrapCommands": [],
}
TASK_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
BRANCH_PREFIX_RE = re.compile(r"[^A-Za-z0-9._/-]+")
CommandRunner = Callable[[List[str], str], Dict[str, Any]]


def _run_command(cmd: List[str], cwd: str) -> Dict[str, Any]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    return {
        "returncode": int(proc.returncode),
        "stdout": str(proc.stdout or ""),
        "stderr": str(proc.stderr or ""),
    }


def _safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return bool(default)


def _normalize_task_token(value: Any, fallback: str = "task") -> str:
    token = TASK_TOKEN_RE.sub("-", str(value or "").strip()).strip("-")
    return token or fallback


def _normalize_branch_prefix(value: Any, fallback: str = "task") -> str:
    token = BRANCH_PREFIX_RE.sub("-", str(value or "").strip())
    token = re.sub(r"-{2,}", "-", token).strip("/-")
    return token or fallback


def _normalize_bootstrap_commands(value: Any) -> List[str]:
    if isinstance(value, list):
        out = []
        for item in value:
            cmd = str(item or "").strip()
            if cmd:
                out.append(cmd)
        return out
    if isinstance(value, str):
        cmd = value.strip()
        return [cmd] if cmd else []
    return []


def _merge_policy(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key in ("enabled", "rootDir", "branchPrefix", "cleanupOnDone", "bootstrapCommands"):
        if key in override:
            merged[key] = override.get(key)
    return merged


def _load_json_dict(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _git_top_level(root: str, runner: CommandRunner) -> str:
    result = runner(["git", "-C", root, "rev-parse", "--show-toplevel"], cwd=root)
    if int(result.get("returncode", 1)) != 0:
        return ""
    lines = str(result.get("stdout") or "").strip().splitlines()
    return os.path.abspath(lines[0].strip()) if lines else ""


def _default_root_dir(root: str, runner: CommandRunner) -> str:
    git_top = _git_top_level(root, runner)
    if git_top:
        return os.path.abspath(os.path.join(git_top, "..", "task-worktrees"))
    return os.path.abspath(os.path.join(root, "..", "task-worktrees"))


def normalize_worktree_policy(root: str, raw: Dict[str, Any], runner: Optional[CommandRunner] = None) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    command_runner = runner or _run_command
    root_dir_raw = str(data.get("rootDir") or "").strip()
    root_dir = os.path.abspath(os.path.expanduser(root_dir_raw)) if root_dir_raw else _default_root_dir(root, command_runner)
    return {
        "enabled": _safe_bool(data.get("enabled"), bool(DEFAULT_WORKTREE_POLICY["enabled"])),
        "rootDir": root_dir,
        "branchPrefix": _normalize_branch_prefix(data.get("branchPrefix"), fallback=str(DEFAULT_WORKTREE_POLICY["branchPrefix"])),
        "cleanupOnDone": _safe_bool(data.get("cleanupOnDone"), bool(DEFAULT_WORKTREE_POLICY["cleanupOnDone"])),
        "bootstrapCommands": _normalize_bootstrap_commands(data.get("bootstrapCommands")),
    }


def load_worktree_policy(root: str, override: Optional[Dict[str, Any]] = None, runner: Optional[CommandRunner] = None) -> Dict[str, Any]:
    policy = dict(DEFAULT_WORKTREE_POLICY)
    script_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    search_roots = [script_root, root]
    for base in search_roots:
        for rel in WORKTREE_POLICY_CONFIG_CANDIDATES:
            path = os.path.join(base, rel)
            if not os.path.exists(path):
                continue
            loaded = _load_json_dict(path)
            if loaded:
                policy = _merge_policy(policy, loaded)
    if isinstance(override, dict):
        policy = _merge_policy(policy, override)
    return normalize_worktree_policy(root, policy, runner=runner)


def build_task_worktree_branch(task_id: str, branch_prefix: str) -> str:
    task_token = _normalize_task_token(task_id, fallback="task")
    prefix = _normalize_branch_prefix(branch_prefix, fallback="task")
    return f"{prefix}/{task_token}"


def build_task_worktree_path(root_dir: str, task_id: str) -> str:
    task_token = _normalize_task_token(task_id, fallback="task")
    return os.path.join(os.path.abspath(root_dir), f"task-{task_token}")


def _git_branch_exists(root: str, branch: str, runner: CommandRunner) -> bool:
    result = runner(["git", "-C", root, "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=root)
    return int(result.get("returncode", 1)) == 0


def ensure_task_worktree(
    root: str,
    task_id: str,
    base_ref: str = "HEAD",
    policy_override: Optional[Dict[str, Any]] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    command_runner = runner or _run_command
    policy = load_worktree_policy(root, override=policy_override, runner=command_runner)
    branch = build_task_worktree_branch(task_id, str(policy.get("branchPrefix") or "task"))
    path = build_task_worktree_path(str(policy.get("rootDir") or ""), task_id)
    out: Dict[str, Any] = {
        "ok": True,
        "created": False,
        "skipped": False,
        "reason": "",
        "path": os.path.abspath(path),
        "branch": branch,
        "policy": policy,
        "bootstrap": [],
    }

    if not bool(policy.get("enabled")):
        out["skipped"] = True
        out["reason"] = "disabled"
        return out

    if os.path.isdir(path):
        out["reason"] = "existing"
        return out

    os.makedirs(str(policy.get("rootDir") or ""), exist_ok=True)
    branch_exists = _git_branch_exists(root, branch, command_runner)
    if branch_exists:
        cmd = ["git", "-C", root, "worktree", "add", path, branch]
    else:
        cmd = ["git", "-C", root, "worktree", "add", path, "-b", branch, str(base_ref or "HEAD")]
    created = command_runner(cmd, cwd=root)
    if int(created.get("returncode", 1)) != 0:
        out["ok"] = False
        out["reason"] = "create_failed"
        out["error"] = str(created.get("stderr") or created.get("stdout") or "git worktree add failed").strip()
        out["command"] = cmd
        return out

    out["created"] = True
    out["reason"] = "created"
    bootstrap_commands = policy.get("bootstrapCommands") if isinstance(policy.get("bootstrapCommands"), list) else []
    for command in bootstrap_commands:
        proc = subprocess.run(str(command), cwd=path, shell=True, capture_output=True, text=True, check=False)
        item = {
            "command": str(command),
            "returncode": int(proc.returncode),
            "stdout": str(proc.stdout or "").strip(),
            "stderr": str(proc.stderr or "").strip(),
        }
        out["bootstrap"].append(item)
        if proc.returncode != 0:
            out["ok"] = False
            out["reason"] = "bootstrap_failed"
            out["error"] = item["stderr"] or item["stdout"] or f"bootstrap command failed: {command}"
            break
    return out


def cleanup_task_worktree(
    root: str,
    task_id: str,
    force: bool = False,
    policy_override: Optional[Dict[str, Any]] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    command_runner = runner or _run_command
    policy = load_worktree_policy(root, override=policy_override, runner=command_runner)
    path = build_task_worktree_path(str(policy.get("rootDir") or ""), task_id)
    branch = build_task_worktree_branch(task_id, str(policy.get("branchPrefix") or "task"))
    out: Dict[str, Any] = {
        "ok": True,
        "removed": False,
        "skipped": False,
        "reason": "",
        "path": os.path.abspath(path),
        "branch": branch,
        "policy": policy,
    }

    if not bool(policy.get("enabled")) and not force:
        out["skipped"] = True
        out["reason"] = "disabled"
        return out

    if not os.path.isdir(path):
        out["skipped"] = True
        out["reason"] = "not_found"
        return out

    cmd = ["git", "-C", root, "worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(path)
    removed = command_runner(cmd, cwd=root)
    if int(removed.get("returncode", 1)) != 0:
        out["ok"] = False
        out["reason"] = "remove_failed"
        out["error"] = str(removed.get("stderr") or removed.get("stdout") or "git worktree remove failed").strip()
        out["command"] = cmd
        return out

    out["removed"] = True
    out["reason"] = "removed"
    return out
