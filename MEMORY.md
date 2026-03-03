# MEMORY.md - LanceDB Pro Plugin 铁律

> 以下内容摘自原始MEMORY.md，仅保留与 LanceDB / memory-lancedb-pro 插件直接相关的条目。

---

## Rule 6 — 双层记忆存储（铁律）

Every pitfall/lesson learned → IMMEDIATELY store **TWO** memories to LanceDB before moving on:

- **Technical layer**: `Pitfall: [symptom]. Cause: [root cause]. Fix: [solution]. Prevention: [how to avoid]` (category: fact, importance ≥ 0.8)

- **Principle layer**: `Decision principle ([tag]): [behavioral rule]. Trigger: [when it applies]. Action: [what to do]` (category: decision, importance ≥ 0.85)

- After each store, **immediately `memory_recall`** with anchor keywords to verify retrieval. If not found, rewrite and re-store.

- Missing either layer = incomplete. Do NOT proceed to next topic until both are stored and verified.

- Also update relevant SKILL.md files to prevent recurrence.

---

## Rule 7 — LanceDB 卫生

Entries must be short and atomic (< 500 chars). Never store raw conversation summaries, large blobs, or duplicates. Prefer structured format with keywords for retrieval.

---

## Rule 8 — Recall before retry

On ANY tool failure, repeated error, or unexpected behavior, ALWAYS `memory_recall` with relevant keywords (error message, tool name, symptom) BEFORE retrying. LanceDB likely already has the fix. Blind retries waste time and repeat known mistakes.

---

## Rule 10 — 编辑前确认目标代码库

When working on memory plugins, confirm you are editing the intended package (e.g., `memory-lancedb-pro` vs built-in `memory-lancedb`) before making changes; use `memory_recall` + filesystem search to avoid patching the wrong repo.

---

## Rule 20 — 插件代码变更必须清 jiti 缓存（MANDATORY）

After modifying ANY `.ts` file under `plugins/`, MUST run `rm -rf /tmp/jiti/` BEFORE `openclaw gateway restart`. jiti caches compiled TS; restart alone loads STALE code. This has caused silent bugs multiple times. Config-only changes do NOT need cache clearing.

---

## Rule 21 — 小红书和抖音操作必须使用本地 MCP Skills（MANDATORY）

**Decision principle (social-media-ops)**: When user requests operations on 小红书 (XiaoHongShu/XHS/RedNote) or 抖音 (Douyin/TikTok China) — including fetching info, posting content, or any interaction — MUST use the local MCP skills.

**Skills location**:
- `~/.openclaw/skills/xhs-douyin-mcp-autostart` (supports both platforms)
- `~/.openclaw/skills/douyin-mcp-autostart` (Douyin only)

**Service ports**:
- 小红书: http://127.0.0.1:18060/mcp
- 抖音: http://127.0.0.1:18070/mcp

**Trigger**: User mentions "小红书", "抖音", "XHS", "RedNote", "Douyin", or requests to get/post content on these platforms.

**Action**:
1. Read the corresponding SKILL.md file first
2. Use scripts to ensure service is running (ensure_xhs.sh / ensure_douyin.sh)
3. Call tools via mcporter (mcporter_call.sh or direct mcporter call)

**Prerequisites**:
- 小红书 requires prior login with cookies at: `/Users/chengren17/opt/xiaohongshu-mcp-bin/cookies.json`
- mcporter config: `/Users/chengren17/config/mcporter.json`

**Never**: Attempt to use web scraping, browser automation, or API calls directly without these skills.

