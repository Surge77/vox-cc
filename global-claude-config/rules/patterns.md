# API Patterns

## Response Format (default — override per project in local CLAUDE.md)
Success:
  { "success": true, "data": <payload>, "timestamp": "<ISO-8601>" }

Error:
  { "success": false, "error": { "code": "<SCREAMING_SNAKE>", "message": "<user-safe string>" }, "timestamp": "<ISO-8601>" }

## Rules
- Never expose internal error messages, stack traces, or DB errors to the client
- Always include a timestamp in responses
- HTTP status codes must be semantically correct: 200 OK, 201 Created, 400 Bad Request, 401 Unauthorized, 403 Forbidden, 404 Not Found, 500 Internal Server Error
- Use cursor-based pagination, not offset-based; include hasMore boolean
- Default page size: 20; max page size: 100

## Validation
- Validate all inputs at the boundary (controller/handler layer)
- Return 400 with field-level errors for validation failures
- Never let unvalidated data reach the service or DB layer
