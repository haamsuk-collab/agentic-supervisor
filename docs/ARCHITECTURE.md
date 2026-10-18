# Architecture

`agentic-supervisor` is a Python 3.12 package (`agentic/`) with zero third-party dependencies that orchestrates autonomous coding missions from plain-language requests or hand-written mission files. All persistent mission state lives in an append-only JSON Lines ledger (`<state>/missions/<id>/ledger.jsonl`); if a supervisor process crashes or the server reboots, `agentic run <id>` replays the ledger and resumes from the exact unfinished step.

---

## 1. Package Modules (`agentic/`)

| Module | Role |
|---|---|
| `agentic/__init__.py`, `agentic/__main__.py` | Package marker and `python3.12 -m agentic` entry point calling `cli.main()`. |
| `agentic/config.py` | Loads and validates `/etc/agentic/agentic.toml` (`AGENTIC_CONFIG`) with `tomllib`, applies `AGENTIC_*` environment overrides, and exposes `Settings`, `Config`, and `agentic config check` formatting. |
| `agentic/i18n/__init__.py`, `en.py`, `ko.py` | Message catalogs for English (`en`, default) and Korean (`ko`), exposed through `t(key, **params)`. Both catalogs define the exact same keys and placeholders. |
| `agentic/display.py` | Converts timezone-aware UTC timestamps into `config.DISPLAY_TIMEZONE` (`UTC` by default; `Asia/Seoul` when configured for KST) as `YYYY-MM-DD HH:MM:SS TZ`. |
| `agentic/mission.py` | Parses and validates `mission.json` (`Mission`, `Criterion`, `Check`), mission IDs (`^[a-z0-9][a-z0-9-]{0,40}$`), and `request.md` payloads (UTF-8, non-empty, no NUL, at most 20,480 bytes). |
| `agentic/ledger.py` | Append-only SHA-256 hash-chained ledger (`Ledger`), `verify()` chain checker, `fsync` durability, and `FS_IOC_GETFLAGS` (`chattr +a`) inspection. |
| `agentic/checks.py` | Copies the worker directory into `<state>/scratch/<id>/<iter>/<cid>-<hex>/` (skipping `.git`, `node_modules`, `__pycache__`, `.venv`, fifos, devices, and sockets), writes check files with `O_NOFOLLOW`, and runs checks in `systemd` (`DynamicUser=yes`) or `local` mode, killing the process group on exit or timeout. |
| `agentic/canary.py` | Computes SHA-256 baseline manifests of the worker folder and re-runs passing checks on a copy where every worker-created or modified regular file is truncated to 0 bytes. |
| `agentic/mutation.py` | Generates up to 6 deterministic single-point AST mutants across changed `.py` files (seeded from `sha256(mission_id)[:8]`) and measures how many mutants are killed by passing checks within a 600-second budget. |
| `agentic/worker.py` | Builds the memoryless worker prompt from ledger state only (never check files or argv) and runs `/opt/omp/omp` as `agwork` inside a transient `systemd-run` unit with `KillMode=control-group` and filesystem sandboxing. |
| `agentic/llm.py` | Executes single-shot, toolless planner and reviewer model calls (`/opt/omp/omp --no-tools`) as `agplan` with the prompt on `stdin` (up to 120,000 bytes) and extracts the final ```` ```json ```` object from `stdout` (up to 1 MiB). |
| `agentic/planner.py` | Runs the four-step automatic planning pipeline (`brief` → `design` → `checks` → `critique`), mechanical empty-folder check vetoes, up to 2 check rewrite rounds, and mutation-triggered check strengthening (rounds 10–12). |
| `agentic/review.py` | Collects up to 300 KB of UTF-8 regular files from the worker folder (`O_NOFOLLOW`, max 100 KB per file), calls a reviewer model from another vendor, mechanically verifies each quoted shortcut (20–300 chars verbatim), and filters check leaks (16-char substring match). |
| `agentic/outage.py` | Detects provider rate limits, quota errors, HTTP `401`/`429`/`503`/`529`, gateway failures, and fast exits (`< 120s` with unchanged workspace digest), computing backoff waits (`5, 10, 20, 40, 60` minutes). |
| `agentic/loop.py` | Rebuilds `LoopState` by folding over ledger records, acquires the non-blocking mission `flock`, drives planning, baseline, worker iterations, escalation, oscillation detection, canary, mutation score, check strengthening, cheat review, and notifications. |
| `agentic/briefing.py` | Pure, deterministic reducer from ledger records + public mission view + `DECISIONS.md` lines to a `Briefing` object (headline, achieved/total, todo list, structured `Decision` options with recommendations, copyable `NextInstruction` commands, notices, and isolated `AiNotes`). |
| `agentic/report.py` | Renders `report.md` from `Briefing` with strict Markdown backslash escaping (`\`, `` ` ``, `*`, `_`, `[`, `]`, `<`, `>`, `#`, `|`) and `> ` blockquotes for unverified AI notes. |
| `agentic/cards.py` | Renders Telegram HTML cards (`parse_mode: "HTML"`) from `Briefing`, escaping all dynamic text through `cards.esc` and packing blocks into messages of at most 4,000 characters without splitting inside HTML tags or entities. |
| `agentic/spool.py` | Atomic spool file operations (`<name>.tmp` created with `O_CREAT|O_EXCL|O_NOFOLLOW` mode `0600`, `fsync`, optional `fchown` to `agbot`, `rename`) and `.doc` document files up to 20 MiB (`20,971,520` bytes). |
| `agentic/outbox.py` | Root-side helper writing packed HTML card lists and `.doc` documents + metadata items into `/var/spool/agentic/outbox`. |
| `agentic/notify.py` | Hooks into `loop.py`'s record emitter to write one outbox notification per event (`<id>-<seq>`, `O_EXCL`): plan completion, model escalation, worker outage `>= 30` minutes, review veto, mission completion (+ `report-<id>.md`), and unit crash (`notify-failure`). Suppresses notifications for `canary-*` missions. |
| `agentic/results.py` | Walks the untrusted worker directory using directory file descriptors (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, max depth 64) and `fstat`, verifies file ownership (`agwork`), and streams `report-<id>.md` plus worker files into a deflated zip buffer capped at 20 MiB. |
| `agentic/commands.py` | Single wrapper (`run_command`) around `subprocess.run` (120-second timeout, never `shell=True`) for `systemctl`, `runuser`, `chattr`, `tar`, `journalctl`, and `aide`. Replaced with a fake in tests. |
| `agentic/admin.py` | Root administration logic: `new_mission`, `start_mission`, `continue_mission` (follow-ups `<id>-c1`, `-c2`, …), `stop_mission`, `list_missions`, `status_text`, and `migrate_workspaces`. Enforces `max_active_missions` (default 3). |
| `agentic/inbox.py` | Root inbox processor (`agentic inbox`) serialized under `/run/agentic-inbox.lock`. Validates `agbot`-owned regular files (`<= 32 KB`) in `/var/spool/agentic/inbox`, dispatches to `admin.py`, writes one reply into `outbox`, and deletes the request. |
| `agentic/telegram.py` | Unprivileged daemon (`agbot`) polling Telegram `getUpdates` (50-second long poll) for the single `owner_id` private chat, managing 1-hour confirmation buttons in `/var/lib/agbot/pending.json`, writing requests to `inbox`, and flushing `outbox` every 2 seconds. |
| `agentic/daily.py` | Runs the daily `canary-<yyyymmdd>` honesty canary mission, deletes `canary-*` missions older than `canary_max_age_days` (default 7 days), and writes `/var/backups/agentic/agentic-<yyyymmdd>.tar.gz` (keeping 30 archives and recording `<state>/backup-status.json`). |
| `agentic/health.py` | Runs five automated self-repairs (inactive services, failed units, guidance drift, disk usage `> 85%`, stuck spool files `> 1h`), probes provider models as `agwork`, checks the latest honesty canary, and queues the daily health card. |
| `agentic/cli.py` | Command-line parser and dispatch for `agentic new|run|report|verify|mission|inbox|notify-failure|migrate-workspaces|honesty-canary|backup|health|config`. |

