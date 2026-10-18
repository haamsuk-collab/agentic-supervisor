# agentic-supervisor — project guide

**CRITICAL:** Done means a supervisor-run check exited 0, and hidden checks never reach a worker, a report, or a notification.

Version: 2026-10-18.1 · Source: /etc/agentic/guidance (root-owned)

A supervisor that drives coding agents in fresh sessions until hidden checks pass, and reports only what the checks prove.

## Specs (root-owned, never edit)
`SPEC.md` (B: loop, ledger, checks) → `SPEC-C.md` (planning, cheat review) → `SPEC-D.md` (Telegram, outages, admin) → `SPEC-E.md` (briefings, /files, follow-ups). A later spec changes an earlier one only where it says so.

## Layout
- `agentic/`: `cli` (entry), `config` (paths, limits, model tables), `mission`, `ledger` (hash chain, UTC), `checks` (snapshots, systemd sandbox), `worker`, `loop`, `report`, `planner`, `llm`, `review`, `outage`, `notify`, `spool`, `inbox`, `admin`, `commands`, `telegram`, `display` (KST).
- `tests/`: unittest only; stubs `stub_worker.py`, `stub_planner.py`; fakes for Telegram and system commands.
- `deploy/`: systemd units and launchers that root installs by hand.

## Invariants (a change that breaks one is wrong even if tests pass)
1. Done means a check passed: only exit code 0 of a supervisor-run check counts.
2. Hidden checks never reach a worker prompt, the worker folder, a report, or a notification.
3. The ledger is append-only, hash-chained, and in UTC; all state is a replay of it.
4. Workers run as `agent`; planners and reviewers as `agplan`; the bot as `agbot`; nothing that parses outside input runs as root.
5. Python 3.12 standard library only. No `shell=True`. Tests use no network and no real model.
6. Text the user reads is Korean, follows the writing rules in `RULES.md` and `TEMPLATE-RULES.md` (root-owned, never edit), and shows times in KST.

## Working here
- Run tests: `make test` (about 300 tests, about 90 s).
- Record every open choice in `DECISIONS.md`, one line each.
- Root reviews each commit, re-runs the tests, mutation-tests the change, and installs it from `git archive <commit>` into `/opt/agentic`. Nothing here is live until then.

**CRITICAL:** Done means a supervisor-run check exited 0, and hidden checks never reach a worker, a report, or a notification.
