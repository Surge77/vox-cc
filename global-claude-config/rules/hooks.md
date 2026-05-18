# Hooks Rules

## Purpose
Hooks run automatically before or after Claude Code tool calls.
Use them as gates and formatters, not as logic layers.

## Defined Hooks (configure in .claude/settings.json per project)
- PreToolUse: runs before any file write — use to block writes to protected paths
- PostToolUse: runs after file write — use to auto-format or lint the changed file

## Rules
- Hooks must be fast (< 2s) — they block the tool call until they return
- A hook that exits non-zero cancels the tool call — use this deliberately
- Never put business logic in hooks — hooks are for mechanical checks only
- Document every hook in this file: what it does, when it runs, what it blocks

## Hook Template (PostToolUse formatter)
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{ "type": "command", "command": "prettier --write $CLAUDE_TOOL_OUTPUT_PATH" }]
      }
    ]
  }
}

## Protected Paths (example — override per project)
- .env, .env.*, *.pem, *.key → block all writes via PreToolUse hook
