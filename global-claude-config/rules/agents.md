# Agent Delegation Rules

## When to Use Subagents
- Task touches more than 3 files → invoke planner first
- New feature from scratch → planner → architect → tdd-guide → code-reviewer
- Any code change → code-reviewer before committing
- Auth, payments, file uploads, external APIs → security-reviewer mandatory
- Build or test failure → build-error-resolver immediately
- After implementation → doc-updater to sync documentation

## Explore → Plan → Execute Pipeline
For any task involving refactoring or more than 3 files:
1. Let Claude explore freely — reading is cheap
2. Invoke planner to produce a scoped plan
3. Present the plan and get explicit approval before any file is modified
4. Execute with code-reviewer as the final gate

## Rules
- Never run multiple agents that write to the same file simultaneously
- An agent's output must be reviewed before the next agent acts on it
- If an agent returns FAIL or BLOCKED, stop and surface it to the user — do not retry silently
- Agents do not override rules defined in security.md or testing.md
