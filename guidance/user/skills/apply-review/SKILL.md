---
name: apply-review
description: Apply the numbered fixes from a code review, one test per fix, then commit. Use when the user pastes review findings ("검토 결과 … 고쳐") or runs /skill:apply-review.
---
# Apply review fixes

**CRITICAL:** Fix only the listed items, one failing-then-passing test per fix, and commit only when `make test` exits 0.

The user's message lists numbered fixes. Treat each number as one task.

1. Restate each fix in one line before changing anything. If a fix is unclear, choose the safest reading and say which one you chose.
2. Change only what the fixes ask for. No refactors or extra features.
3. For each fix, add a test that fails without the fix and passes with it.
4. Never edit SPEC files. Never weaken existing tests.
5. Run `make test > /tmp/test.log 2>&1; echo exit=$?` (never print the whole log; read its last 5 lines and any FAIL/ERROR blocks); commit only when it exits 0.
6. Reply in Korean, conclusion first: a table of fix number → what changed → test name, then the `make test` result (count and exit code) and the commit hash.

**CRITICAL:** Fix only the listed items, one failing-then-passing test per fix, and commit only when `make test` exits 0.
