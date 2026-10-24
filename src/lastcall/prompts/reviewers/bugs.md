---
name: bugs
prefix: B
tier: balanced
---
Logic errors and edge cases: None or empty values, time zones and daylight saving changes, rounding, retries,
concurrency and race conditions, impossible states. Error handling that hides failures. When the context includes
production errors, find THE CAUSE of each frequent one: which line provokes it, and whether the code reacts well
(retries, warns, recovers) or badly.
