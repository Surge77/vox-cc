# Coding Style Rules

## Immutability
- Prefer immutable data: use const over let; never use var
- Do not mutate function arguments — return new values instead
- Avoid side effects in pure functions

## File Size Limits
- Hard limit: 300 lines per file
- If a file exceeds 300 lines, split it into focused modules before continuing
- One responsibility per file — if you need "and" to describe it, split it

## General
- Prefer explicit over clever — readable code beats terse code
- Use named exports over default exports (easier to grep and refactor)
- No commented-out code in commits — delete it or open a TODO with a ticket reference
- Avoid deep nesting: max 3 levels; extract early returns or helper functions instead

## Naming
- Variables and functions: camelCase
- Classes and types: PascalCase
- Constants: UPPER_SNAKE_CASE
- Files: kebab-case
- Boolean variables: prefix with is, has, can, should (e.g., isLoading, hasError)