---

## 2. Accounts and Privilege Boundaries

| Account | Default Name | Home / Mode | Privileges & Role |
|---|---|---|---|
| Supervisor | `root` | `/root` | Owns `/opt/agentic`, `/var/lib/agentic` (`0700`, missions, hidden checks, ledgers, scratch snapshots), and `/etc/agentic`. Starts transient `systemd` units and processes the inbox spool. |
| Worker | `agwork` | `/var/lib/agwork` (`0700`, `nologin`) | Runs coding iterations inside `/var/lib/agwork/missions/<id>/work`. Cannot read `/home`, `/var/lib/agentic`, `/var/lib/agplan`, `/var/spool/agentic`, or `/etc/agentic/telegram.json`. |
| Planner / Reviewer | `agplan` | `/var/lib/agplan` (`0700`, `nologin`) | Runs toolless `omp --no-tools` calls for planning and cheat review. Isolated from `agwork` and `/var/lib/agentic`. |
| Telegram Bot | `agbot` | `/var/lib/agbot` (`0700`, `nologin`) | Talks to `https://api.telegram.org`. Reads `/etc/agentic/telegram.json` (`root:agbot 0640`) and owns `/var/spool/agentic/{inbox,outbox}` (`0700`). Never touches `/var/lib/agentic` or workspaces. |
| Interactive User | `agent` | `/home/agent` | Human operator / interactive agent account. Its `~/.omp/agent/` guidance files are synced from `/opt/agentic/guidance/user` and locked with `chattr +i`. Has no `sudo` unless `install.sh --agent-sudo` was explicitly passed. |

