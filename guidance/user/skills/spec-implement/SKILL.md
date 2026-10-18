---
name: spec-implement
description: Implement a root-owned SPEC file in the current project exactly, test it, and commit. Use when the user says "SPEC…를 구현해" or runs /skill:spec-implement <SPEC file>.
---
# Implement a spec

**CRITICAL:** Build exactly what the SPEC says, never edit SPEC files, and commit only when `make test` exits 0.

Argument: the SPEC file to implement (for example `SPEC-E.md`). If none is given, ask which one.

1. Read the whole SPEC file, then every earlier SPEC it names. The SPEC files are root-owned: never edit them.
2. Read the project's `AGENTS.md`, `README.md`, and `DECISIONS.md` before writing code.
3. Build exactly what the SPEC says. When it leaves a choice open, pick the simplest option and add one line to `DECISIONS.md`.
4. For every requirement, write a test that fails before your change and passes after it. Never weaken an existing test.
5. Run the full test suite quietly: `make test > /tmp/test.log 2>&1; echo exit=$?`. Never print or repeat the whole log; read only its last 5 lines and any FAIL/ERROR blocks (`grep -n -A15 -E '^(FAIL|ERROR):' /tmp/test.log`).
6. Commit with a clear message once the suite exits 0. If it does not, do not commit: report what fails.
7. Reply in Korean, conclusion first:
   - 결론: 구현 완료 or 미완료, with the commit hash.
   - 시험: the test count and the exit code of `make test` (the last summary lines only; failures in full if any).
   - 만든 것: one line per SPEC section.
   - 스스로 정한 것: the new `DECISIONS.md` lines.
   - 못 한 것: anything left out, and why. Write `없음` only if that is true.

**CRITICAL:** Build exactly what the SPEC says, never edit SPEC files, and commit only when `make test` exits 0.
