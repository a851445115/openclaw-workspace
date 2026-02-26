---
name: agent-team-starter
version: "1.0.0"
description: Helps users correctly create and start agent teams for parallel task execution. Use when you need multiple agents working together on related tasks, or when tasks can be parallelized across specialized agents.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - TeamCreate
  - Task
  - TaskCreate
  - TaskGet
  - TaskUpdate
  - TaskList
  - SendMessage
  - ToolSearch
---

# Agent Team Starter

Guide for creating and managing agent teams in Claude Code.

## ⚠️ CRITICAL: Agent Team vs Subagent

**This is the #1 source of confusion. Read carefully!**

| Criteria | Agent Team | Subagent (Task without team_name) |
|----------|------------|-----------------------------------|
| **Creation** | `TeamCreate` + `Task` with `team_name` & `name` | `Task` without `team_name` |
| **Duration** | Long-running, persistent sessions | Short, one-off tasks |
| **Communication** | Peers can message each other via `SendMessage` | Isolated, reports back only |
| **Context** | Shared team context via tasks | Independent context |
| **Coordination** | Team lead coordinates work | Main agent coordinates |
| **Use Case** | Complex multi-part projects | Quick parallel searches |

### When to Use Agent Team

- Multiple related tasks that benefit from coordination
- Tasks requiring specialized expertise (code review, testing, docs)
- Long-running projects with multiple phases
- When agents need to communicate with each other

### When to Use Subagent

- Simple parallel searches or lookups
- One-off tasks that don't need coordination
- Quick codebase exploration
- Independent research tasks

## Creating an Agent Team

### Step 1: Create the Team

Use `TeamCreate` to create a new team:

```
TeamCreate(
  team_name: "my-project-team",
  description: "Team description"
)
```

This creates:
- A team config file at `~/.claude/teams/{team-name}/config.json`
- A task list directory at `~/.claude/tasks/{team-name}/`

### Step 2: Add Teammates

Use `Task` tool with BOTH `team_name` AND `name` parameters:

```
Task(
  description: "Short task description",
  name: "teammate-name",           // REQUIRED: teammate identifier
  prompt: "Detailed task instructions...",
  subagent_type: "Explore",        // or "general-purpose", "Plan", etc.
  team_name: "my-project-team"     // REQUIRED: links to team
)
```

**⚠️ Common Mistake:**
```
# WRONG - This creates a subagent, NOT a teammate!
Task(
  description: "Task",
  prompt: "...",
  subagent_type: "Explore"
  # Missing team_name and name!
)

# CORRECT - This creates a teammate in the team
Task(
  description: "Task",
  name: "my-teammate",
  prompt: "...",
  subagent_type: "Explore",
  team_name: "my-project-team"
)
```

### Step 3: Define Teammate Roles

Common teammate archetypes:

| Role | subagent_type | Responsibility |
|------|---------------|----------------|
| `explorer` | Explore | Codebase exploration, research |
| `planner` | Plan | Architecture design, planning |
| `implementer` | general-purpose | Code implementation |
| `reviewer` | general-purpose | Code review, quality |

## Communicating with Teammates

### CRITICAL: Use SendMessage, NOT Text!

Your text responses are **NOT visible to teammates**. You MUST use `SendMessage`:

```
SendMessage(
  type: "message",
  recipient: "teammate-name",      // Use the name from Task()
  content: "Your message here",
  summary: "Brief summary"
)
```

### Message Types

| Type | Use Case |
|------|----------|
| `message` | Direct message to one teammate |
| `broadcast` | Message ALL teammates (use sparingly) |
| `shutdown_request` | Request teammate to shut down |

### Example Communication Flow

```
# Assign task to teammate
SendMessage(
  type: "message",
  recipient: "explorer",
  content: "Please explore the authentication module and report findings.",
  summary: "Assign exploration task"
)

# Teammate will respond automatically - wait for notification

# When all done, summarize to user
```

## Complete Workflow Example

```markdown
# 1. Create Team
TeamCreate(
  team_name: "feature-implementation",
  description: "Implement new feature with tests"
)

# 2. Add Teammates
Task(
  description: "Explore codebase",
  name: "code-explorer",
  prompt: "Explore the codebase structure...",
  subagent_type: "Explore",
  team_name: "feature-implementation"
)

Task(
  description: "Design architecture",
  name: "architect",
  prompt: "Design the architecture for...",
  subagent_type: "Plan",
  team_name: "feature-implementation"
)

# 3. Communicate
SendMessage(
  type: "message",
  recipient: "code-explorer",
  content: "Start exploring the auth module.",
  summary: "Start exploration"
)

# 4. Wait for responses (they come automatically)

# 5. Collect results and summarize to user

# 6. Shutdown when done
SendMessage(
  type: "shutdown_request",
  recipient: "code-explorer",
  content: "Task complete, thank you!"
)
```

## Task Management (Optional)

Agent teams can use the shared task list for coordination:

### List Tasks
```
TaskList()
```

### Check Task Status
```
TaskGet(taskId: "task-id")
```

### Update Task Status
```
TaskUpdate(
  taskId: "task-id",
  status: "completed"  // or "in_progress", "pending"
)
```

## Closing the Team

### Step 1: Shutdown Teammates

```
SendMessage(
  type: "shutdown_request",
  recipient: "teammate-name",
  content: "Task complete, wrapping up."
)
```

### Step 2: Summarize Results

Collect findings from all teammates and provide a unified summary to the user.

## Anti-Patterns to Avoid

| Don't | Do Instead |
|-------|------------|
| Use Task without team_name + name | Always include BOTH parameters for teammates |
| Message teammates via text output | Use SendMessage tool explicitly |
| Create team for simple parallel searches | Use subagent (Task without team_name) |
| Skip TeamCreate step | Always create team first with TeamCreate |
| Forget to shutdown teammates | Send shutdown_request when done |

## Quick Reference Card

```
# 1. Create Team
TeamCreate(team_name: "my-team", description: "...")

# 2. Add Teammates (BOTH name AND team_name required!)
Task(
  name: "teammate-name",        // REQUIRED
  team_name: "my-team",         // REQUIRED
  description: "...",
  prompt: "...",
  subagent_type: "Explore"
)

# 3. Communicate (ONLY via SendMessage!)
SendMessage(
  type: "message",
  recipient: "teammate-name",
  content: "...",
  summary: "..."
)

# 4. Shutdown
SendMessage(
  type: "shutdown_request",
  recipient: "teammate-name",
  content: "Done!"
)
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Created subagent instead of teammate | Add `team_name` AND `name` to Task call |
| Teammate not responding | Check recipient name matches Task's `name` |
| Messages not delivered | Use SendMessage, not text output |
| Team not created | Run TeamCreate before adding teammates |
