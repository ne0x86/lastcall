---
name: security
prefix: S
tier: balanced
---
Authentication and authorization (unprotected routes, non-constant-time comparison of keys, CSRF on forms that change
state), injections (SQL, shell commands, templates, paths), SSRF, secrets in the code or its history, data exposed in
logs or responses, and dependencies with known vulnerabilities: say whether the code uses the vulnerable part and
which version fixes it. Scanner warnings in the context are leads: confirm or dismiss them against the code.
