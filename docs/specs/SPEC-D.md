# agentic supervisor — Stage D specification: Telegram, model outages, operations

Stages B (SPEC.md) and C (SPEC-C.md) are built and installed. They stay unchanged and still apply.
Stage D adds:

1. A **Telegram bot**. The owner gives missions and reads status and reports from a phone, and
   gets a message when something important happens.
2. **Model-outage handling.** Rate limits, quota and provider errors are infrastructure failures.
   They must not look like "the worker made no progress".
3. **Operations code.** The `mission` admin command and the systemd units move into the
   repository so the project can be installed from it.

Read SPEC.md, SPEC-C.md and this file before writing code. Record every choice this file leaves open
in DECISIONS.md. The rules from earlier stages still hold:
- Python 3.12, standard library only.
- No `shell=True`.
- Tests use no network and no real model. The Telegram API is replaced by a local fake.

## 1. Privilege separation (the most important rule)

The bot parses messages from the internet, so it never runs as root and never touches mission
state directly.

| process | account | can do |
|---|---|---|
| bot daemon (`agentic.telegram`) | `agbot` (no login) | talk to `api.telegram.org`; write `/var/spool/agentic/inbox/`; read and delete files in `/var/spool/agentic/outbox/` |
| inbox processor (`agentic inbox`) | root | read the inbox, act through `agentic.admin`, write replies to the outbox |
| supervisor (`agentic run`) | root | as before, plus write notifications to the outbox |

- **Spool directories:** both are owned by `agbot`, mode 0700. Root writes into the outbox and
  reads from the inbox.
- **Root writing to the outbox:**
  - Random file names (`secrets.token_hex(16)`), created with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode
    0600, then `fchown` to agbot.
  - Write `<name>.tmp` first and `rename` it into place, so the bot never reads half a file. The
    rename happens inside the directory; follow no symlink.
- **Root reading from the inbox:** accept only a regular file that is owned by agbot, at most
  32 KB, and opened with `O_NOFOLLOW|O_NONBLOCK`. Validate its JSON strictly (section 3). Delete
  the file after handling it, even when it was rejected.

## 2. The bot daemon (`python3.12 -m agentic.telegram`, runs as agbot)

**Configuration.** `/etc/agentic/telegram.json` (root:agbot, 0640):
`{"token": "...", "owner_id": <int>}`. `owner_id` is both the owner's Telegram user id and the
private chat id. Its own state, such as the update offset and pending confirmations, lives in
`/var/lib/agbot/` (0700).

**Polling.**
- Call `getUpdates` with `timeout=50` and the stored offset. Save the offset after handling each
  update.
- Use `urllib.request` with a 70-second socket timeout.
- On network errors, back off 5 → 10 → 20 → 60 seconds. Never crash on bad data.

**Authorization.** Ignore every update unless it is a private message or a button press where
`from.id == owner_id` and `chat.id == owner_id`. Ignored updates get no reply; count them in the
log only.

**Commands** (all replies in Korean):
- **Plain text** (not a command), 10 to 20,000 bytes: treat it as a mission request.
  - Reply with its first 300 characters and two inline buttons, `시작` and `취소`.
  - The pending text is stored under a random 16-character key and expires after 1 hour.
  - `시작` writes an inbox `new` item and edits the message to "접수했습니다".
  - `취소` drops it.
  - A second press, or a press after expiry, does nothing except reply "이미 처리했거나 만료된
    요청입니다".
- `/list`: all missions with their state.
- `/status <id>`: one mission's state and its latest events.
- `/report <id>`: the full report.md.
- `/stop <id>`: stop the mission. This is the only command with a confirm button (`멈추기` /
  `취소`).
- `/help` and `/start`: usage text.

Every command except plain text becomes one inbox item. The bot only relays; root does the work.

