# multi-agent-orchestrator

Purpose: OpenClaw native multi-agent dispatch using `sessions_spawn`. Main agent (GLM-5) plans and coordinates; sub-agents (minimax-m2.5) execute tasks in parallel.

---

## When to Activate

Activate when a task can be broken into 2+ independent subtasks, such as:
- Research + writing
- Multi-source data gathering
- Parallel analysis of different topics
- Any task where subtasks don't depend on each other

---

## Core Tools

### sessions_spawn
Spawn a sub-agent to handle a specific task.

```json
{
  "tool": "sessions_spawn",
  "task": "Detailed description of what this sub-agent should do and return",
  "runTimeoutSeconds": 120
}
```

Returns: `{ "sessionId": "...", "result": "..." }`

### sessions_history
Check a sub-agent's output after it completes.

```json
{
  "tool": "sessions_history",
  "sessionId": "..."
}
```

### agents_list
List available agents (for dynamic agent selection).

```json
{
  "tool": "agents_list"
}
```

---

## Workflow Pattern

### 1. Decompose
Break the user's request into 2-5 independent subtasks. Each subtask should:
- Be self-contained (no dependency on other subtasks)
- Have a clear, specific output format
- Be completable within the timeout

### 2. Spawn in Controlled Parallel
Spawn sub-agents in parallel, but respect concurrency cap (`maxConcurrent=2`):
- First launch up to 2 sub-agents
- When one finishes, immediately launch the next pending subtask
- Keep the pipeline full without overloading provider connections

Example for a research task:
```
Spawn sub-agent 1: "Search for X using Tavily API and return top 3 findings as bullet points"
Spawn sub-agent 2: "Search for Y using SerpAPI and return top 3 findings as bullet points"
Spawn sub-agent 3: "Summarize the current state of Z based on your knowledge"
```

### 3. Collect & Synthesize
Wait for all sub-agents to complete, then synthesize their outputs into a coherent final response for the user.

### 4. Notify
Send the synthesized result to the user via the appropriate channel.

---

## Rules

- **Max concurrent sub-agents: 2** — don't spawn more than 2 at once
- **Timeout: 120s per sub-agent** — if a sub-agent times out, proceed with available results and note the gap
- **Connection retry with exponential backoff**:
  - If result contains `Connection error.` or `No reply from agent.`, retry that subtask
  - Retry delays: 2s, 4s, 8s (max 3 attempts total)
  - If still failing after 3 attempts, mark subtask failed and continue synthesis with remaining results
- **Task clarity** — each sub-agent task must be fully self-contained; include all context it needs in the task description
- **No nested spawning** — sub-agents should not spawn further sub-agents
- **Synthesize, don't concatenate** — the main agent must produce a unified response, not just paste sub-agent outputs together

---

## Example: 早间简报

User asks: "帮我生成今日简报"

Main agent decomposes into:
1. Sub-agent 1: "Use Tavily API to search for top 3 AI/tech news from the past 24 hours. Return title, source, and 1-sentence summary for each."
2. Sub-agent 2: "Use SerpAPI to search Google News for top 3 China tech/startup news today. Return title, source, and 1-sentence summary for each."
3. Sub-agent 3: "Create a Feishu document titled '🌅 [DATE] 早间战略简报' and return the document URL."

Main agent waits for all three, then:
- Writes the news content into the Feishu doc (sub-agent 3's URL)
- Sends a summary message to the Feishu group with the doc link

---

## Feishu Notifications

Send progress updates for long-running multi-agent tasks:
- On start: `🚀 任务已拆分为 {n} 个子任务，并行执行中...`
- On completion: synthesized result
- On partial failure: `⚠️ {n}/{total} 个子任务完成，以下结果基于可用数据：`
