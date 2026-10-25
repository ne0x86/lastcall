

## Your first attempt did not pass

Your change is still in `/work`: `git diff` shows it. lastcall ran `${command}` on it ${where}, and the check failed.
The last lines of its output:

```
${tail}
```

Fix the change so that the check passes. Keep it as small as before, and inside the same findings. Do not delete,
skip or weaken tests to make them pass. If the failure has nothing to do with your change (a test that already
failed, a service the tests need), say so in `pending` and set `done` to false. Answer with the JSON of the whole
change, as before.
