# Decisions

Choices the spec did not dictate. Each is the simplest option that keeps the three rules
(a check passed = done; fresh worker every iteration; no human).

## Ledger and resume
- Record `seq` starts at 1. `ts` is UTC ISO 8601 with milliseconds and a `Z` suffix.
- Times people read (report.md `마지막 검사`, the `mission list`/`status` lines, Telegram replies and notifications that
  carry them) go through `display.display_time` only: converted with `zoneinfo` to `config.DISPLAY_TIMEZONE`
  (`Asia/Seoul`) and shown as `2026-10-18 14:53:21 KST` (to the second; the zone name comes from tzdata). The ledger
  `ts`, its hash chain and its format stay UTC. Mission ids keep `m-<UTC yyyymmdd-hhmmss>` (SPEC-C.md): an id is a
  name, not a time display. A naive datetime is refused rather than guessed.
- A hash chain cannot show that records were cut off the **end** of the file. `verify()` detects edits,
  deletions and reordering of anything but a pure tail truncation. Torn last lines (no `\n`) are reported as broken.
- The ledger is the only state. `loop.LoopState` is a fold over the records; the live loop applies each record
  it appends through the same code path as resume, so resumed and uninterrupted runs cannot diverge.
- `mission_started` stores the *public* mission view (id, goal, criteria id+text, limits, ladder). Prompts and the
  report are built from it, never from the mission file, so check data cannot leak into either.
- `baseline` is one record holding every baseline result plus the list of `invalid` ids. A kill before it is
  written simply re-runs the baseline.
- An iteration is "finished" when every criterion listed in its `iteration_worker.active` has a `check_result`.
  After a kill in between, resume runs only the missing checks (the worker is not run again). A decision
  (escalate / give up) that was computed but not yet written is re-derived from the ledger and then written.
- A worker run that was killed *before* its `iteration_worker` record is durable is not a finished iteration and is
  repeated; its output was never recorded.
- After `gave_up` and after `stopped_time` the ledger also gets a `mission_finished` record (final status:
  `complete`, `partial`, `stopped_time`). `run` on a finished mission only rewrites the report.

## Loop rules
- "Smaller failing set" means strictly fewer failing criteria than the best so far. The best starts as the
  number of non-invalid criteria at the baseline.
- Oscillation is evaluated per iteration: the flag is on for the next prompt iff the latest failing set equals
  one from an earlier iteration other than the immediately preceding one. Consecutive identical sets (a stuck
  worker) are not oscillation. The baseline is not counted as an earlier iteration.
- Give-up on the last step needs `escalate_after + give_up_after` stalls counted from arriving on that step
  (the counter resets on escalation), exactly as the pseudocode reads. A one-step ladder never escalates.
- Final status: `complete` only if every criterion is passing; any invalid_check or gave_up criterion makes it
  `partial` (일부 완료). A mission whose checks all pass at baseline is `partial` and never calls the worker.
- Time is measured with the ledger clock from the `mission_started` timestamp to now, so it survives restarts and
  includes downtime between runs. Time is checked before each iteration, not during one.
- Invalid-check criteria are left out of the prompt (they are not checked, so "currently" would be a lie).
- `attempts` in `gave_up` and in the report ("n번 시도 후 포기") is the number of iterations run so far.

## Mission file
- Defaults for missing parts: check `timeout` 120, `network` false, `files` {}; ladder `thinking` "high" and
  `iteration_minutes` = `limits.iteration_minutes`. Partial `limits` objects are filled from the defaults.
- `mission_hours` and `iteration_minutes` may be fractional (formatted as `--max-time 0.5m`); counts are integers.
- `check.files` keys must be relative paths without `..`. Criterion ids use the same pattern as mission ids.
- The CLI accepts a mission id or a path to a mission file. A file is copied to `<state>/missions/<id>/mission.json`
  (mode 0600); if one already exists with different content the CLI refuses.

## Checks
- Output goes to temp files, not pipes, so orphaned grandchildren cannot keep a read open; the process group is
  killed after a normal exit too, not only on timeout. The pid is only used as a group id while it is still
  unreaped (no pid-reuse race).
- The snapshot copier never follows symlinks and skips fifos, sockets and devices (a fifo would hang a plain copy).
  Check files are written with `O_NOFOLLOW` after removing any worker-planted symlink on their path, so a worker
  cannot redirect a check file outside the snapshot.
- A check that cannot start (missing program) fails with `exit_code: null`.
- Snapshot folders are `<state>/scratch/<id>/<iteration>/<criterion>-<32 random hex>/` (128 bits from `secrets`), so
  a leftover process cannot guess where the next check will run (spec section 5 names the folder `<criterion>`
  only; the random suffix is a requested change). A killed run can leave snapshots behind; every `run` deletes
  `<state>/scratch/<id>/` once it holds the mission lock.
- systemd mode (untested here: needs root): the snapshot tree is opened with `a+rw` (symlinks never chmod-ed) and
  `scratch/`, `scratch/<id>/`, `scratch/<id>/<n>/` get mode 0711 so the DynamicUser can reach it. `<state>` itself
  must then be at least `0711` (the hidden mission files live in `missions/`, which stays 0700). The outer Python
  timeout is `timeout + 10 s`; `RuntimeMaxSec` does the real enforcement.

## Concurrency
- `run` and `report` take an exclusive non-blocking `flock` on `<state>/missions/<id>/lock`; a second process
  fails at once with "already running" (exit code 2) and does not touch the ledger. The descriptor is not
  inheritable, so workers and checks never hold the lock, and the kernel frees it when the supervisor dies in any
  way (SIGKILL included). `verify` is read-only and takes no lock.

## Test time limits
- Every test class derives from `helpers.TimedTestCase`: a SIGALRM timer (60 s per test, `TIMEOUT` can be raised
  per class, at most 300 s) turns a hang or endless loop into a failed test. The slowest test takes about 5 s.
  `run_process` kills its child's process group if it is interrupted, so a timed-out test leaves no process
  behind. One test checks that every test in the suite is a `TimedTestCase`. Subprocess calls in tests carry their
  own timeouts as well.

## Worker
- The default worker command **deliberately differs from SPEC section 6** (change requested after review; SPEC.md is
  unchanged): instead of `runuser -u agent`, the worker runs as a one-off systemd unit
  `systemd-run --uid=agent --wait --pipe --collect --quiet -p KillMode=control-group -p TimeoutStopSec=10
  -p RuntimeMaxSec=<s> -p WorkingDirectory=<worker folder> -p Slice=user-<agent uid>.slice -- env HOME=/home/agent
  .../omp ...`.
  When omp exits, systemd kills the whole control group, so processes the agent detached with `setsid`/`nohup`
  die after every iteration. `TimeoutStopSec=10` bounds how long a SIGTERM-ignoring leftover can delay the end of
  the unit. `RuntimeMaxSec` = `(iteration_minutes + 5)` minutes enforces the timeout inside systemd (killing only
  the `systemd-run` client would leave the unit running); the client-side backstop is 10 s longer.
  `AGENTIC_WORKER_CMD` still replaces everything before `--model` and is used unwrapped (tests use a stub).
  Not run here (needs root): tests check the exact argv only.
- `Slice=user-<uid>.slice` (uid from `pwd.getpwnam("agent")` at the moment the command is built) keeps the worker
  unit under the agent's own resource limits instead of `system.slice`. If the user does not exist the run stops with
  a `ConfigError` (exit 2) before an `iteration_worker` record is written, so it can be resumed after fixing it.
- systemd expands `${NAME}` and turns `$$` into `$` in every command argument, and this server's systemd 239 has no
  `--expand-environment=no`. Every `$` in the arguments after `--` (the omp options, model, thinking, max-time and
  the prompt) is therefore doubled (`worker.systemd_escape`); systemd turns it back. `%` is left alone: transient
  units do not expand specifiers on 239 (checked by running `systemd-run --user`). Unit properties (`-p ...`) and
  the `AGENTIC_WORKER_CMD` override are not escaped. A test runs the real `systemd-run --user` round trip (with an
  unescaped control run) and skips itself where no user manager is available.
- The worker's stdout tail is the "note"; its stderr tail is kept in the ledger but never shown to the next worker.
- The test stub reads its scenario from `AGENTIC_STUB_PLAN` (a JSON plan per call) rather than from a name.

## Report
- 걸린 시간 = first to last ledger timestamp. Since Stage E the report shows the worker models by short name
  (`config.MODEL_SHORT_NAMES`), see "Stage E".
- Report sections never trust worker text. `DECISIONS.md` is read without following symlinks, only if it is a
  regular file, at most 64 KB, and each line is rendered as a `> ` quote so it cannot imitate a heading or a ✅ line.
- `🛠️ 진행 중` (or `🧭 계획 중`) is the headline if the report is generated from an unfinished ledger.
- ❌ reasons (Stage E wording): `<n>번 시도 후 포기`, `시간이 다 되어 멈춤`, or `아직 통과하지 못함`.

## CLI
- `python3.12 -m agentic run|report|verify <id-or-file>`. Exit codes: 0 complete, 1 finished but not complete,
  2 error (including a broken ledger).

# Stage C (SPEC-C.md): planning and cheat review

Choices SPEC-C.md did not dictate. SPEC.md and SPEC-C.md are unchanged; Stage B rules still hold.

## Planner/reviewer calls (llm.py)
- The call's working directory on the supervisor side is `/`. The prompt goes to omp on stdin, never in argv
  (changed from SPEC-C's "stdin must be `/dev/null`" on request: `omp -p` without a positional prompt reads stdin, and
  `systemd-run --pipe` passes stdin to the unit). `run_process` got an optional `stdin` (bytes) argument; the bytes
  are fed from a temp file, so a child that does not read them cannot block the supervisor. The worker keeps stdin
  `/dev/null`. Not being argv, the prompt is not `$`-escaped and has no 128 KiB limit; a prompt larger than 120,000
  bytes (the prompt budget) or containing a NUL is not run: the attempt is recorded as failed and the step fails at
  once (the same prompt would fail on every model).
- At most 1 MiB of a model's stdout is read (the last 1 MiB; the answer is at the end). Worker/check output keeps its
  4 KB tail. `run_process` got an optional `stdout_bytes` argument for this.
