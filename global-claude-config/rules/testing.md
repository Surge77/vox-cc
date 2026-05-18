# Testing Rules

## TDD Workflow
- Write the failing test first, then write the minimum code to pass it
- Never write implementation code without a corresponding test
- Run the test suite before and after every change

## Coverage
- Minimum 80% line coverage on all new code
- 100% coverage required for: auth logic, payment flows, data validation
- Coverage below threshold is a build failure — do not merge

## Test Structure
- Test file mirrors source path: src/users.ts → tests/users.test.ts
- One describe block per module, one it/test block per behavior
- Test names describe behavior, not implementation: "returns 404 when user not found" not "test getUserById error"

## What to Test
- Happy path: expected inputs produce expected outputs
- Edge cases: empty inputs, nulls, boundary values
- Error cases: invalid inputs throw or return correct errors
- Do not test implementation details — test observable behavior

## What NOT to Do
- Never mock the module under test itself
- Never skip tests with .skip without a comment explaining why and a linked issue
- Never assert on console output as a primary test strategy