**Outbox.**
- Every 2 seconds, send each outbox file as a message, then delete it.
- Texts longer than 4,000 characters are split on line boundaries.
- Send with `parse_mode` unset (plain text), so no formatting injection is possible.
- When Telegram rejects a message with 4xx, log it and delete the file. On 5xx or a network error,
  keep the file and retry later.

## 3. Inbox items and the processor (`agentic inbox`, root)

Inbox JSON (reject unknown keys):
`{"type": "new"|"list"|"status"|"report"|"stop", "text"?: str, "id"?: str, "reply_to"?: int}`.
- `new` requires `text`.
- `status`, `report` and `stop` require a valid mission `id`.
- `reply_to` is the Telegram message id the answer refers to; the outbox item carries it back.

The processor handles every file in the inbox (oldest first) through `agentic.admin` and writes one
outbox reply per item:
- `new`: `admin.new_mission(text)`. Reply `🚀 미션 시작: <id> — 계획을 세우는 중입니다`.
- `list`: one line per mission: id, service state, last record type and time, iteration count.
- `status`: the same line, plus the mission's last 5 ledger events summarised in Korean. Never
  show check data.
- `report`: report.md, or `아직 보고서가 없습니다`.
- `stop`: `admin.stop_mission(id)`. Reply `⏹️ 멈췄습니다: <id>`.

Errors become a Korean reply such as `없는 미션입니다: <id>`; nothing is raised to the bot. A
systemd path unit runs the processor when the inbox changes, and a timer runs it every minute as a
backup. A flock on `/run/agentic-inbox.lock` keeps one processor at a time.

## 4. `agentic.admin` and the `mission` command

Move the logic of the currently installed `/usr/local/sbin/mission` script into the package. The
current script is given in section 9 for reference.
- `admin.new_mission(text, mission_id=None)`:
  - validate the text the same way as `cli._read_request_file`;
  - create the mission folder (0700) and `request.md` (0600);
  - create the worker folder **as the worker user** with `runuser -u agent -- mkdir -p <workdir>`,
    never as root;
  - run `systemctl enable --now agentic@<id>.service`.
- `admin.start_mission(path)` does the same for a hand-written mission.json, as `mission start` does
  today.
- `admin.stop_mission(id)` runs `systemctl disable --now agentic@<id>.service`.
- `admin.list_missions()` and `admin.mission_status(id)`.
- New CLI command `python3.12 -m agentic mission ...` with the same subcommands as today: `new`,
  `start`, `list`, `status`, `report`, `stop`. It must refuse to run unless the user is root.

`systemctl` and `runuser` are run through a small command runner that tests replace with a fake.
Tests must never call the real ones.

## 5. Notifications (supervisor → outbox)

Notifications are best effort. A failure to notify is logged to stderr and never changes the
mission. The outbox path is `AGENTIC_OUTBOX`, default `/var/spool/agentic/outbox`. When the
directory does not exist, notifications are silently skipped (hand-run tests, no bot installed).