All non-root service accounts are excluded from the `procview` group when `/proc` is mounted with `hidepid=2`, preventing any account from inspecting another account's command line or environment variables in `/proc/<pid>/`.

---

## 3. Data Flow

### 3.1 Automatic Planning (`agentic/planner.py`)

When `agentic new <request-file>` or `agentic mission new <request-file>` (or a Telegram text message) starts a mission:

1. `request.md` is validated (10 to 20,480 UTF-8 bytes in Telegram; 1 to 20,480 UTF-8 bytes on the CLI, no NUL) and saved in `<state>/missions/<id>/request.md` (`0600`).
2. `plan_started` records `sha256(request.md)` and its byte length.
3. Four sequential model steps run as `agplan` via `llm.ask` (`plan_step` records):
   - `brief`: extracts `goal`, `constraints`, `deliverables`, and `assumptions`.
   - `design`: produces the implementation `approach` and 1 to 12 testable `criteria` (`c1`, `c2`, …).
   - `checks`: writes a hidden `Check` (`argv`, `timeout`, `network`, `files`) for each criterion.
   - `critique`: before calling the critic model, the supervisor runs every new check on a fresh empty temporary folder; any check that exits `0` is mechanically vetoed (`passes on an empty folder`). The critic model then checks whether each check genuinely tests its criterion.
4. Vetoed criteria are sent back to `checks` and `critique` for at most 2 rewrite rounds (`round` 1 and 2). Criteria still vetoed after round 2 are dropped and recorded in `plan_finished.dropped` (with leak-filtered reasons). If every criterion is dropped, planning ends with `plan_failed`.
5. Otherwise `mission.json` (`0600`) and `plan_finished` are written. Planning time does not count against `mission_hours`.

### 3.2 Iteration Loop, Escalation, and Outages (`agentic/loop.py`)