- The JSON is found by decoding from each ```` ```json ```` fence with `raw_decode`, so a ```` ``` ```` inside a JSON string
  (a check script containing a markdown fence) does not end the block. The last block must parse; an earlier good block
  is not a fallback. ```` ```json5 ````/```` ```jsonc ```` are not our fence. The whole-output fallback applies only
  when there is no fence at all. The answer must be a JSON object.
- A call that cannot start (missing program) is a failed attempt (`kind` "start") and is retried/falls back like any other.
- The client-side time limit is `RuntimeMaxSec` (1200 s) for an override command and +10 s for the real `systemd-run`
  (same as the worker). Tests patch `config.PLANNER_RUNTIME_SECONDS`.
- Every attempt record (`plan_step`, and the attempt list inside `review`) also stores `stderr` (4 KB tail), and for a
  rejected answer the last 2,000 characters of stdout. The ledger is root-only.

## Planning flow
- `plan_step` has extra fields: `round` (0 for the first call of a step; 1 and 2 for the `checks` re-runs and the
  `critique` rounds that follow them) and `thinking`. `attempt` counts calls within one step-round (1-4).
- The supervisor's empty-folder results are stored inside the `critique` `plan_step` records (`empty_folder`:
  criterion id -> problem or null), not recomputed on resume, so a resumed run takes the same decisions as an
  uninterrupted one even if a check were flaky. They are computed before the critique call.
- Only a criterion that was just (re)written is reviewed again: round 0 critiques every criterion; after a re-run the
  critique covers only the re-written ones. Accepted checks are final ("keep the accepted checks unchanged"), so a
  later, different AI opinion cannot veto them. The critique prompt shows the checks under review only; the
  criterion texts of those are listed. The `checks` re-run prompt shows the rejected check and its problem.
- Veto problems from the empty-folder run and the critique are joined with `; `. A check that cannot even be set up
  (e.g. conflicting file paths) is vetoed with `the check could not be set up: ...`.
- The empty-folder run uses a fresh empty temporary folder, not `<workspaces>/<id>/work`.
- The critique schema allows `problem` to be absent when `ok` is true. `design.approach`, `brief.goal`, criterion text
  and every list item must be non-empty; strings with a NUL character are rejected (they could not be put in argv).
  A check's `argv` must not contain NUL either (`mission.parse_check`).
- Prompts carry a `Your step: <name>.` line (second line) and, for `checks`/`critique`, a line
  `Write checks for exactly these criterion ids: ...` / `Review exactly these criterion ids: ...` that the stub reads.
  Untrusted text sits between `=====BEGIN <LABEL>=====` / `=====END <LABEL>=====` markers. If the text itself contains
  the marker, ` #<n>` is added to both markers until it does not (so a request cannot close its own block).
- `request.md` is hashed at `plan_started`; a changed file at resume is a ledger error.
- Planning that ended `plan_failed` stays failed: `run` again only rewrites the report. The failing step's `reason` is
  the last error. For `checks` schema errors the reason is generic ("details are in the ledger") because validation
  messages can name check files and the report must not show them.
- When every criterion is dropped, the plan fails with step `checks`, kind `dropped`.
- A dropped criterion's problem is shown in the report. If it shares 16 characters with any generated check (the review's
  leak filter) it is replaced by `details withheld: they would reveal check content`.
- If the mission file was written but `plan_finished` was not, resume replays the recorded answers (no model call),
  rewrites `mission.json` atomically and records `plan_finished`.
- `mission_started` holds the full request (the report prints it from the ledger); `plan_started` holds only sha256 and size.
- The planned mission uses the default `limits` and `ladder`. Planning time does not count against `mission_hours`
  (that clock starts at `mission_started`).

## Review
- Runs when no criterion is failing, the mission has an `approach`, at least one iteration ran and at least one
  criterion is currently passing, and this iteration has not been reviewed yet. The review also runs for a mission that
  would finish `partial` (give-up/invalid criteria) as long as something passes.
- One `review` record per review call, also when the call failed (`ok: false`, `findings: []`). It holds the winning
  model, all findings with `verified`, the attempts, the file names shown and the skipped files. `review_veto` follows
  it when there is at least one verified finding and fewer than 2 vetoes so far.
