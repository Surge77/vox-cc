# Performance & Model Selection

## Model Selection
- Haiku: simple lookups, formatting, single-file edits, quick Q&A
- Sonnet: standard coding tasks, multi-file changes, code review (default)
- Opus: system design, architecture decisions, complex debugging, security review

## Code Performance Rules
- Do not fetch inside a loop — batch queries or use joins
- Avoid N+1 queries; check ORM-generated SQL for new DB interactions
- Cache results that are expensive to compute and rarely change
- Set timeouts on all external HTTP requests — default 5s, max 30s
- Paginate all list endpoints — never return unbounded result sets

## Build Performance
- Do not add dependencies for tasks achievable with the standard library
- Tree-shake imports: import { x } from 'lib', not import lib from 'lib'
- Flag any new dependency that adds more than 50KB to the bundle

## When to Raise a Performance Concern
- Any DB query without an index on the filter column
- Any synchronous operation that could block the event loop
- Any response payload over 1MB