1. **Lock and Mission Start:** `loop.run` acquires an exclusive non-blocking `flock` on `<state>/missions/<id>/lock` (exit code `2` if already locked) and emits `mission_started` containing only the public mission view (never check commands or files).
2. **Red Baseline:** Every criterion's check runs once on a snapshot of the initial worker directory (`baseline` record, plus SHA-256 file manifest). For a new mission, any check that exits `0` before the worker runs is marked `invalid_check` (⚠️) and excluded from worker prompts. For a follow-up mission (`<state>/missions/<id>/parent`), checks that pass at baseline are recorded in `inherited_pass` and verified by the canary at iteration 0.
3. **Worker Iteration:** While failing criteria remain and the mission wall-clock limit (`mission_hours`, default 24 h from `mission_started`) has not expired, the supervisor builds a fresh prompt containing the goal, suggested approach, criteria (`PASSING` / `FAILING`), any verified reviewer findings or canary failure lines, the previous worker's 4 KB stdout tail (`note`), and an oscillation warning if the current failing set matches a non-consecutive earlier iteration.
4. **Model Outages vs. Stalls:** If the worker exits non-zero with a rate-limit/quota/gateway/auth pattern on `stderr` or fails within 120 seconds without changing the workspace digest, `worker_unavailable` is recorded instead of `iteration_worker`. The supervisor tries `config.ALTERNATE_PROVIDER` once immediately (`wait_seconds = 0`), then sleeps with backoff (`5, 10, 20, 40, 60` minutes). Outages never advance the iteration counter or count as a stall. Planner outages (`plan_unavailable`) wait up to 6 hours total; review outages wait up to 1 hour.
5. **Checks, Stalls, and Escalation:** After a normal `iteration_worker`, the supervisor runs checks for all active (`FAILING` and `PASSING`) criteria (`check_result` records). If the number of failing criteria does not shrink below the best count seen so far, `stalls` increments. After `escalate_after` stalls (default 2), `escalated` advances to the next model on `worker_ladder` and resets `stalls`. On the final ladder step, after `escalate_after + give_up_after` stalls (default 2 + 2 = 4), remaining failing criteria are marked `gave_up`.
6. **Canary, Mutation Score, and Strengthening:** Whenever an iteration leaves zero failing criteria:
   - `canary.run` re-runs all passing checks on a snapshot where every worker-created or modified regular file is truncated to 0 bytes (`canary` record). Any check that still exits `0` fails the canary (`canary_failed`), marks its criterion `FAILING` again, and sends the worker back.
   - Once the canary holds for all passing criteria and `.py` files were modified, `mutation.run` tests up to 6 single-point AST mutants within 600 seconds (`mutation` record). If at least 3 mutants ran and fewer than 50% were killed, the planner runs once (rounds 10–12) to strengthen the checks (`checks_strengthened`), re-evaluates the current work, and re-runs the canary and mutation score.
7. **Cheat Review:** Before finishing with any passing criteria, `review.run_review` calls a model from another vendor (`review` record). If at least one finding is mechanically verified (quote `>= 20` stripped chars verbatim in a shown file) and fewer than 2 `review_veto` records exist, `review_veto` marks the affected criteria `FAILING` and sends the worker back with the verified quote and leak-filtered problem.
8. **Completion:** `mission_finished` records the final status (`complete`, `partial`, or `stopped_time`), writes `report.md`, and queues the completion card and report document in `outbox`.

### 3.3 Follow-Up Missions and `/files` Delivery

- **Follow-ups (`admin.continue_mission`):** Triggered via Telegram (`/continue <id> <text>` or `<id> continue: <text>` / `<id> 이어서: <text>`). Requires the parent mission's unit to be inactive and its ledger intact. Creates `<base>-c<n>`, writes `<parent>\n` to `<state>/missions/<new_id>/parent` (`0600`), composes a new `request.md` combining the parent request, achieved/unachieved criteria summary, and new instructions, and copies the parent workspace as `agwork` (`runuser -u agwork -- cp -a`).
- **`/files <id>` (`results.py`):** Streams `report-<id>.md` followed by all regular files owned by `agwork` inside `/var/lib/agwork/missions/<id>/work` (opened with `O_NOFOLLOW|O_NONBLOCK`, skipping `.git`, `node_modules`, `__pycache__`, `.venv`, symlinks, fifos, devices, and sockets) into `results-<id>.zip` (max 20 MiB).

---

## 4. Ledger Record Types