- A criterion is failing again after a veto only if it was passing; a finding on a gave_up/invalid criterion still
  counts as a veto (the spec's rule) but changes no status.
- The `Reviewer finding` lines stay in every worker prompt until the next review record. Quotes and problems are put on
  one line (whitespace collapsed) in prompts and in the report.
- Leak filter: a `problem` leaks if it shares a 16-character substring with any check's single argv element, the
  space-joined argv, or any check file content (all checks of the mission).
- Prompt budget: the file text is limited to 300 KB and also to what fits in the 120,000-byte prompt budget (with
  25 KB reserved for the skipped list and markers). Files left out by the budget are
  listed with `(over the total size limit)`. Other skipped files: not regular, over 100 KB, not UTF-8, NUL (binary),
  symlinks. `.git`, `node_modules`, `__pycache__` and `.venv` folders are skipped without listing.
- A finding's `quote` must be non-empty, at most 300 characters; `problem` at most 500. Longer values are a schema
  error (retry). Findings of an answer are not capped in number.
- A finding is verified only if its `quote` is at least 20 characters long after stripping leading/trailing whitespace
  (a shorter excerpt proves little), in addition to SPEC-C's three rules. Every finding in the `review` record has
  `reason`: null when verified, otherwise the first failed rule (short quote, file not shown, quote not verbatim,
  unknown criterion). The reviewer prompt asks for quotes of 20 to 300 characters.
- Oscillation is never flagged for an empty failing set (that set only occurs around a review veto).
- A vetoed criterion has no ✅ in the report even if the time ran out before its next check.

## Report
- `요청:` is printed for planned missions (first 300 characters, whitespace collapsed to one line).
- A failed plan (Stage E): the notice `계획 <step> 단계 실패: <reason>` under `## 5. 알아 두실 것`, no ❌ line; the report
  has no goal line any more (SPEC-E section 3 layout).
- ⚠️ order: invalid checks, then dropped criteria (mission order). `꼼수 리뷰를 하지 못했습니다` is a notice since Stage E.
- `[계획]` lines come from the mission's `assumptions` (or the recorded brief if there is no mission yet).

## CLI
- `new <request-file> [--id ID]`: exit codes as for `run`, so a failed plan exits 1. The mission folder is created with
  mode 0700. A request of exactly 20 KB (20,480 bytes) is accepted; whitespace-only, non-UTF-8 and NUL requests are
  refused before anything is written.
- `run <id>` on a folder with `request.md` and no `mission.json` plans first; the lock is held for the whole planning.

## Tests
- `tests/stub_planner.py` plays planner and reviewer; the scenario is in `AGENTIC_STUB_PLANNER` (see its docstring).
  `PlannedTestCase` in `helpers.py` is the base class for planned-mission tests.

# Stage D (SPEC-D.md): Telegram, model outages, operations

Choices SPEC-D.md did not dictate. SPEC.md, SPEC-C.md and SPEC-D.md are unchanged; earlier rules still hold.

## Model outages
- A worker run whose exit code is `None` (timed out, or could not start) counts as a non-zero exit. A timed-out run is
  never "fast", so only the outage patterns can make it unavailable; a run that could not start (missing program) and
  changed nothing is unavailable.
- The pattern test looks only at the worker's stderr tail (the 4 KB the ledger keeps), never at its final text: omp
  reports model errors on stderr and exits 1, while the agent's own text may mention gateways or quotas as part of
  normal work. This narrows SPEC-D.md section 6 ("stderr or final text"). `reason` is the first matching line
  (whitespace collapsed, at most 200 characters) as `오류 메시지: <line>`, or for the fast-exit rule
  `<n>초 만에 종료 코드 <c>로 끝났고 작업 폴더가 그대로임`. The folder is hashed before every worker run and again only
  when a failed run was fast.
- Folder digest: depth-first by name, SNAPSHOT_SKIP folders skipped at every depth; a file counts by path and the
  sha256 of its content, a symlink by path and link text (never followed), a folder by its path (so a new empty folder
  is a change), other entries by path. A missing folder hashes like an empty one.
- `worker_unavailable` data: `iteration` (the one being attempted, unchanged), `step`, `model` (the model actually
  run), `thinking`, `reason`, `attempt` (1, 2, ... within this outage), `wait_seconds`, `next_model` (the alternate
  provider for the next try, or null = the step's own model), plus `exit_code`, `timed_out`, `duration_seconds`,
  `stderr`, `note` (root-only ledger). With an alternate the wait after the step's own model is 0; the wait after the
  alternate (or after a model without one) is the backoff. The backoff index counts the waits of this outage, so the
  pairs go (0, 5 min), (0, 10 min), (0, 20 min), (0, 40 min), (0, 60 min), ...
- An outage ends with the next `iteration_worker`. The alternate is only ever used right after its own model was
  unavailable; `iteration_worker.model` records the alternate when that attempt worked.
- Waiting: before a worker run the supervisor sleeps until `ts(last worker_unavailable) + wait_seconds`, but never past
  the mission deadline; at the deadline the usual `stopped_time` follows. On resume the same rule sleeps only the rest.
- The ⚠️ line `- 모델을 쓸 수 없어 기다린 시간: <h>시간 <m>분` appears when `stopped_time` is recorded during an outage; the
  time is from the outage's first `worker_unavailable` to `stopped_time`.
- Planner/reviewer outage = `llm.ask` failed, every attempt failed with kind `exit`, `timeout` or `start`, and at least
  one attempt's stderr has a line matching the patterns (`StepFailed.outage` is that line); stdout is the model's answer
  and is never searched. One schema, `bad_json` or `no_json` attempt (or a too-large prompt) makes it a normal failure.
- `plan_unavailable` data: `step`, `round`, `attempt` (within this outage), `reason`, `wait_seconds`; for the review
  also `iteration`, with `step` = `review` and `round` 0. The review's waits use the same record type (it is the same
  kind of model call) and are replayed by `LoopState`; the planning steps' ones by `PlanState`.
- The 6-hour planner limit counts the recorded waits of the whole planning (not reset between outages); the last wait
  is cut so the total is exactly 6 h, and the next outage failure ends planning with `plan_failed` (step = the step,
  kind `unavailable`, reason `모델을 6시간 동안 쓸 수 없었음`). The review's 1-hour limit works the same way per review
  (5, 10, 20, then 25 minutes); then the failed call is recorded as a failed `review` (Stage C). Planning time still
  does not count against `mission_hours`, and a review wait does not check the mission clock (as in Stage C, the review
  runs whenever nothing is failing).
- `loop.run` and `planner.plan` take injectable `clock` and `sleep` (defaults: UTC now, `time.sleep`).

## Notifications
- Hooked into the one `emit` of `loop._run_locked` (planning and the loop share it): append, apply to `LoopState`,
  then `notify.Notifier.on_record`. A failure is printed to stderr as `agentic: notification for <id> record <seq>
  failed: ...` and ignored. A missing outbox folder, or an existing file of the same name, skips silently.
- The outbox file of a notification is named `<mission id>-<seq>` (`O_EXCL`), so one ledger record can never produce
  two files. Records replayed on resume are never notified again; a crash between the ledger append and the outbox
  write loses that one notification (best effort) rather than risking a duplicate. `notify-failure` uses a random name.
- Outbox items are written by `agentic.outbox` (Stage E shapes, see "Stage E") through `spool.write_item`:
  `<name>.tmp` with `O_CREAT|O_EXCL|O_NOFOLLOW` (0600) inside the folder opened with `O_DIRECTORY|O_NOFOLLOW`, fsync,
  `fchown` to agbot only when running as root (hand runs and tests are not root), then `rename` in the same folder. An
  existing entry of the final name (a planted symlink included) is refused.
- Texts are Stage E cards (see "Stage E"). `plan_finished` counts the criteria in the written mission.json; `escalated`
  names the ladder models of `from_step` and `to_step`; the outage card is sent for the first `worker_unavailable` whose
  time is at least 30 minutes after the outage's first one (`<minutes>분째` = that difference), once per outage.
- `notify-failure <id>` asks `systemctl show --property=SubState --value agentic@<id>.service`: `auto-restart` means
  systemd will restart the unit (`자동으로 다시 시작합니다`), anything else that it gave up (`다시 시작하지 않습니다`).

## Admin, `mission` and the inbox processor
- Request validation lives in `mission.validate_request(raw)`; `cli._read_request_file`, `agentic new` and
  `admin.new_mission` all use it, so the limits and messages are identical (20 KB, UTF-8, not empty, no NUL).
- `admin.create_request` makes the mission folder (0700, an existing folder is refused) and `request.md` (0600,
  `O_EXCL`, fsync); `agentic new` uses it too. `admin.install_mission_file` installs a mission.json (0600, existing
  different content refused, same content accepted) and is used by `agentic run FILE` and `mission start`; its folder
  is now created 0700.
- Default mission id `m-<UTC yyyymmdd-hhmmss>`; when that folder already exists (two requests in one second) `-2`,
  `-3`, ... is appended, up to 100 tries. An id given by hand is never changed: an existing one is an error.
- `admin.new_mission` (so `mission new` and the bot's `new`) is refused with `TooManyMissions`
  (`돌고 있는 미션이 3개라 새 미션을 시작하지 않았습니다`) while `config.MAX_ACTIVE_MISSIONS` (3) or more mission folders
  have a unit whose `systemctl is-active` output is `active` or `activating`; nothing is created. The request is
  validated first, so a bad request gets its own error. `mission start FILE` is not limited. The count and the start
  are not atomic: the inbox processor runs one item at a time, but a `mission new` by hand at the same moment can
  exceed the limit by one.
- Starting a mission = `runuser -u agent -- mkdir -p <workdir>` then `systemctl enable --now agentic@<id>.service`, both
  through `commands.run_command`. A non-zero exit raises `CommandFailed` (command, exit code, first 200 characters of
  stderr); the second command is not run when the first failed. The mission folder and request.md stay.
- Tests: importing `tests/helpers.py` replaces `commands.run_command` with a function that fails the test;
  `helpers.FakeCommands` records argv lists and answers.
- `mission list` line (the CLI; Telegram `/list` and `/status` use the Stage E cards) =
  `<id> · <systemctl is-active output> · 마지막 기록: <type> (<shown time>) · 반복 <n>회`. The state is the
  stripped stdout of `systemctl is-active` also when it exits non-zero, `unknown` when it printed nothing. No ledger:
  `기록 없음`; broken ledger: `원장이 손상됨` (never raised). Iterations = number of `iteration_worker` records.
- Status = the line plus the last 5 ledger records, one Korean line `- <time> <summary>` each. Summaries use only
  counts, ids, model names, exit codes, pass/fail and public texts: a check result shows criterion and pass/fail only,
  `plan_step` shows step, round and ok only (never its answer), `review`/`review_veto` show counts only,
  `iteration_worker` hides note and stderr. A record with unexpected data falls back to `기록 <type>`.
- Unknown mission (no folder under `<state>/missions/<id>`) raises `admin.UnknownMission` for stop, status and report;
  the processor answers `없는 미션입니다: <id>`; `mission` prints `error: unknown mission '<id>'` and exits 2. `report` of a
  mission without report.md answers `아직 보고서가 없습니다`. Folders whose names are not valid ids are ignored by `list`.
- `mission status ID` prints the status, an empty line, then report.md (or `아직 보고서가 없습니다`). `mission report ID`
  regenerates like `agentic report`. `mission new/start/stop` print one line (`mission <id> started|stopped`).
  `mission` exits 2 with `error: agentic mission must be run as root` unless `os.geteuid() == 0`, before touching
  anything.
- Inbox processor: blocking `flock` on `AGENTIC_INBOX_LOCK` (0600, `O_NOFOLLOW`), so a second run waits and then finds
  what is left; a missing inbox folder returns 0 silently. One pass per run, oldest first (mtime, then name); `*.tmp`
  and dot files are skipped. Every item is deleted afterwards through `spool.remove` (a symlink is unlinked, never
  followed; an empty stray folder is removed) even when handling failed; a delete that fails is logged and the pass
  goes on.
- Inbox JSON is parsed strictly: duplicate keys and NaN/Infinity are rejected; only keys `type`, `text`, `id`,
  `reply_to`; `text` (a string) required for `new` and `continue` and allowed only there; `id` required for
  status/report/stop/files/continue (must pass `valid_id`) and allowed only there; `reply_to` optional positive int
  (bool, float, string, 0 and negatives rejected). The content of `text` is checked by `validate_request`, so a bad
  `new` text gets the error reply.
- Every inbox item gets exactly one reply. A rejected item gets `⚠️ 받은 요청을 처리할 수 없습니다: <reason>` (one line, at
  most 500 characters; it carries `reply_to` only when the item was readable and its `reply_to` valid). Any other
  exception becomes `❌ 처리하지 못했습니다: <message>` and is logged as `agentic inbox: ...`; `list` with no mission answers
  `등록된 미션이 없습니다`. A failure to write the reply is logged and the request is still deleted. `agentic inbox` exits 0
  when it ran.

## Telegram bot
- Config `AGENTIC_TELEGRAM_CONFIG` (default `/etc/agentic/telegram.json`) is strict: exactly `token` and `owner_id`; the
  token must match `[A-Za-z0-9:_-]+` (it goes into the URL path), `owner_id` a positive int (not bool). A bad file
  prints one line on stderr (never the token) and the daemon exits 2. State dir `AGENTIC_BOT_STATE` (default
  `/var/lib/agbot`, created 0700 if missing), API base `AGENTIC_TELEGRAM_API`. The bot imports only `agentic.spool` and
  `agentic.config`.
- The API client raises `ApiError(status, description, retry_after)` for an HTTP error (also `ok: false` with HTTP 200)
  and `NetworkError` for URLError/timeout/OSError/non-JSON body. Exception texts never contain the URL (it holds the
  token).
- getUpdates (`allowed_updates` message and callback_query) failures of any kind back off 5, 10, 20, 60, 60 ... s;
  reset after a successful call; `retry_after` (capped at 1 h) lengthens a wait.
- Update handling is at-most-once: a Telegram call that fails while answering an update is logged and the update is
  not retried; the offset (update_id + 1) is written (tmp + rename, 0600) after every update. A malformed update is
  logged and skipped.
- Authorization needs `message.chat.type == "private"`, `from.id == owner_id` and `chat.id == owner_id` (exact ints);
  buttons the same via `callback_query.from.id` and `callback_query.message.chat`. Everything else (groups, channel
  posts, edited messages, other update kinds) is counted in `Bot.ignored` and logged as a count only.
- Plain text is stripped, then must be 10..20,000 UTF-8 bytes (otherwise a Korean explanation, no pending). A request
  whose inbox JSON would exceed 32 KB (escapes included) is answered as too long, so root never receives an item it
  must reject.
- Preview = first 300 characters (plus `…` when cut) and `이 요청으로 미션을 시작할까요?` with buttons `시작` / `취소`
  (callback data `ok:<key>` / `no:<key>`, key = `secrets.token_urlsafe(12)`, 16 characters). Pending items live in
  `pending.json` (0600, atomic) with `expires = now + 3600` (wall clock, so a restart keeps them), pruned on every update.
- An inbox item's `reply_to` is the id of the owner's original message (the request or the `/stop` command), so root's
  reply quotes what the owner wrote; `allow_sending_without_reply` covers a deleted original.
- A button press always calls answerCallbackQuery first. An unknown, forged, malformed, expired or used key creates
  nothing and sends `이미 처리했거나 만료된 요청입니다` as a new message. The pending entry is removed before the inbox
  write and put back if the write fails (the owner is asked to press again). After success the message is edited to
  `접수했습니다` (stop: `멈추기 요청을 접수했습니다`; cancel: `취소했습니다`) without buttons; edit failures are only logged.
- `/list`, `/status <id>`, `/report <id>`, `/files <id>` write their inbox item at once and send no acknowledgement (root's reply is
  the answer). A bad or missing id, or extra arguments, gives the Korean usage line and no item. `/help`, `/start` and
  unknown commands are answered by the bot itself with the usage text (they need nothing from root, and SPEC-D's inbox
  has no type for them). `/cmd@BotName` is accepted; command names are case-insensitive. A non-text message from the
  owner gets a one-line Korean reply. A failed inbox write tells the owner `지금은 요청을 전달할 수 없습니다...`.
- Outbox: a daemon thread flushes every 2 s, oldest first. An item is exactly `{"html": [str, ...], "reply_to"?}`
  (non-empty list, each part 1..4096 characters) or `{"document", "filename", "caption"?, "reply_to"?}`. Anything else
  (unknown key, both shapes, the old `{"text"}` shape, bad JSON, symlink, over 1 MiB) is logged and deleted. The bot
  sends each html part with `parse_mode: "HTML"`, in order, with `reply_to_message_id` + `allow_sending_without_reply`
  only on the first. It no longer splits (root splits between blocks); whitespace-only parts are no longer skipped
  (Telegram's 400 deletes the item).
- 4xx → log and delete; 5xx or network error → keep and stop the pass (order is kept) until the next tick. HTTP 429 is
  rate limiting, not a rejected message: the file is kept and the outbox pauses for `retry_after` seconds. Parts
  already sent are remembered in memory, so a retry in the same process never resends them (a bot restart in the
  middle of a multi-part item may).
- SIGTERM exits at once; all state files are atomic.

## Deployment files
- `deploy/systemd/agentic@.service` is the installed unit plus `OnFailure=agentic-failed@%i.service`;
  `agentic-failed@.service` is oneshot `agentic notify-failure %i`; `agentic-inbox.service` is oneshot with
  `UMask=0077`; `agentic-inbox.timer` is `OnCalendar=minutely`, `Persistent=true`; the bot unit also has `Group=agbot`,
  `After=`/`Wants=network-online.target` and `WantedBy=multi-user.target`. `systemd-analyze verify` found no syntax
  problem. Steps not verified on a real machine are marked in `deploy/README.md`.

# Stage E (SPEC-E.md): briefings, result delivery, follow-up missions

Choices SPEC-E.md did not dictate. SPEC.md, SPEC-C.md, SPEC-D.md and SPEC-E.md are unchanged; earlier rules still hold.

## Precedence over TEMPLATE-RULES.md
- Where root-owned `TEMPLATE-RULES.md` (which describes the Stage B-D templates) conflicts with SPEC-E.md, SPEC-E wins because it explicitly replaces those layouts: HTML cards with `parse_mode: "HTML"` (TEMPLATE-RULES 5.3 says plain text), the SPEC-E report layout (TEMPLATE-RULES 1.2), splitting between blocks (TEMPLATE-RULES 1.3), card titles such as `🚀 접수 │ <id>` (TEMPLATE-RULES 1.1). Everything that does not conflict is kept: times to the second with `KST`, `<n>번 시도 후` on a given-up criterion, `마지막 검사 <KST>, 종료 코드 0` on a ✅ line.

## Briefing (`agentic/briefing.py`)
- `build(records, public_view, decisions_lines, request)`: before `mission_started` exists the caller passes `{"id": <mission id>}`; `None` takes the view from the `mission_started` record. `report.make_briefing` does this for every caller.
- State: the `mission_finished` status (a `plan_failed` record without it counts as `plan_failed`); before that `planning` while there is a `plan_started` but no `mission_started`, else `running`.
- `total` = mission criteria + dropped criteria. `achieved` = criteria whose latest supervisor check (a `check_result`, or the baseline result of an `inherited_pass`) passed with exit code 0; a `review_veto` takes its criteria back until a new check passes. Invalid, given-up and dropped criteria are never achieved.
- Item reasons: `<n>번 시도 후 포기` (n = `gave_up.attempts`), `시간이 다 되어 멈춤`, `아직 통과하지 못함`, `작업 전에도 통과하는 검사라서 증거가 되지 못함`, `믿을 만한 검사를 만들지 못해 제외함: <problem>` (problem cut at 300 characters). A ✅ item's reason holds the review suspicion (`리뷰어가 꼼수를 의심함: <file>: "<quote>"`) or is empty.
- Evidence: one item per mission criterion in mission order, then the dropped ones; `attempts` = `check_result` records of the criterion (the baseline is not counted); last check time and exit code from the latest supervisor check, `-` when there is none.
- `models_used` = the distinct worker models of `iteration_worker` records in order, by short name (`config.MODEL_SHORT_NAMES`, other models: the part after the last `/`). Planner and reviewer models are not listed.
- "running" in the table means `running` or `planning`: both get the running row, and neither gets the `ai_notes` row.
- Several rows with the todo `판단 1건` are shown as one todo `판단 N건` at the place of the first; the dropped/invalid row has its own todo, so its decision is not counted there.
- The table leaves facts and reasons open in some rows: `failed_time` facts = the stopped criteria, reason `남은 조건이 N개입니다`; dropped/invalid facts = `<id> <text> — <reason>` (dropped first), reason `감독자의 자동 검사가 없는 조건입니다`; suspect facts = `<criterion> · <file>: "<quote>"`, reason `리뷰어가 인용한 코드가 파일에 그대로 있습니다`.
- Next-instruction labels: `진행 상황 보기`, `결과물 받기`, `AI가 정한 것을 고칠 때`, `판단 <k>에서 B를 고를 때` / `판단 <k>에서 A를 고를 때`, `고쳐서 다시 보낼 요청`. The given-up instruction is one message: `<id> 이어서: <text 1>은(는) …로 바꿔 줘`, then one line `<text k>은(는) …로 바꿔 줘` per further criterion. Instructions with the same text are given once (e.g. `/files <id>` from two rows).
- Running notice: `지금: 작업 <step+1>단계 · 모델 <short> · 반복 <n>회` (ladder step of the latest `escalated`); planning: `지금: 계획 <step> 단계 · 모델 <short> · 반복 0회` from the latest `plan_step` (`brief` and no model before the first). Plan failure notice: `계획 <step> 단계 실패: <public reason>`.
- Outage wait = the sum over `worker_unavailable` and `plan_unavailable` records of `min(wait_seconds, time to the next record)`; a wait still open at the last record counts 0 (the briefing is a pure function of the records). The notice appears when that sum is above 0 and shows whole minutes.
- `BANNED_PHRASES` (section 6a list plus `도움이 되셨기를`, `에서의`, `에 의해` from RULES.md) lives in `briefing.py`. Tests render every row, card and report section, and also scan every string literal in `agentic/` for them.

## report.md (`agentic/report.py`)
- A ✅ line keeps the evidence of TEMPLATE-RULES 3.1: `- [c1] <text> — 마지막 검사 <KST>, 종료 코드 0` (+ ` — ⚠️ 리뷰어가 꼼수를 의심함: …`). ❌/⚠️ lines: `- [c2] <text> — <reason>`. Result headings carry their counts.
- Escaping: every dynamic field outside a code block is collapsed to one line and backslash-escapes `` \ ` * _ [ ] < > # | `` (the backslash too, so `\` cannot undo an escape). AI notes are `> [계획] <assumption>` / `> <DECISIONS.md line>`, escaped the same way.
- The summary row shows `진행 중` as the end time while the mission runs and `없음` when no worker model ran. The appendix shows `없음` instead of an empty table; its attempts column reads `<n>회`.
- A next-instruction fence is `` ` `` repeated max(3, longest backtick run + 1) with the info string `text`; the text inside is not escaped (it is what the owner copies).
- `report.load_briefing(cfg, id)` (verified ledger, None without records) is the one way other modules read a mission's briefing.

## Telegram cards (`agentic/cards.py`, `agentic/outbox.py`)
- Root formats and splits: a card is a list of HTML blocks (header, todo, one block per decision and per next instruction, remaining, notices, AI notes); `cards.pack` joins blocks with a newline into messages of at most 4,000 characters. A block over the limit becomes `<pre>` chunks of its plain text, escaped one character at a time, so no tag or entity is cut.
- The achievement line is `🎯 <b>달성 a / t (p%)</b> · 감독자가 직접 검사한 결과` (section 6a rule 4). The finish card's time line is `ended_at` (or the latest record time); every time is `display_time` (to the second, `KST`).
- Each next instruction shows its label on its own line, then `<code>text</code>`.
- The card shows at most 3,000 characters of AI notes, then `… 나머지 N건은 /report <id> 보고서에 있습니다`; report.md keeps them all.
- Other cards: `🧭 계획 완료` (`완료 조건 N개[ · 제외 M개]`, then `· <id> <text>` for the first 5), `⬆️ 모델 승격` (`<from> → <to>`, `반복 <stalls>회 동안 실패한 조건 수가 줄지 않음`), `⏸️ 모델 대기` (`<m>분째 기다리는 중 · 모델 <short>`, `이유: …`), `🔍 꼼수 발견` (criteria count and ids, `다시 작업합니다`), `💥 비정상 종료`, `🚀 접수` (new and follow-up, the latter with `이어서 한 미션: <parent>`).
- The `plan_failed` record sends no card of its own: the `mission_finished` finish card right after it carries the reason.
- `💥 비정상 종료` when systemd gave up uses the todo `/status <id> 로 상태 보기` instead of `없음 — 자동으로 진행 중입니다`, which would be false (honesty rule).
- `/status` of a finished mission = the finish card without the report document; running/planning = header, achievement, bar, `모델 <short> · 반복 <n>회`, `⏱ 걸린 시간 …`, `🕒 <KST> 기준`, the todo, and `최근 기록 (KST)` with the last 3 events (`admin.event_text`). No ledger: `<id> · 기록 없음`; broken: `<id> · 원장이 손상됨`.
- `/list` sorts by the latest record's time (missions without records last), at most 20. Without a ledger: `<b>id</b> · 기록 없음`.
- `/report <id>` is answered with the document `report-<id>.md` (caption `미션 보고서 전문 · <id>`), like the finish notification, instead of split text. `mission status/report` on the command line are unchanged.
- Short replies (errors, `⏹️ 멈췄습니다: <id>`, `없는 미션입니다: <id>`, `등록된 미션이 없습니다`) are one escaped block.

## Documents in the outbox
- 20 MB = 20 × 1024 × 1024 bytes (`config.DOCUMENT_MAX_BYTES`). A document is the file `<name>.doc` written like an item (`spool.write_document`), then its item; when the item cannot be written the document is removed again. Item readers (`spool.list_names`) skip `*.doc`; `write_item` refuses such names.
- Notification documents are named `<id>-<seq>-report(.doc)`, after their card `<id>-<seq>`, so a resumed run cannot send them twice. A report over the limit is replaced by the message `보고서가 20MB를 넘어 보낼 수 없습니다 (<size>)`.
- Sizes are MiB with one decimal (`21.3MB`, `20.0MB 이상` when building stopped at the limit).
- The bot's own messages (usage, previews with buttons, errors, button edits) stay plain text without `parse_mode`: they carry owner text and need no formatting.
- Bot document items: `document` must pass `spool.is_document_name`, `filename` must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, `caption` is a string of at most 1,024 characters; the file is read with `spool.read_document` (`O_NOFOLLOW|O_NONBLOCK`, regular file, at most `DOCUMENT_MAX_BYTES`).
- Sending: `api.send_document(params, filename, content)` with `chat_id`, `caption` + `parse_mode: "HTML"` when there is a caption, and the reply fields when `reply_to` is given. Success deletes the document, then the item; 4xx deletes both; 5xx/429/network keeps both and stops the pass. An invalid item or an unreadable, missing, non-regular, oversized or symlinked document is logged; the item and the `.doc` entry itself are removed (`spool.remove`, never followed). Documents are not part of the sent-parts memory (one call).
- `TelegramApi.send_document` builds multipart/form-data by hand: boundary `agbot` + `secrets.token_hex(16)`, redrawn until it occurs in no part; each param a text part (`True` → `true`); the file part `document` with `application/octet-stream`. A file or field name with `"`, CR, LF or NUL raises `ValueError`. Error mapping and token hiding are shared with `call`.

## `/files` (`agentic/results.py`)
- The worker folder is walked with directory fds (`O_NOFOLLOW`, `dir_fd`, fstat on the open fd); no symlink is followed. A worker folder that is itself a symlink or a file counts as no results. Fifos, devices and sockets are skipped before any open; files are opened `O_NONBLOCK`.
- `SNAPSHOT_SKIP` names are skipped as folders at any depth (a file of such a name is kept); folders deeper than 64 levels are not entered; names that are not UTF-8, vanished and unreadable entries are skipped.
- When root runs it, files not owned by the worker user are skipped (a hard link to a root file is never sent). Folders are not owner-checked.
- Order: the report first, then the worker files depth-first by name. A worker file whose path equals `report-<id>.md` is skipped (the supervisor's report wins); the same name in a subfolder is kept. Dates come from the mtime (local time, clamped to 1980..2107), entries are `ZIP_DEFLATED` with mode 0644.
- The zip is written into a size-limited buffer in 64 KiB chunks; passing 20 MB stops at once (`TooLarge`, size shown with `이상`). Only the compressed size is limited.
- The report in the zip is report.md, or (before the supervisor wrote one) the report of the ledger so far, or `아직 보고서가 없습니다`.
- `파일 <n>개` counts every file in the zip, the report included. No worker file at all (missing or empty folder): `결과물이 아직 없습니다: <id>`.

## Follow-up missions
- The bot's pattern `^([a-z0-9][a-z0-9-]{0,40})\s*이어서\s*[:：]\s*(.+)` (dot-all) is matched on the stripped text; text = group 2 stripped. `/continue <id> <text>` takes the text from the raw message (`split(None, 2)`), so it may span lines. Both use the 10..20,000-byte and 32 KB-inbox-JSON limits of a new request, measured on the `continue` item. Preview as for a new request, then `이 내용으로 미션 <id>을(를) 이어서 맡길까요?`; pending kind `continue`. A bad or missing id or text gets `USAGE_CONTINUE`. `demo 이어서:` with nothing after it is a (too short) new request.
- Whether a mission is a follow-up comes only from the root-only file `<state>/missions/<id>/parent` and the `parent` field of mission.json, never from request.md, so a request that looks like a follow-up header does not become one. A `parent` file that is not a valid id makes planning raise `MissionError` before `plan_started`.
- `continue_mission` checks in this order: unknown parent, parent unit `active`/`activating`, bad text, too many missions; a refusal creates nothing. A broken parent ledger is refused with `이전 미션의 기록이 손상돼 있어 이어서 시작하지 않았습니다`.
- `[이전 결과]`: `확인됨: <id> <text>; …` (✅ evidence) `/ 안 됨: <id> <text> (<reason>); …` (every non-achieved item, ⚠️ included); `없음` when empty or when the parent has no records. A parent without request.md is `(직접 작성한 미션)`. The parent request is stripped.
- Over `REQUEST_MAX_BYTES` the composed request is cut in characters: first the previous request (longest prefix ending in `…`), then the text after `[이전 결과] `; if the new text alone is too long: `이어서 맡길 내용이 너무 깁니다. 더 짧게 줄여서 다시 보내 주세요`.
- New id `<base>-c<n>`: base drops one trailing `-c<digits>`, n is the smallest free number (gaps are filled). A candidate that is not a valid id (over 41 characters) uses the default id scheme.
- Order of work: request.md, the `parent` file (0600, `O_EXCL|O_NOFOLLOW`, `<parent>\n`), `runuser -u agent -- mkdir -p`, `runuser -u agent -- cp -a <parent work>/. <new work>/`, `systemctl enable --now`; the first failing command stops (the mission folder stays, like `new_mission`).
- The follow-up paragraph is added to the design prompt only; other steps and the empty-folder veto are unchanged.
- Baseline of a follow-up: passing checks are `inherited_pass` (`invalid: []`); they count as PASSING, stay active and are re-checked every iteration (the worker prompt shows them as PASSING). Missions without `parent` write the old record without the key, and old ledgers replay unchanged.
- The review runs at iteration 0 only when `inherited_pass` is non-empty; a verified finding then vetoes as usual and the worker runs.

# Stage F (SPEC-F.md): worker isolation, proof that checks bite, daily operations

Each line is one choice SPEC-F left open. Stage F supersedes the `runuser -u agent` and `/home/agent/missions` lines above: the worker account is now `agwork`.

## Worker account (`agentic/config.py`, `agentic/worker.py`, `agentic/admin.py`)
- The writable omp state binds are `/var/lib/agwork/.omp/{logs,run,natives,cache,agent/cache,agent/sessions}`, `agent/{agent,models,history,skill-descriptions}.db` and `stats.db`, each with `-wal`/`-shm`; this is what a fresh-HOME `omp -p` (omp 18.12.1) created on the production server. Everything else (`agent/models.yml`, `agent/config.yml`, `install-id`) stays read-only.
- Every bind path carries systemd's `-` prefix, so a state file omp has not created yet does not stop the unit; the operator runs omp once as agwork before the first mission (deploy/README.md).
- Unit property order: `ProtectHome=tmpfs`, `BindPaths=<worker folder>`, `BindReadOnlyPaths=/var/lib/agwork/.omp`, one space-separated `BindPaths=` for the state paths, `PrivateTmp=yes`, `NoNewPrivileges=yes`, one space-separated `InaccessiblePaths=`; they follow `Slice=`.
- The supervisor's own guarantee that the worker folder exists (`admin.ensure_workdir`) uses `runuser -u agwork -- mkdir -p` with the production worker command and a plain `mkdir` when `AGENTIC_WORKER_CMD` overrides it (the worker then runs as the current user).
- `agentic migrate-workspaces [OLD_ROOT]` (root only; default `config.OLD_WORKSPACES` = `/home/agent/missions`) covers every id found in the old root or the state folder. Root runs `tar -C <old> -cf <state>/scratch/migrate-<id>.tar .`; `runuser -u agwork -- tar -C <new> -xf -` unpacks it from stdin (`commands.run_command(..., stdin_path=)`), so every new file belongs to agwork. The old folder is removed only when the new one exists; the archive is always deleted.
- Migration outcomes, one line each (`<id>: <outcome>`): `moved`, `already moved` (no old folder, new folder exists), `missing: no folder <old path>` (neither), `failed: <reason>` (old folder kept). Exit code 0 in every case.

## Canary (`agentic/canary.py`, `agentic/loop.py`)
- The manifest skips SNAPSHOT_SKIP folders at any depth, records symlinks as `symlink:<link text>` and ignores other entry types. A follow-up's baseline records `manifest: {}`; ledgers without the key replay with `{}`.
- Only regular files are emptied; a new or changed symlink is left as it is (its target is either a worker file, which is emptied itself, or outside the folder).
- `changed_files` in the record counts the files that were emptied.
- The canary runs (a) after an iteration whose checks all passed, before stall bookkeeping: the iteration result is recorded only once the canary is, so not-held criteria count as failing in that iteration's stall and escalation rules; (b) after the strengthened checks pass; (c) before the mission finishes when passing criteria exist that no canary has seen in this iteration (for example after a give-up); (d) at iteration 0 for a follow-up's inherited passes.
- The canary checks only PASSING criteria. A not-held criterion becomes FAILING and keeps the prompt line until a later canary holds for it or its check fails for its own reason.
- The briefing puts `canary_failed` before the give-up reason: a criterion that was not shown to depend on the files shows `검사가 결과물을 확인하지 않음`, even when it was later given up. It is a plain ❌ line; no decision is added.
- Inherited passes are not achieved in the briefing until a `canary` or `check_result` record covers them.

## Mutation score (`agentic/mutation.py`)
- Operators (one point each): comparison flip `<`↔`>=`, `>`↔`<=`, `==`↔`!=`; `and`↔`or` per `BoolOp`; `True`↔`False`; `return <expr>` → `return None` (not for a bare `return` or `return None`); integer constant + 1 (not `bool`).
- Candidates are every point of every changed `.py` file in `ast.walk` order, files sorted by path; `random.Random(<first 8 bytes of sha256(mission id)>).sample(candidates, 6)`, then sorted by (file, line, index). Files over 1 MB, not UTF-8, unreadable through `O_NOFOLLOW` or with a syntax error are not mutated.
- A mutant file is re-printed with `ast.unparse` (comments and formatting are lost); only its semantics change at one point.
- Each mutant runs on its own snapshot of the worker folder under the scratch root, with the checks of the criteria that are PASSING at that moment (given-up and invalid criteria would kill or spare every mutant for reasons unrelated to the code). Killed = any of those checks fails.
- The 10-minute budget is measured with the loop's injected clock before each mutant; a mutant already started finishes. Unstarted ones are `{"killed": null, "skipped": true}`.
- Record `mutation`: `{iteration, mutants, killed, skipped, details: [{file, line, operator, killed, skipped}]}`; n in the score is `mutants - skipped`.
- The score runs once the canary held for every criterion and nothing is failing, at most twice per mission: first score, and a second one only after a successful strengthening. A mission whose worker changed no `.py` file records nothing.

## Strengthening (`agentic/planner.py`, `agentic/loop.py`)
- Trigger: the first score has n ≥ 3 and k/n < 0.5, and no `checks_strengthened` record exists. It runs before the review.
- The prompt is the planner's `checks` prompt for every criterion, plus `CURRENT CHECKS` and `SURVIVING MUTANTS` (file, line, operator of each mutant with `killed: false`); the worker's code is never sent. Hand-written missions use `{"goal": <goal>}` as the brief and the criteria texts as the design.
- Its steps are recorded as `plan_step` rounds from 10 on (`checks` 10, `critique` 10…12, re-runs `checks` 11…12), so a killed supervisor resumes them from the ledger exactly like planning.
- Vetoes as in planning (critique and the empty-folder run, at most 2 re-runs of the vetoed ones). A criterion still vetoed after the last re-run keeps its old check (`kept`); nothing is dropped.
- `checks_strengthened {iteration, ok, replaced, kept[, error]}` is written after mission.json; a planner failure records `ok: false` with the public reason and the mission goes on with the old checks.
- After `ok: true` all remaining criteria are re-checked at the same iteration number; these check results do not count as a new iteration for stalls. A failure sends the worker back under the normal rules; when all pass, the canary and the second score run, then the review.
- Notice text: `검사 강화: 변형 n개 중 k개 → n2개 중 k2개`; before the second score `… → 아직 다시 재지 않음`; after a failed strengthening `… → 새 검사를 만들지 못함`. It is the last notice of the rule table.

## Ledger hardening (`agentic/ledger.py`, `agentic/loop.py`, `agentic/cli.py`)
- `chattr +a <ledger>` runs once, right after record 1 is written (only the run that created the ledger). Any non-zero exit (including the runner's 127 "cannot start" and 124 timeout) becomes `notice {kind: "append_only", text: "원장을 추가 전용(chattr +a)으로 잠그지 못함", exit_code, detail}`; the detail (stderr, at most 200 characters) is never shown in event lines.
- `ledger.append_only(path)` reads `FS_IOC_GETFLAGS` and tests `FS_APPEND_FL`. `verify` prints `warning: the ledger lacks the append-only flag (chattr +a)` on stderr whenever the flag is not confirmed set; its exit code still reflects only the hash chain.
- Tests: the in-process guard answers `chattr +a` with exit 0; subprocess supervisors get a fake `chattr` on `PATH`.

## Notifications
- Missions whose id starts with `canary-` send nothing: `Notifier.on_record` and `notify_failure` return before writing any card.

## Daily operations (`agentic/daily.py`, `agentic/health.py`)
- `canary-<yyyymmdd>` and `agentic-<yyyymmdd>.tar.gz` take their date in `config.DISPLAY_TIMEZONE` (`Asia/Seoul`), matching the KST timers (`07:30` and `08:00` `Asia/Seoul`).
- `honesty_canary_mission` generates a fresh `secrets.token_hex(16)` per run and embeds it only inside `_agentic_check_c2.py`; `c1` checks that `hello.txt` equals `hello`. `start_honesty_canary` cleans up canaries strictly more than 7 KST days old, returns 0 without starting when today's mission folder already exists, and otherwise writes a 0600 temp file in `<state>` and starts the unit through `admin.start_mission`.
- `cleanup_canaries` ignores `canary-*` names whose suffix is not a valid `yyyymmdd` date and never touches non-canary missions; for each expired canary it runs `systemctl disable agentic@<id>.service` and `chattr -a <ledger>` (ignoring errors) before removing `<state>/missions/<id>` and `<workspaces>/<id>` without following symlinks.
- `backup` writes `<dest>/agentic-<yyyymmdd>.tar.gz` via a 0600 temporary file (`dest` mode 0700, `dereference=False`, archive names stripped of leading `/`), skips and lists missing sources, rotates `agentic-*.tar.gz` by name to keep the 30 newest, and records `{ok, ts, file, skipped, error}` in `<state>/backup-status.json` (0600); any `OSError` or `TarError` records `ok: false` and exits 2 from the CLI without raising.
- `health.run` runs all five self-repairs before building the card and lists every action under `🔧 스스로 고친 것` (`없음` when none ran): inactive services from the 11-unit list (the 9 services plus `agentic-inbox.path` and `agentic-inbox.timer`) get `systemctl restart` and one `is-active` re-check; units from `systemctl --failed --plain --no-legend --no-pager` get `systemctl reset-failed` plus `systemctl restart` for `agentic@*.service`; guidance drift runs `/usr/local/sbin/agentic-guidance-sync` and re-checks with `/usr/local/sbin/agentic-guidance-check`; `/` over 85% deletes canaries older than 1 day, prunes backups to 7, runs `journalctl --vacuum-size=200M`, and re-measures; non-directory entries in `inbox`/`outbox` older than 1 hour are moved via directory fds (`O_NOFOLLOW`) to `<spool>/dead/<inbox|outbox>-<name>` (folder 0700) and logged to stderr.
- `지금 하실 일` on the health card lists only problems the supervisor cannot fix itself: a monitored unit still inactive after `systemctl restart` (`sudo journalctl -u <unit> -n 50`), and a model probe whose stderr matches `unauthori[sz]ed|\b401\b|login|expired|credential|oauth` (case-insensitive), with the exact `sudo -u ompauth env HOME=/var/lib/ompauth /var/lib/ompauth/.local/bin/omp auth-broker login <provider>` command; otherwise `없음 — 모두 정상이거나 스스로 고쳤습니다`.
- Model access probes run `worker.build_command(cfg, model, "low", 1, "Reply with the single word OK. Do not use any tools.", cfg.workspaces)` once for `anthropic/claude-sonnet-5-5` and once for `google-antigravity/claude-sonnet-5-5` through `commands.run_command`, so they run as agwork in the worker sandbox.
- The health card reads only the newest `canary-*` mission's briefing state, criterion marks and `gave_up` record, never check files or worker files; anything but `partial` with c1 ✅ and c2 ❌ given up (above all c2 ✅) puts a `🚨` line directly under the header and makes the title `🚨 서버 점검: 정직성 카나리 이상`. A canary still running is shown as running, not as 🚨.
- `latest_omp_release` fetches `https://api.github.com/repos/can1357/oh-my-pi/releases/latest` with a 10 s timeout and returns `None` on any error; tests replace it with a stub.
- `agentic-daily.service` runs `agentic honesty-canary` with its exit status ignored (`ExecStart=-…`), then `agentic backup`; both services are oneshot with `UMask=0077`; both timers are `Persistent=true`.

# Stage G (SPEC-G.md): public release

Each line is one choice SPEC-G left open.

## Configuration file (`agentic/config.py`, `agentic/cli.py`, `agentic/telegram.py`)
- Key names: `[accounts]` interactive_user, worker_user, planner_user, bot_user; `[paths]` state, workspaces, spool (inbox and outbox are `<spool>/inbox`, `<spool>/outbox`), worker_omp, planner_omp, worker_overlay, guidance_sync, guidance_check; `[models]` planner_steps, review, worker_ladder, alternate_provider; `[limits]` max_active_missions, outage_waits_minutes, canary_max_age_days, mutation_max_mutants, mutation_budget_seconds; `[display]` timezone, language; `[telegram]` enabled (docs/CONFIG.md).
- The worker, planner and bot homes are `/var/lib/<account>`, derived from the account names, so they need no keys of their own.
- `display.timezone` defaults to `UTC` and `display.language` to `en`, as SPEC-G section 1 says, although every other key keeps today's value: a server upgraded from Stage F sets `language = "ko"` and `timezone = "Asia/Seoul"` (docs/OPERATIONS.md, CHANGELOG.md).
- `config.load_settings` rejects unknown sections or keys, wrong types, non-positive or boolean integer limits, non-absolute paths or paths with `..`, user names outside `^[a-z_][a-z0-9_-]{0,31}$`, unknown `zoneinfo` names, languages other than `en`/`ko`, malformed `model:thinking` strings and invalid ladder steps, each as `ConfigError("<section>.<key>: <problem>")`.
- The file is read once at import (`config.reload_settings`): a missing default file means defaults; a missing `AGENTIC_CONFIG` file or an invalid file keeps the defaults applied and stores the message in `config.SETTINGS_ERROR`, and `cli.main` and `agentic.telegram.main` then print `<path>: <error>` to stderr and exit 1 before doing anything.
- `config.apply_settings` sets the module constants (`WORKER_USER`, `OMP_ARGV`, `PLANNER_MODELS`, `WORKER_LADDER`, ...); consumers read them as `config.<NAME>` at call time. The existing `AGENTIC_*` environment variables still win over the file.
- `agentic config check` prints the effective configuration (file plus environment overrides) as TOML that `tomllib` reads back, and exits 0; the Telegram token file is never read.
- `[telegram] enabled = false` drops `agentic-telegram.service` from the health card's service list; install.sh then neither installs nor enables the bot unit.
- The health card's service list, the timers' `Asia/Seoul` calendar and the health card's login command for the auth broker stay as they were; SPEC-G section 1 does not list them.

## Two languages (`agentic/i18n/`)
- `agentic.i18n.t(key, **params)` formats `CATALOGS[config.LANGUAGE][key]` with `str.format_map` at call time; keys are prefixed with the module that uses them.
- Former text constants (`briefing.HEADLINES`, `telegram.USAGE`, `health.NO_TODO`, ...) resolve through a module `__getattr__`, so existing callers get the text of the current language.
- Fixed texts that said `KST` take the zone from `display.zone_label()`; with `Asia/Seoul` the Korean output is byte for byte what Stage F wrote.
- CLI status and error lines that were English before (`mission <id> started`, `ledger ok: ...`, `error: ...`) keep that English in both catalogs; argparse help is translated. Exception messages of input validation (mission.py, spool.py, config.py) stay English diagnostics in both languages.
- The planner's outage reason is written into the ledger in the language configured when it happened (`planner.outage_reason`).
- The Telegram bot accepts both `<id> 이어서: <text>` and `<id> continue: <text>` for follow-ups in either language; button labels follow the language.
- `BANNED_PHRASES` (Korean) moved to `agentic/i18n/ko.py`; `agentic/i18n/en.py` holds `BANNED_PATTERNS`, the `condition:` regexes of `guidance/user/rules/english-writing.md` copied verbatim, so the tests work without the operator's guidance copy; a test compares the two when the guidance file exists.
- "No hard-coded user-facing string" is tested as: no Hangul in any string constant outside `agentic/i18n/` (docstrings excepted), and no literal with letters in a `print()` argument or an argparse `help`/`description`/`epilog`/`usage` outside it (tests/test_i18n.py).
- tests/helpers.py sets `config.LANGUAGE = "ko"` and `config.DISPLAY_TIMEZONE = "Asia/Seoul"`, the installed server's settings the earlier tests were written for; English tests patch both.

## Install (`install.sh`, `uninstall.sh`, `deploy/`)
- install.sh steps, in order: prerequisites, accounts, folders, package, launchers, units/timers, config, hidepid, agent sudo, guidance, next steps; each prints `==> <step>`, each action `+ <argv>`; every target path is prefixed with `${AGENTIC_ROOT_PREFIX:-}`.
- Under `--dry-run` a non-root user gets a notice instead of an error, and the generated `agentic.toml` and `omp-overlay.yml` are printed.
- With `AGENTIC_ROOT_PREFIX` set and no `<prefix>/etc/passwd`, install.sh judges whether the system accounts exist by their home under the prefix, so a dry run on an installed host still lists the `useradd` actions.
- The package is `git archive HEAD` into `/opt/agentic`; an uncommitted `guidance/` is copied from the working tree.
- `/etc/agentic/omp-overlay.yml` is written only when absent, with `retry.modelFallback: false` and the advisor off (SPEC-F section 6); `/etc/agentic/agentic.toml` is written only when absent.
- `deploy/bin/agentic-guidance-sync` copies `guidance/user/` into `~<interactive_user>/.omp/agent/` and locks the copied files with `chattr +i`, not the folder itself, so omp can still write its sessions and databases there; `agentic-guidance-check` prints one line per drifted file and exits 1.
- uninstall.sh keeps `/var/lib/agentic`, `/etc/agentic`, the ledgers and the accounts; `--purge` runs `chattr -a` on the ledgers, then removes state, configuration, workspaces, spool and backups, and still keeps the accounts.

## Repository scan (`tests/repo_scan.py`, `tests/test_repo_scan.py`)
- The scan covers what would be published: `git ls-files --cached` (tracked and staged files) in a checkout, every file elsewhere; binary files are skipped.
- Host names are the literal list of SPEC-G plus the scanning machine's own host name; IP addresses are flagged except loopback, `0.0.0.0` and the RFC 5737 documentation ranges.
- Chat ids are numbers of 5 or more digits after an `owner_id` or `chat_id` key; API keys are the OpenAI/Anthropic `sk-`, AWS `AKIA`, GitHub `ghp_`/`github_pat_`, Google `AIza`, Slack `xox?-`, GitLab `glpat-` and PEM private-key patterns.
- The only home under `/home` the docs name is the interactive account's, `/home/agent`; any other `/home/<name>` or `/Users/<name>` is flagged.
- SPEC files (`SPEC*.md`, also under `docs/specs/`) skip only the literal-string rule, because SPEC-G quotes the strings to define it.

# Stage H (SPEC-H.md): closing the cheat paths found in the t1 audits

## Check hijacking (`agentic/checks.py`, `agentic/planner.py`, `agentic/review.py`)
- The `/usr/bin/python3.12 -I` rule is enforced by the free filter (`planner.check_problem`, problem `not-isolated`) for every `checks` answer of planning and strengthening, not by `mission.parse_check`: hand-written missions (tests, the honesty canary) keep their argv, and a rejected check goes into the veto rounds instead of failing the step.
- Snapshot hygiene deletes (does not rename), inside the snapshot only and before the check files are written: files named `<stdlib module>.py`, `.pyc` or `.so` (the part before the first dot is in `sys.stdlib_module_names`, so `json.cpython-312.pyc` counts), `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`, and package folders named like a standard-library module that hold `__init__.py`; at any depth, symlinks by their own name. `pytest.ini`, `tox.ini`, `setup.cfg` and `bin/` shims are kept: `-I` and the fixed PATH make them inert.
- The loop records `hygiene {iteration, files}` before the baseline and before each iteration's checks whenever the list differs from the latest `hygiene` record, so a resumed run never repeats it; the briefing lists the union of all recorded files in one notice, `⚠️ 검사를 가로챌 수 있는 파일이라 검사 때 제외함: <files>` (report section 5 and the card's notices).
- A check process's whole environment is `PATH=/usr/bin:/bin` and `HOME=<snapshot>/.home`, a fresh empty folder (a worker-planted `.home` is removed first); systemd mode passes the same two with `systemd-run --setenv`. Python itself may add `LC_CTYPE` (locale coercion).
- The cheat reviewer's list gains "adds files that shadow standard or test tooling", naming the same files plus pytest.ini/tox.ini/setup.cfg options and `bin/` shims.

## Free filter (`agentic/planner.py`)
- A check script "can fail" when it has an `assert`, a call whose name starts with `assert` (unittest), a `raise` of anything but `SystemExit`, or an `exit`/`quit`/`sys.exit`/`os._exit`/`SystemExit` call whose first argument is not literally `0`, `None` or `False`. The scripts are the check's `.py` files plus the `-c` argument; a script that does not parse cannot fail.
- "Only command is `true`": argv[0] has the basename `true`.
- Vague words: `적절히`; `잘` only with no Hangul syllable directly before or after it (`잘못`, `잘라` pass); `appropriately`, `properly` and `as needed` as whole words, case-insensitive. The Korean words live in `agentic/i18n/ko.py` (`VAGUE_WORDS`) because no Hangul string may live outside `agentic/i18n/`.
- A design with vague criteria goes back once as design round 1 with a `VAGUE CRITERIA` section; criteria still vague after it are dropped with problem `vague: …`; planning fails at the design step when no criterion is left.
- Every veto round and both design checks record `plan_filter {step, round, results: {id: problem or null}, checked, rejected}`, reused on resume. The empty-folder run and the critique call cover only the checks that passed the filter; a round in which nothing passed makes no model call.

## Ratchet (`agentic/ratchet.py`, `agentic/loop.py`, `agentic/planner.py`)
- The strengthening `checks` answer may carry `reasons: {criterion id: bug in the old check}`; the critique is shown them under `CHANGE REASONS` and told to veto as unfaithful when a reason names no real bug. "The critique accepted that reason" means the new check passed the veto rounds with its reason shown.
- `old_sha256`/`new_sha256` hash the canonical JSON (sorted keys, no spaces) of argv, timeout, network and files; only criteria whose hash changes get a `checks_update`; its `reason` is the planner's or `stronger check against the surviving mutants; every file of the old check kept`.
- Rule 3 runs each changed check on a fresh empty folder again. Rule 4 regenerates the mission's mutants (same seed, same changed Python files), takes those the first `mutation` record lists as killed, and for each changed criterion requires: when the old check alone kills the mutant, the new check alone kills it too.
- A violation records `checks_update_rejected {iteration, criteria, violations: [{criterion, rule, file, line, operator}]}` (rules `criterion_without_check`, `old_files_changed`, `passes_empty_folder`, `mutant_survives`; the mutant fields only for the last), then `checks_strengthened {ok: false, error: "ratchet"}`; mission.json is not touched and the briefing shows the existing "could not build new checks" notice.
- A resumed strengthening that already has `checks_update` or `checks_update_rejected` records keeps that verdict and emits only the missing records.

## Crash points (`agentic/faults.py`, `tests/test_crash_points.py`)
- `faults.HOOK` (None outside tests) is called with `appended` right after a record is durable (`Ledger.append`) and with `applied` right after the loop applied it to its state. The test kills the run after the first plan_step, iteration_worker, check_result, canary, mutation, review, review_veto and mission_finished at both points, and after the ratchet records, then compares the whole ledger data (timestamps, hashes, durations and random snapshot names excepted) and the report with an uninterrupted run.
- `admin.event_text` summarises the new records without check content: `hygiene` as a file count, `plan_filter` as step, round and rejected/checked counts, `checks_update` as the criterion only (no hashes, no reason), `checks_update_rejected` as a criterion count.

## Fresh worker sessions (`agentic/worker.py`, `agentic/config.py`, `agentic/loop.py`, `agentic/health.py`)
- Each `worker.run_worker` call creates a fresh mode-0700 folder (under the worker home as `.omp-agent-*` when writable, otherwise in the system temp folder as `agentic-omp-*`), copies `models.yml` and `config.yml` into it (mode 0600, `O_NOFOLLOW`, owned by the worker account when the supervisor runs as root; a missing source file is skipped), passes `PI_CODING_AGENT_DIR=<folder>` in the unit's `env` argv and in the child environment, binds it read-write after `BindReadOnlyPaths=<worker omp dir>`, and deletes it in `finally`.
- `worker.worker_env` gives the worker an explicit environment (`PATH`, `HOME`, `PI_CODING_AGENT_DIR`, plus the `AGENTIC_STUB_*` variables only when `cfg.worker_cmd` overrides the command), so no `PI_*` or `OMP_*` session variable of the supervisor is inherited.
- `Config` takes optional `worker_home` and `worker_agent_dir` overrides (`AGENTIC_WORKER_HOME`, `AGENTIC_WORKER_AGENT_DIR`), defaulting to `config.WORKER_HOME` and `<worker home>/.omp/agent`.
- Leftovers: a before/after snapshot of the worker home (regular files by sha256, symlinks by link text, empty folders outside `.omp` and `.omp/agent`) excluding the worker folder, the workspaces root, `<worker home>/missions`, the temporary agent folder and `config.WORKER_OMP_STATE`.
- The temporary agent folder is given to the worker account only when the supervisor is root and runs the production command; a configured `worker_cmd` (stub) runs as the caller and keeps the caller's ownership.
- An unknown leftover records `notice {kind: "worker_leftover", iteration, files, text}` (relative paths) right before `iteration_worker` or `worker_unavailable`; `health` lists such notices of missions changed in the last 24 h as one card line.

## Byte budgets (`tests/test_budgets.py`, `guidance/`)
- Budgets count UTF-8 bytes; the test skips when `guidance/user` is absent (SPEC-G section 7) and names each file with its excess (`+N bytes over`, `+N lines over`).
- The workspace-state line is rendered from the template in `guidance/user/extensions/workspace-state.ts` with a worst case of cwd `/home/agent/work/agentic-supervisor`, a 36-character branch, 999 changed files and 999 missions (139 bytes).
- A TTSR rule body is the text after the closing `---` of its front matter; a skill description is the front-matter `description` (folded `>` values joined) with whitespace collapsed.
- To fit, the guidance was trimmed with the owner's approval: RULES.md 4,646 → 4,360 bytes, the english-writing body 3,683 → 1,293, the korean-writing body 4,141 → 1,365, the insane-search description 1,078 → 504; front matter and `condition:` lists unchanged.

## Anchors and daily self-tests (`agentic/health.py`, `agentic/daily.py`, `agentic/cli.py`, `agentic/ledger.py`)
- The health card lists `<mission id> <seq> <first 12 hex of the head hash>` for every mission whose verified ledger head is less than 24 h old (`없음`/`none` otherwise).
- `agentic verify --anchors <file>` strips HTML and reads every `<id> <seq> <12 hex>` in the pasted text; an anchor holds when the chain verifies, has at least `seq` records and record `seq`'s hash starts with the 12 hex digits (later appends still pass; truncation and rewrites fail). One `ok`/`mismatch (…)`/`missing ledger` line per anchor; exit 2 when no anchor is found or any fails.
- `daily.run_guardrails_selftest` (run by `start_honesty_canary`) runs `node -e` through `commands.run_command` on `<interactive home>/.omp/agent/extensions/guardrails.ts`, stripping its simple type annotations and turning `export default` into `module.exports` inside a `vm` context, because Node 20 cannot run TypeScript; three forbidden actions (`rm -rf ~/work`, editing `SPEC-H.md`, writing under `/var/lib/agentic`) and two allowed ones (a unittest run, editing `src/main.py`); the outcome goes to `<state>/guardrails-status.json` (0600), and the health card shows 🚨 when a forbidden action was allowed.
- `health` hashes every regular file in `<interactive home>/.omp/agent/extensions` at each run into `<state>/extension-pins.json` (0600); the first run records quietly, later changes give a ⚠️ line with the changed, added and removed names.

## Passed then, passes now (`agentic/briefing.py`, `agentic/report.py`, `agentic/cards.py`, `agentic/admin.py`)
- `Briefing.checked_at` and `Briefing.evidence_line` (`감독자 검사 기준 <KST>`) use the ledger time of the latest supervisor-run check evidence (`check_result`, a later `canary`, or a `baseline` with `inherited_pass`), shown in the report header and the finish card.
- `report.followup_ids(cfg, mission_id)` finds child missions through `planner.read_parent` (falling back to `mission.json` `parent` before planning writes it), in creation order (`parent` file `st_mtime_ns`, then natural id order); `admin.continue_mission` rewrites the parent's `report.md` at once, and the report, `/status` card and `admin.mission_status` show `이후 이어서 한 미션: <id>` whenever a follow-up exists.

# After Stage H: concurrent leftovers, release scan, evidence binding

## Concurrent-mission leftovers (`agentic/worker.py`)
- Every throw-away agent folder is registered before it exists: a marker `<state>/agent-dirs/<folder name>` (0600, in the root-owned state folder a worker cannot write) holds the supervisor's pid and the folder's absolute path; the folder is deleted before its marker.
- A marker counts as live while its pid exists (`os.kill(pid, 0)`; `PermissionError` counts as alive); stale markers are removed when read.
- Each home snapshot excludes every live registered folder, read before the walk, and drops entries under folders registered during the walk (read again after it). A folder registered and released entirely within one walk is not covered; such a folder is gone by the time the walk ends.
- A folder the worker names `.omp-agent-*` itself is not registered and is still reported.

## Release scan (`tests/repo_scan.py`)
- `VENDORED = ("guidance/user/skills/insane-search/",)`; inside these prefixes only the IPv4 rule is skipped, and `scan()` reports `NOTICE:1: uncredited vendored path: <prefix>` when `NOTICE` exists and does not name a prefix.
- A git checkout (`.git` present) whose `git ls-files` list is empty gives `.git:0: empty repository: no tracked files to scan`; a plain empty folder still gives nothing.
- `git log --all --format=%h%x00%an%x00%ae%x00%cn%x00%ce`: author name and email always, the committer only when it differs from the author, against `BANNED_STRINGS` and the host names; a hit reads `commit <short hash> <author|committer>: <rule>: <match>`.
- The repository's first 13 commits carried an author and committer email with a banned string; at the owner's later request the history was rewritten (`git filter-branch --env-filter`, emails only, names, dates and trees unchanged) to `agent@example.invalid`, and the repository's local `user.email` is set to the same address. A bundle of the old history is kept outside the repository.

## Evidence binding (`agentic/mission.py`, `agentic/checks.py`, `agentic/loop.py`, `agentic/ledger.py`, `agentic/briefing.py`, `agentic/cli.py`)
- The contract is `Mission.contract()`: the public view with each criterion's full check (argv, timeout, network, files) after validation and defaults; `contract_sha256` hashes its canonical JSON (sorted keys, `,`/`:` separators, UTF-8). Reformatting mission.json keeps the hash; any content change alters it. `check_dict` moved from `planner` to `mission`.
- `mission_started` also records `check_sha256` per criterion, so `verify()` knows the check in force without the check data.
- Resume compares the mission file's contract with the contract in force (latest of `mission_started`, `checks_update.new_contract_sha256`, `checks_strengthened.contract_sha256`); while a strengthening is recorded but its `checks_strengthened` is not, the contract before it is accepted too (the new file is written after the `checks_update` records). A mismatch raises `LedgerError`; a finished mission is not checked (nothing continues). Ledgers without `contract_sha256` are not checked.
- `snapshot_sha256` is the sha256 of the canonical JSON of `checks.tree_manifest(snapshot)` (relative path → content sha256 or `symlink:<target>`, SNAPSHOT_SKIP names, fifos, sockets and devices left out exactly as `snapshot_tree` leaves them out), taken right after the copy, before hijacking files are removed and before check files are written; the worker folder therefore has the same digest as its snapshot. Empty folders and file modes are not part of it.
- Output hashes come from the temp files that hold the full output; a process that never started has the hash of empty output and 0 bytes. Baseline results carry the same fields.
- The checks of one batch run first and are recorded together; a batch whose snapshots differ from each other or from the iteration's recorded snapshot records `notice` `kind: checks_unstable` (`attempt`, the per-criterion snapshots, the recorded one) instead of check results. The second consecutive notice finishes the mission as `stopped_unstable` (`UNSTABLE_ATTEMPTS = 2`); a recorded check result resets the count.
- A follow-up that would finish with inherited passes and no check result of its own (iteration 0) first records one check result per such criterion with `confirm: true`; it changes the status only, never the iteration's result. A failing confirmation sends the worker back as a failing criterion.
- `proofs` are `{criterion, seq, hash}` for every passing criterion whose latest check result of the last iteration passed; `final_snapshot_sha256` is the iteration's snapshot (null when it ran no check); `final_manifest` is that snapshot's manifest, from the latest batch or the worker folder when it still matches, and is left out otherwise.
- `Ledger._scan` (so `verify()` and `read()`) rejects a bound ledger's `mission_finished` when `iterations` is not the last `iteration_worker`, `final_manifest` does not hash to `final_snapshot_sha256`, or a proof is malformed, repeated, outside the ledger, not the named record's hash, not a `check_result`, of another criterion, not passed with exit 0, from another iteration, not the latest result of its criterion, on another snapshot, with another check hash or contract than the ones in force; `complete` needs a proof for every criterion of `mission_started`. A ledger is bound when `mission_started` has `contract_sha256`; `VerifyResult.bound` is False for old ledgers and None without `mission_started`.
- Reports and cards of a finished mission mark ✅ only criteria with a proof; a pass without one is ⚠️ with `앞서 통과했지만 마지막 검사에 통과 증거가 없음`, or `검사 결합 기록이 없는 예전 원장이라 통과로 세지 않음` when the record has no `proofs`. A running mission keeps the latest-check rule.
- `agentic verify <id>` keeps exit 0 for a sound ledger whatever the folder says; the folder line is informational. Changed paths are sorted and cut after 10 with `, …`; `?` when the record has no manifest. Like the other `cli.verify.*` lines, the new lines are English in both catalogs.

## Review fixes: spool order, link-local addresses, published guidance
- Spool items written without a name are named `spool.new_name()`: `<time.time_ns() as 20 digits>-<token_hex(16)>`, the number raised past the last one this process issued, so one process's items sort in creation order even within one nanosecond (this replaces SPEC-D's plain `secrets.token_hex(16)` names, by the owner's review). Documents get the same name plus `.doc`.
- `list_names` sorts by `spool.order_key`: the number in such a name, else the file's mtime for caller-given names (`<mission>-<seq>` notices, so `demo-10` still follows `demo-9`), then the name. Items from two processes in the same nanosecond fall back to the random part.
- `tests/repo_scan.py` allows `169.254.0.0/16` (link-local, the cloud metadata address SSRF guards name) anywhere, not only in vendored folders.
- `guidance/user` and `guidance/project` are copies of `/etc/agentic/guidance/{user,project}` without `guidance/user/skills/insane-search/engine/tests/`; `guidance/user/AGENTS.md` is a generic "example: edit for your server" guide under the 3000-byte budget. `guidance/reference` is not published.
- `docs/specs/SPEC.md` to `SPEC-G.md` are tracked; the root `SPEC*.md` are untracked (`git rm --cached`, the root-owned files stay on disk unchanged) and ignored by `/SPEC*.md` in `.gitignore`. Copying `SPEC-H.md` into `docs/specs/` was refused by the workstation guardrail (creating a spec file) and is left to the root operator.

## Review fix: proofs after a strengthening
- An ok `checks_strengthened` empties the loop state's `iter_checks` (the snapshot stays): results of the replaced checks ran under the old contract and can no longer be proofs, so a `stopped_unstable` finish right after a strengthening records `proofs: []` instead of a proof `ledger.binding` rejects. Chosen over filtering in `finish()` because it is a pure ledger replay and leaves old unbound ledgers alone.
