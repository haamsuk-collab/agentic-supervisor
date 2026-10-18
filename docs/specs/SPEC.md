# agentic supervisor — Stage B specification

You are building the **supervisor**: a small program that drives a coding agent until a mission's
acceptance checks pass, and that reports only what the checks prove. Read this whole file before
writing code. Build exactly what it says; when something is unspecified, pick the simplest choice
and record it in `DECISIONS.md`.

## 0. Why it exists (the three rules everything follows)

1. **Done means a check passed.** The worker agent's words never count as evidence. A criterion is
   done only when the supervisor itself ran its check and the check exited 0.
2. **Every iteration starts fresh.** The worker is a new process with no memory. Everything it needs
   is in the prompt the supervisor builds from the ledger.
3. **No human in the loop.** The supervisor keeps going until all checks pass or a limit is reached,
   then writes an honest report.

## 1. Constraints

- Python **3.12**, standard library only. Run with `/usr/bin/python3.12`. No third-party packages.
- Target size: about 1,500 lines of code plus tests. Small, plain functions. English comments.
- Never use `shell=True`. Every command is an argv list.
- Do not install anything system-wide, do not use `sudo`, do not edit files outside this folder.
- Tests must not use the network and must not call a real model.

## 2. Layout

```
agentic/            the package
  cli.py            entry point: python3.12 -m agentic ...
  config.py         paths and limits (all overridable by environment variables for tests)
  mission.py        load and validate mission files
  ledger.py         append-only hash-chained ledger
  checks.py         run checks on a snapshot copy, in "local" or "systemd" mode
  worker.py         build the prompt, run the worker, capture its result
  loop.py           the iteration loop, progress tracking, escalation, stop conditions
  report.py         the report, generated from the ledger only
tests/              unittest tests (see section 10)
Makefile            `make test` runs python3.12 -m unittest discover -s tests -v
README.md           short: what it is, how to run it, how to test it
DECISIONS.md        every choice you made that this spec did not dictate
```

## 3. Paths (config.py)

| Name | Default | Env override |
|---|---|---|
| state root (root-only in production) | `/var/lib/agentic` | `AGENTIC_STATE` |
| workspace root (worker-writable) | `/home/agent/missions` | `AGENTIC_WORKSPACES` |
| check mode | `systemd` | `AGENTIC_CHECK_MODE` (`local` in tests) |
| worker command | see section 6 | `AGENTIC_WORKER_CMD` (a JSON argv list; tests point it at a stub) |

Per mission `<id>`:
- `<state>/missions/<id>/mission.json` — the mission (contains the hidden checks)
- `<state>/missions/<id>/ledger.jsonl` — the ledger
- `<state>/missions/<id>/report.md` — the report
- `<state>/scratch/<id>/<iteration>/` — snapshot copies used to run checks
- `<workspaces>/<id>/work/` — the worker's working folder (the only place the worker writes)

## 4. Mission file (mission.py)

```json
{
  "id": "weather-cli",
  "goal": "Plain-language description of what the user wants.",
  "criteria": [
    {
      "id": "c1",
      "text": "Human-readable requirement shown to the worker.",
      "check": { "argv": ["python3.12", "-m", "pytest", "-q", "checks/test_c1.py"],
                 "timeout": 120, "network": false,
                 "files": { "checks/test_c1.py": "<file content>" } }
    }
  ],
  "limits": { "mission_hours": 24, "iteration_minutes": 45,
              "escalate_after": 2, "give_up_after": 2 },
  "ladder": [
    { "model": "anthropic/claude-sonnet-5-5", "thinking": "high",   "iteration_minutes": 45 },
    { "model": "anthropic/claude-opus-5-5",   "thinking": "high",   "iteration_minutes": 45 },
    { "model": "anthropic/claude-fable-5-1",  "thinking": "high",   "iteration_minutes": 90 }
  ]
}
```

Validation: reject unknown keys; ids match `^[a-z0-9][a-z0-9-]{0,40}$`; `argv` is a non-empty list of
strings; `timeout` 1..600; `limits` and `ladder` have the defaults above when omitted.

