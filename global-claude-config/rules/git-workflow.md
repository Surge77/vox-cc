# Git Workflow Rules

## Conventional Commits
All commit messages must follow this format:
  <type>(<scope>): <short description>

Types: feat, fix, docs, style, refactor, test, chore, perf
Examples:
  feat(auth): add JWT refresh token support
  fix(api): return 404 when user not found
  test(users): add edge case for empty email

## Branch Naming
- Features: feature/<short-description>
- Bug fixes: fix/<short-description>
- Hotfixes: hotfix/<short-description>

## Rules
- Never commit directly to main or master
- Each commit should be a single logical change — do not batch unrelated changes
- Run linter and tests before committing
- PR required before merge; minimum 1 approval
- Squash commits when merging to keep history clean

## What NOT to Do
- Never force-push to shared branches
- Never commit node_modules, build artifacts, or .env files
- Never use "WIP", "fix", or "update" as the entire commit message