Hook into the ledger append path (`Runner.emit` and the planner's emit). Each notification file
name is derived from `<mission id>-<seq>`, so a resumed run never sends the same event twice
(`O_EXCL`; an existing file means it was already sent).

| record | message (Korean, one line plus a few detail lines) |
|---|---|
| `plan_finished` | `🧭 계획 완료: <id> — 완료 조건 N개` (and `, 제외 M개` when some were dropped) `. 작업을 시작합니다.` |
| `plan_failed` | `❌ 계획 실패: <id> — <public reason>` |
| `escalated` | `⬆️ 더 강한 모델로 바꿈: <id> — <from model> → <to model>` |
| `worker_unavailable` (section 6) | only when the current outage has lasted ≥ 30 minutes, and once per outage: `⏸️ 모델을 쓸 수 없어 기다리는 중: <id> — <minutes>분째, 이유: <reason>` |
| `review_veto` | `🔍 꼼수 리뷰가 문제를 찾음: <id> — 다시 작업합니다` |
| `mission_finished` | first line `✅ 미션 완료` / `⚠️ 미션 일부 완료` / `⏱️ 미션 시간 초과` / `❌ 계획 실패`, then `: <id>`; then counts `✅ a · ❌ b · ⚠️ c`, the time taken, and the whole report.md text (the bot splits it) |

A crashed supervisor unit is reported too: `agentic notify-failure <id>`, run by
`OnFailure=agentic-failed@%i.service`, writes `💥 감독자가 비정상 종료됨: <id> — 자동으로 다시
시작합니다` (or `— 다시 시작하지 않습니다` when systemd gave up).

## 6. Model outages (changed Stage B and C behaviour)

**Detecting an unavailable worker.** A worker iteration counts as *unavailable* instead of a normal
iteration when its exit code is non-zero and either:
- its stderr or final text matches (case-insensitive) one of: `rate.?limit`, `quota`,
  `usage.?limit`, `too many requests`, `\b429\b`, `overloaded`, `\b529\b`, `\b503\b`,
  `service unavailable`, `credit`, `unauthori[sz]ed`, `\b401\b`, `econnrefused`,
  `connection refused`, `gateway`; or
- it ran less than 120 seconds **and** the worker folder is byte-for-byte unchanged. Compare a
  sha256 over sorted relative paths plus contents, skipping SNAPSHOT_SKIP directories and symlink
  targets.

An unavailable iteration:
- records `worker_unavailable` with the model, reason, attempt number in this outage, and the wait
  before the next try;
- runs no checks, does not count as a stall, does not change the iteration number, and does not
  update the previous-iteration note.

**What happens next:**
1. Try the same ladder step on its alternate provider, if it has one:
   `anthropic/claude-sonnet-5-5` → `google-antigravity/claude-sonnet-5-5`,
   `anthropic/claude-opus-5-5` → `google-antigravity/claude-opus-5-5`. Put the table in
   config.py. Fable has no alternate.
2. Use the alternate only for that attempt. Every new attempt starts from the step's own model.
3. If the alternate is also unavailable, or there is none, wait. Waits go 5, 10, 20, 40, then 60
   minutes, and stay at 60.
4. The mission clock keeps running. When `mission_hours` is used up while waiting, the mission ends
   with `stopped_time` as usual, and the report adds the line
   `- 모델을 쓸 수 없어 기다린 시간: <h>시간 <m>분` under ⚠️.
5. **Resume:** the wait is rebuilt from the last `worker_unavailable` record. A restart sleeps only
   for what is left of the wait.

**Planner and reviewer outages.**
- An `llm.ask` failure where every attempt failed with `exit`, `timeout` or `start` kinds (never
  `schema`, `bad_json` or `no_json`) and the text matches the patterns above is an outage. Record
  `plan_unavailable` and wait with the same backoff.
- After 6 hours of waiting in total, planning fails with reason
  `모델을 6시간 동안 쓸 수 없었음`.
- A review outage waits at most 1 hour. Then the review is recorded as failed (Stage C behaviour).

Add `worker_unavailable` and `plan_unavailable` to the ledger record types. `LoopState` and the
planner's state must replay them.

## 7. Deployment files (`deploy/`, installed by the operator, not by the code)

Write these files. They are reviewed and installed by hand:
- `deploy/systemd/agentic@.service`: as currently installed (section 9), plus
  `OnFailure=agentic-failed@%i.service`.
- `deploy/systemd/agentic-failed@.service`: oneshot `agentic notify-failure %i`.
- `deploy/systemd/agentic-telegram.service`:
  - `User=agbot`, `Restart=always`, `RestartSec=10`;
  - `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`;
  - `ReadWritePaths=/var/spool/agentic /var/lib/agbot`;
  - `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`;
  - `ExecStart=/usr/bin/python3.12 -I -c 'import sys; sys.path.insert(0, "/opt/agentic"); from agentic.telegram import main; sys.exit(main())'`.
- `deploy/systemd/agentic-inbox.path` (`PathChanged=/var/spool/agentic/inbox`),
  `agentic-inbox.service` (oneshot `agentic inbox`), and `agentic-inbox.timer` (every minute).
- `deploy/bin/agentic` and `deploy/bin/mission`: tiny launchers like the current
  `/usr/local/sbin/agentic` (section 9). `mission` calls `agentic mission "$@"`.
- `deploy/README.md`: the install steps in order:
  - accounts `agent`, `agplan`, `agbot`;
  - folders and modes;
  - gateway tokens;
  - `/proc` `hidepid=2` with the `procview` group;
  - the units.

## 8. Tests (no network, fake Telegram, fake command runner)

All earlier tests keep passing. Add:

1. **Authorization.** Updates from another user id, another chat, a group chat or a channel are
   ignored with no reply and no inbox item.
2. **Confirm flow.**
   - Plain text produces the preview and buttons. `시작` creates exactly one `new` item.
   - A second `시작`, a press after expiry, or a forged callback key creates nothing.
   - `/stop` needs its button.
3. **Spool safety:**
   - The processor rejects and deletes a symlink, a file not owned by agbot (where testable), an
     oversized file, bad JSON, unknown keys and an invalid id.
   - Outbox writes refuse a pre-planted symlink at the target name.
4. **Processor.** Each command produces the right reply. `new` calls the fake runner with
   `runuser -u agent -- mkdir -p …` and `systemctl enable --now agentic@<id>.service`, in that
   order.
5. **Notifications:**
   - Each record type in section 5 produces its message.
   - A resumed run does not repeat one.
   - A missing outbox directory changes nothing.
   - No notification contains check argv or check file content.
6. **Worker outage:**
   - A rate-limit stderr gives `worker_unavailable`, then the alternate provider, then a backoff
     wait.
   - No stall is counted and the ladder does not escalate.
   - An unchanged folder with a fast non-zero exit is unavailable.
   - A slow non-zero exit with changes is a normal iteration.
   - Resume after a kill during the wait sleeps only the remainder.
   - Time running out while waiting gives `stopped_time` and the ⚠️ wait line.
   - Use an injectable clock and sleep; no test really waits.
7. **Planner outage.** Outages are retried after waits and give `plan_failed` after 6 hours.
   A schema error is never treated as an outage.
8. **Long messages.** Splitting at 4,000 characters keeps every line and the original order.
9. **`/report`.** The report text sent equals report.md.

## 9. Reference: what is installed today

`/usr/local/sbin/agentic`:
```
#!/usr/bin/python3.12 -I
import sys
sys.path.insert(0, "/opt/agentic")
from agentic.cli import main
sys.exit(main())
```

`/etc/systemd/system/agentic@.service`:
```
[Unit]
Description=agentic mission %i
After=network-online.target omp-auth-gateway.service
Wants=network-online.target omp-auth-gateway.service
[Service]
Type=exec
ExecStart=/usr/local/sbin/agentic run %i
SuccessExitStatus=1
Restart=on-abnormal
RestartSec=30
TimeoutStopSec=30
UMask=0077
[Install]
WantedBy=multi-user.target
```

`/usr/local/sbin/mission` today:
- `new REQUEST [ID]`: validates with `cli._read_request_file`, writes `request.md` 0600, runs
  `runuser -u agent -- mkdir -p <workdir>`, then `systemctl enable --now agentic@<id>`.
- `start FILE`: validates the file and copies it to `mission.json` 0600; refuses an existing
  mission with different content.
- `list`: one line per mission: id, `systemctl is-active` state, last record type and time,
  iteration count.
- `status ID`: the same line plus the report.
- `report ID`: runs `agentic report`.
- `stop ID`: runs `systemctl disable --now`.

## 10. Done means

- `make test` passes and you show its full output.
- README.md documents the bot and `mission`.
- DECISIONS.md records every choice.
- Commit with a clear message.
- SPEC.md, SPEC-C.md and SPEC-D.md are not modified.

Out of scope: installing anything, creating accounts, and talking to the real Telegram API.