Every record in `<state>/missions/<id>/ledger.jsonl` is a single-line canonical JSON object with six keys: `seq` (1-based integer), `ts` (UTC ISO 8601 with milliseconds, e.g. `2026-10-18T05:53:21.123Z`), `type`, `data`, `prev` (previous record's `hash`, or 64 zeros for `seq == 1`), and `hash` (hex SHA-256 of canonical JSON without `hash`). Immediately after record 1 is written, the supervisor runs `chattr +a` on the ledger file.

| Record Type | Emitted When / `data` Contents |
|---|---|
| `plan_started` | Planning begins: `{request_sha256, request_bytes}`. |
| `plan_step` | One planner model attempt completes: `{step, round, attempt, model, thinking, ok, duration_seconds, answer?, kind?, error?, stderr?, output?, empty_folder?}`. |
| `plan_unavailable` | All models for a planning or review step failed with provider outage patterns: `{step, round, attempt, reason, wait_seconds, iteration?}`. |
| `plan_finished` | Planning succeeded and `mission.json` is written: `{goal, criteria_count, dropped: [{id, text, problem}]}`. |
| `plan_failed` | Planning failed permanently: `{step, kind, reason}`. |
| `mission_started` | Loop starts: public mission view `{id, goal, criteria: [{id, text}], limits, ladder, request?, approach?, assumptions?, parent?}`. |
| `baseline` | Initial check run before iteration 1: `{results: {cid: CheckResult}, invalid: [cid], inherited_pass?: [cid], manifest: {path: sha256}}`. |
| `iteration_worker` | Worker iteration ran: `{iteration, step, model, thinking, exit_code, timed_out, duration_seconds, active: [cid], note, stderr}`. |
| `worker_unavailable` | Worker failed due to model/provider outage: `{iteration, step, model, thinking, reason, attempt, wait_seconds, next_model, exit_code, timed_out, duration_seconds, stderr, note}`. |
| `check_result` | One criterion check finished: `{iteration, criterion, passed, exit_code, timed_out, stdout, stderr}`. |
| `canary` | Canary check run on zero-truncated changed files: `{iteration, results: {cid: {held, exit_code}}, changed_files}`. |
| `mutation` | AST mutation score run: `{iteration, mutants, killed, skipped, details: [{file, line, operator, killed, skipped}]}`. |
| `checks_strengthened` | Planner rewrote checks after a weak mutation score: `{iteration, ok, replaced: [cid], kept: [cid], error?}`. |
| `review` | Cross-vendor cheat review completed: `{iteration, ok, model, findings: [{criterion, file, quote, problem, verified, reason}], files_shown, files_skipped, attempts, error?}`. |
| `review_veto` | Verified reviewer findings vetoed passing criteria (at most twice per mission): `{iteration, findings: [{criterion, file, quote, problem}]}`. |
| `escalated` | Stall threshold reached; ladder advanced: `{after_iteration, from_step, to_step, model, stalls}`. |
| `gave_up` | Final ladder step stalled out: `{after_iteration, criteria: [cid], attempts}`. |
| `stopped_time` | Mission wall-clock limit (`mission_hours`) reached: `{after_iteration, unfinished: [cid]}`. |
| `mission_finished` | Terminal record for every mission: `{status: "complete" | "partial" | "stopped_time" | "plan_failed", iterations}`. |
| `notice` | Operational warning recorded in the ledger (e.g. `{kind: "append_only", text, exit_code, detail}` when `chattr +a` fails). |

---

## 5. Spool Protocol (`/var/spool/agentic`)

The Telegram daemon (`agbot`) and the supervisor (`root`) communicate exclusively through `/var/spool/agentic/inbox` and `/var/spool/agentic/outbox` (both owned by `agbot:agbot`, mode `0700`):

- **Atomic writes:** `spool.py` opens the spool directory with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, creates `<name>.tmp` via `openat` (`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0600`), writes the JSON payload, calls `fsync`, calls `fchown` to `agbot` when running as `root`, and atomically renames `<name>.tmp` to `<name>`. Readers ignore `*.tmp`, `*.doc`, and dot files.
- **Inbox (`agbot` → `root`):** Each file is at most 32 KB (`INBOX_ITEM_MAX_BYTES`) with strict JSON validation (`type` in `new`, `list`, `status`, `report`, `stop`, `files`, `continue`; optional `text`, `id`, `reply_to`). `agentic inbox` locks `/run/agentic-inbox.lock`, processes files oldest-first, writes one reply into `outbox`, and unlinks the inbox file even if rejected.
- **Outbox (`root` → `agbot`):** Each item is either `{"html": ["..."], "reply_to"?: int}` (up to 1 MiB total, each string `<= 4096` chars of pre-escaped Telegram HTML) or `{"document": "<name>.doc", "filename": "...", "caption"?: "...", "reply_to"?: int}` where `<name>.doc` (up to 20 MiB) is written before its JSON item. `agbot` flushes the outbox every 2 seconds, deleting items on HTTP `200` or `4xx` and backing off on `429`/`5xx`/network errors.

---

## 6. `systemd` Units, Timers, and Launchers

### Launchers (`/usr/local/sbin/`, `root:root 0755`)
- `/usr/local/sbin/agentic`: runs `/usr/bin/python3.12 -I` with `/opt/agentic` on `sys.path` and calls `agentic.cli.main()`.
- `/usr/local/sbin/mission`: shorthand wrapper calling `agentic.cli.main(["mission", ...])` (requires `root`).
- `/usr/local/sbin/agentic-guidance-sync` and `agentic-guidance-check`: sync `/opt/agentic/guidance/user` into `/home/agent/.omp/agent`, lock synced files with `chattr +i`, and check for SHA-256 or `lsattr` drift.

### Installed Units (`/etc/systemd/system/`)
| Unit | Account | Trigger / Schedule | Purpose |
|---|---|---|---|
| `agentic@.service` | `root` | Started by `mission new/start` or inbox (`%i` = mission ID) | Runs `/usr/local/sbin/agentic run %i` (`Restart=on-abnormal`, `SuccessExitStatus=1`, `OnFailure=agentic-failed@%i.service`). |
| `agentic-failed@.service` | `root` | `OnFailure=` of `agentic@%i.service` | Runs `/usr/local/sbin/agentic notify-failure %i` to send a crash card to Telegram. |
| `agentic-telegram.service` | `agbot` | Enabled when `--telegram` is used | Runs `agentic.telegram` (`Restart=always`, `ProtectSystem=strict`, `ProtectHome=yes`, `ReadWritePaths=/var/spool/agentic /var/lib/agbot`). |
| `agentic-inbox.path` | `root` | `PathChanged=/var/spool/agentic/inbox` | Starts `agentic-inbox.service` immediately when the bot writes a request. |
| `agentic-inbox.timer` | `root` | `OnCalendar=minutely` (`Persistent=true`) | Backup trigger for `agentic-inbox.service` every minute. |
| `agentic-inbox.service` | `root` | Triggered by `.path` and `.timer` | Oneshot `/usr/local/sbin/agentic inbox` (`UMask=0077`). |
| `agentic-daily.timer` | `root` | `OnCalendar=*-*-* 07:30:00 Asia/Seoul` | Triggers `agentic-daily.service` once per day. |
| `agentic-daily.service` | `root` | Started by `agentic-daily.timer` | Oneshot: runs `-agentic honesty-canary` (exit code ignored so backup always runs) and `agentic backup`. |
| `agentic-health.timer` | `root` | `OnCalendar=*-*-* 08:00:00 Asia/Seoul` | Triggers `agentic-health.service` once per day. |
| `agentic-health.service` | `root` | Started by `agentic-health.timer` | Oneshot: runs `/usr/local/sbin/agentic health` (self-repairs + morning health card). |

### Transient `systemd-run` Units
- **Worker unit (`worker.py`):** `systemd-run --uid=agwork --wait --pipe --collect --quiet` in `Slice=user-<agwork uid>.slice` with `KillMode=control-group`, `TimeoutStopSec=10`, `RuntimeMaxSec=(iteration_minutes + 5) * 60`, `ProtectHome=tmpfs`, `BindPaths=<workdir>`, `BindReadOnlyPaths=/var/lib/agwork/.omp`, writable binds for omp state/SQLite files, `PrivateTmp=yes`, `NoNewPrivileges=yes`, and `InaccessiblePaths=/var/lib/agentic /var/lib/agplan /var/spool/agentic /etc/agentic/telegram.json`.
- **Planner / Reviewer unit (`llm.py`):** `systemd-run --uid=agplan --wait --pipe --collect --quiet` in `Slice=user-<agplan uid>.slice` with `KillMode=control-group`, `RuntimeMaxSec=1200`, `WorkingDirectory=/`, running `/opt/omp/omp --no-tools` with the prompt on `stdin`.
- **Check unit (`checks.py`, when `AGENTIC_CHECK_MODE=systemd`):** `systemd-run --wait --pipe --collect --quiet` with `DynamicUser=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`, `RuntimeMaxSec=<timeout>`, `WorkingDirectory=<snapshot>`, `ReadWritePaths=<snapshot>`, and `PrivateNetwork=yes` (unless `check.network` is `true`).

---

## 7. Internationalization (`agentic/i18n/`)

All human-readable output—`report.md`, Telegram HTML cards, bot replies and buttons, `agentic mission` CLI output, and the daily health card—is rendered through `agentic.i18n.t(key, **params)`:

- `agentic/i18n/en.py` (`MESSAGES`) provides the English catalog (default: `display.language = "en"`).
- `agentic/i18n/ko.py` (`MESSAGES`) provides the Korean catalog (`display.language = "ko"`).
- `tests/test_i18n.py` verifies that `en.MESSAGES` and `ko.MESSAGES` expose the exact same keys and `{placeholder}` sets, that no Korean string literal exists outside `agentic/i18n/`, and that no message in either catalog matches the banned AI-slop patterns from `TEMPLATE-RULES.md` or `guidance/user/rules/english-writing.md`.
- Prompts sent to coding, planning, and review models (`worker.py`, `planner.py`, `review.py`) remain in English regardless of `display.language`.

---

## 8. Configuration (`/etc/agentic/agentic.toml`)

Host-specific settings live in `/etc/agentic/agentic.toml` (or the path in `AGENTIC_CONFIG`), organized into `[accounts]`, `[paths]`, `[models]`, `[limits]`, `[display]`, and `[telegram]`. Built-in defaults use `timezone = "UTC"` and `language = "en"`; servers upgraded from earlier stages set `timezone = "Asia/Seoul"` and `language = "ko"`. Run `agentic config check` to validate and print the effective configuration. For every key, type, default, and environment variable override, see [`docs/CONFIG.md`](CONFIG.md).

---

## 9. CLI Reference and Operational Details

### 9.1 Command-Line Interface (`python3.12 -m agentic` / `/usr/local/sbin/agentic`)

```bash
/usr/bin/python3.12 -m agentic new <request-file> [--id ID]   # plan a mission from a plain-language request, then run it
/usr/bin/python3.12 -m agentic run <mission-id | path/to/mission.json>
/usr/bin/python3.12 -m agentic report <mission-id>            # regenerate report.md from the ledger
/usr/bin/python3.12 -m agentic verify <mission-id>            # verify ledger hash chain and warn if not chattr +a
/usr/bin/python3.12 -m agentic mission new|start|list|status|report|stop ...   # root only
/usr/bin/python3.12 -m agentic inbox                          # root: handle the Telegram bot's inbox requests
/usr/bin/python3.12 -m agentic notify-failure <id>            # run by OnFailure= of agentic@<id>.service
/usr/bin/python3.12 -m agentic migrate-workspaces [OLD_ROOT]  # root: move Stage B-E worker folders to agwork
/usr/bin/python3.12 -m agentic honesty-canary                 # root, daily: start canary-<yyyymmdd>, delete old canaries
/usr/bin/python3.12 -m agentic backup                         # root, daily: /var/backups/agentic/agentic-<yyyymmdd>.tar.gz
/usr/bin/python3.12 -m agentic health                         # root, daily: self-repairs, then the health card
/usr/bin/python3.12 -m agentic config check                   # validate and print effective configuration
/usr/bin/python3.12 -m agentic.telegram                       # the Telegram bot daemon (account agbot)
```

- **Exit codes (`new` and `run`):** `0` when `status == "complete"`, `1` when finished but not complete (`partial`, `stopped_time`, or `plan_failed`), `2` on error (invalid arguments, broken ledger, or `MissionBusy` when another process holds the mission `flock`).
- **`mission` admin subcommands (`root` only, `/usr/local/sbin/mission`):**
  - `mission new REQUEST_FILE [ID]`: validates the request, creates the worker folder as `agwork` (`runuser -u agwork -- mkdir -p ...`), and enables `agentic@<id>.service`. Refused when `max_active_missions` (default 3) units are already `active` or `activating`.
  - `mission start FILE`: installs a hand-written `mission.json` (`0600`) and starts `agentic@<id>.service`.
  - `mission list`: prints one summary line per mission (`id`, `systemctl is-active` state, last ledger record type and display timestamp, iteration count).
  - `mission status ID`: prints the summary line, the last 5 ledger events (never check content), and `report.md`.
  - `mission report ID`: regenerates `report.md` from the ledger.
  - `mission stop ID`: runs `systemctl disable --now agentic@<id>.service`.

### 9.2 Briefings, `report.md`, and Telegram Cards (`briefing.py`, `report.py`, `cards.py`)

Every `report.md` and Telegram card is rendered from a single deterministic `Briefing` structure built from the ledger, the public mission view, and `DECISIONS.md`—never by a model. In order, it answers:
1. **What happened (headline):** `✅ Complete` (`✅ 완료`), `⚠️ Partly complete` (`⚠️ 일부 완료`), `⏱️ Time limit reached` (`⏱️ 시간 초과`), `❌ Planning failed` (`❌ 계획 실패`), `🧭 Planning` (`🧭 계획 중`), or `🛠️ Running` (`🛠️ 진행 중`).
2. **How much is achieved:** `Achieved a / t (p%)` (`달성 a / t (p%)`), counting only criteria whose latest supervisor check exited `0` (dropped and invalid criteria count in the denominator `t`, never in `a`).
3. **What to do now (`## 1. What to do now` / `## 1. 지금 하실 일`):** concrete owner actions, or `None — no action needed`.
4. **Open decisions (`## 2. Decisions needed` / `## 2. 판단하실 것`):** structured A/B choices with one deterministic recommendation and reason.
5. **Copyable next commands (`## 3. Next command to send` / `## 3. 다음에 보낼 말`):** exact bot commands the owner can copy and send.
6. **Remaining criteria (`## 4. Remaining criteria` / `## 4. 남은 조건`)** and **Notes (`## 5. Notes` / `## 5. 알아 두실 것`)**.
7. **Unverified AI notes (`## 6. AI notes (unverified)` / `## 6. AI가 정한 것 (검증 안 됨)`):** planner assumptions (`[Plan]` / `[계획]`) and worker `DECISIONS.md` lines rendered inside `> ` blockquotes.
8. **Appendix (`## Appendix · Check results by criterion`):** per-criterion mark (`✅`, `❌`, `⚠️`), text, reason, last check time in the display time zone, exit code, and attempt count.

### 9.3 Telegram Bot Interaction (`telegram.py`)

- Configured by `/etc/agentic/telegram.json` (`root:agbot 0640`): `{"token": "<bot token>", "owner_id": <your numeric Telegram user id>}`. Only private-chat updates where `from.id == chat.id == owner_id` are processed; all other updates are ignored silently.
- Plain text of 10 to 20,000 UTF-8 bytes is treated as a new mission request: the bot previews the first 300 characters with `Start` / `Cancel` (`시작` / `취소`) inline buttons valid for 1 hour (`/var/lib/agbot/pending.json`).
- Commands: `/list` (newest 20 missions), `/status <id>` (finish card, or progress card with the last 3 events while running), `/report <id>` (`report-<id>.md` document), `/files <id>` (`results-<id>.zip` document up to 20 MiB), `/continue <id> <text>` (or `<id> continue: <text>` / `<id> 이어서: <text>`), `/stop <id>` (with confirmation buttons), `/help`, and `/start`.