**Hidden checks.** `check.argv` and `check.files` are never shown to the worker, never copied into the
worker's folder, and never appear in any prompt. The worker only sees `criteria[].id` and `.text`.
`check.files` are written into the snapshot copy (section 5) just before the check runs.

## 5. Running checks (checks.py)

For every check run:
1. Copy the worker folder to a fresh snapshot directory `<state>/scratch/<id>/<iteration>/<criterion>/`
   (skip `.git`, `node_modules`, `__pycache__`, `.venv`). Write the check's `files` into it.
2. Run `argv` with the snapshot as working directory, stdin `/dev/null`, a timeout, and a new process
   group that is killed on timeout.
   - **local mode:** plain `subprocess` as the current user.
   - **systemd mode:** wrap as
     `systemd-run --wait --pipe --collect --quiet -p DynamicUser=yes -p PrivateTmp=yes -p ProtectSystem=strict -p ProtectHome=yes -p NoNewPrivileges=yes -p RuntimeMaxSec=<timeout> -p WorkingDirectory=<snapshot> -p ReadWritePaths=<snapshot>`
     plus `-p PrivateNetwork=yes` unless `network` is true. Make the snapshot world-readable/writable
     only as far as needed for the dynamic user. (Only root can use this mode; tests use local mode.)
3. The result is **only** the exit code: 0 = pass, anything else = fail, timeout = fail.
   Never parse output for words like "passed". Keep the last 4 KB of stdout and stderr for the
   ledger.
4. Delete the snapshot afterwards.

**Red baseline.** Before the first iteration, run every check against the initial (empty) worker
folder. A check that already passes proves nothing: mark that criterion `invalid_check`, exclude it
from the loop, and list it under "⚠️ 확인 불가" in the report.

## 6. Running the worker (worker.py)

Default worker command (production):
```
runuser -u agent -- env HOME=/home/agent /home/agent/.local/bin/omp -p --no-session --no-rules
  --no-skills --no-extensions --no-title --config /etc/agentic/omp-overlay.yml
  --model <model> --thinking <thinking> --max-time <minutes>m <prompt>
```
- **stdin must be `/dev/null`** (omp appends stdin to the prompt).
- Working directory: the worker folder. Timeout: the ladder step's `iteration_minutes` plus 5.
- Record the exit code and the last 4 KB of the worker's final text. That text is stored as an
  **unverified note**: it may be shown to the next iteration labelled as such, never as fact.
- In tests, `AGENTIC_WORKER_CMD` replaces everything before `--model`; the supervisor appends
  `--model <m> --thinking <t> --max-time <n>m <prompt>` so the stub can read them.

The prompt (build it from the ledger, nothing else):
```
You are working on a mission. Work only inside the current folder.
Mission: <goal>
Requirements (each is checked automatically by the supervisor; you cannot see the checks):
- [c1] <text> — currently: FAILING | PASSING
...
Previous iteration note (written by the previous agent, unverified): <note or "none">
<if oscillation detected: "The same set of requirements failed before. Try a different approach.">
Rules:
- When something is unclear, choose the safest reasonable option and append it to DECISIONS.md
  as one line: "- <decision> (reason)".
- Do not stop to ask questions. Nobody will answer.
- Finish by describing what you changed. Your description is not evidence; only the checks are.
```
Never include check argv, check file names, check file contents, or check output in the prompt.

## 7. The loop (loop.py)

```
load mission; open ledger; if the ledger is new: write "mission_started", run the red baseline
step = current ladder step (from ledger), stalls = 0
while there are failing (non-invalid, non-given-up) criteria:
    stop if mission time is used up            -> "stopped_time"
    run the worker for one iteration           -> ledger "iteration_worker"
    run every remaining check                  -> ledger "check_result" (one per criterion)
    failing = set of failing criterion ids
    if failing is smaller than the best (smallest) failing set so far: stalls = 0, record progress
    else: stalls += 1
    if failing equals a failing set seen in an earlier, non-consecutive iteration: set oscillation flag
    if stalls >= escalate_after and a higher ladder step exists: step += 1, stalls = 0 -> "escalated"
    elif stalls >= escalate_after + give_up_after on the last step:
         mark every still-failing criterion "gave_up" -> "gave_up"
all done -> "mission_finished" with the final status; write the report
```
- **Resume:** `run` on an existing mission rebuilds all state (step, stalls, best set, seen sets,
  statuses) from the ledger and continues. Killing the process at any point and running again
  must neither lose nor repeat a finished iteration's records.
