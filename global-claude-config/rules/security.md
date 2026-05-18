# Security Rules

## Mandatory Checks
- Never commit secrets, API keys, tokens, or credentials — not even in comments
- Always validate and sanitize all user inputs before use
- Use parameterized queries for all database operations — no string concatenation in SQL
- Flag any use of eval(), exec(), or dynamic code execution and require explicit approval
- Never log sensitive data (passwords, tokens, PII)

## Dependencies
- Do not add new dependencies without checking for known vulnerabilities first
- Prefer well-maintained packages with recent commits and high download counts

## Auth & Access
- Never store passwords in plain text
- Always use HTTPS for external requests
- Do not expose internal error details in API responses — use generic messages to the client

## Code Review Trigger
- If a change touches auth, payments, file uploads, or external APIs — invoke the security-reviewer agent automatically
