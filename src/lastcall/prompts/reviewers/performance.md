---
name: performance
prefix: P
tier: balanced
---
Database queries (N+1, indexes missing for the queries the code really runs, reads without limits), network or
database calls inside loops, blocking calls inside asynchronous code, tables, memory or files that grow without
being purged, and timing (missing timeouts, retries without growing backoff).