- A criterion that passed once and later fails again counts as failing.

## 8. Ledger (ledger.py)

- JSON Lines. Record: `{"seq": n, "ts": "<UTC ISO8601>", "type": "...", "data": {...},
  "prev": "<hash of previous record>", "hash": "<sha256 of canonical JSON of this record without hash>"}`.
  First `prev` is 64 zeros. Canonical JSON: `sort_keys=True, separators=(",", ":"), ensure_ascii=False`.
- Append with `O_APPEND`, `fsync` after each record.
- `verify()` recomputes the chain and reports the first bad record. Any command that reads the
  ledger calls `verify()` first and refuses to continue on a broken chain.
- Record types: `mission_started`, `baseline`, `iteration_worker`, `check_result`, `escalated`,
  `gave_up`, `stopped_time`, `mission_finished`.

## 9. Report (report.py)

Written in Korean, generated from the ledger only (never from the worker's text, except the
decisions file, which is clearly labelled as AI-written). Template:

```
# 미션 보고서: <id>
목표: <goal>
상태: 완료 | 일부 완료 | 중단(시간 초과)      걸린 시간: <h>시간 <m>분      반복: <n>회
쓴 모델: <ladder steps actually used>

## ✅ 된 것 (감독자가 직접 검사해서 통과)
- [c1] <text> — 마지막 검사 <time>, 종료 코드 0
## ❌ 안 된 것
- [c2] <text> — <failing | gave_up: "n번 시도 후 포기" | stopped_time>
## ⚠️ 확인 불가
- [c3] <text> — 작업 전에도 통과하는 검사라서 증거가 되지 못함
## 📝 AI가 정한 것 (AI가 쓴 기록, 검증 안 됨)
<lines of DECISIONS.md from the worker folder, or "없음">
```

## 10. Tests (tests/, unittest, local check mode, stub worker)

Write a stub worker script for tests that reads `--model`, the prompt, and an environment variable
telling it which scenario to play. Required tests:

1. Ledger: appends chain correctly; editing, deleting or reordering any record is detected.
2. Checks: exit 0 passes, non-zero fails, timeout fails and kills the process group, output
   containing "ALL TESTS PASSED" with exit 1 still fails, argv is never run through a shell.
3. Red baseline: a check that passes on the empty folder becomes `invalid_check` and is excluded.
4. Happy path: stub fixes everything in one iteration → status 완료, all criteria ✅.
5. **False completion:** stub writes "All done, everything works!" but changes nothing → never ✅,
   loop continues, report shows ❌.
6. Escalation: stub never makes progress → moves sonnet → opus → fable after the configured stalls,
   then gives up; the ledger shows each step; the report shows ❌ gave_up.
7. Oscillation: stub alternates between two failing sets → the next prompt contains the
   "different approach" line.
8. Resume: run, kill after one iteration, run again → continues from the ledger with no duplicate or
   missing records, final result identical to an uninterrupted run.
9. Hidden checks: no prompt ever contains check argv, check file names or contents.
10. Worker stdin is `/dev/null`.
11. Time limit: a mission whose time is used up stops with "중단(시간 초과)".
12. Report: every ✅ line corresponds to a passing `check_result` in the ledger.

## 11. Done means

- `make test` passes, and you show its full output at the end.
- `README.md` and `DECISIONS.md` exist.
- Commit your work to the git repository in this folder with a clear message.
- Out of scope for this stage (do not build): automatic planning of criteria, AI reviewers,
  the researcher, notifications, the systemd service unit, installing anything.
