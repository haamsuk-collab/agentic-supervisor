---
description: Edits to tests, checks, or specs need a stated reason
condition: ["**/work/**/tests/**", "**/work/**/test_*.py", "**/work/**/*_test.py", "**/work/**/*.test.*", "**/work/**/SPEC*.md", "**/missions/**/_agentic_check_*"]
interruptMode: never
---
**CRITICAL:** Never weaken a test, a check, or a spec to make work pass.

You are editing a test, a check, or a spec. Rule 2.6 of RULES.md: never weaken a test or a check to make work pass. If this edit changes what is being tested, tell the user exactly what changed and why in your reply. SPEC files are root-owned and must not be edited at all.

**CRITICAL:** Never weaken a test, a check, or a spec to make work pass.
