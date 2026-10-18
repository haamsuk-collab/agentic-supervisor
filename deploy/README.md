# Installing the agentic supervisor

These files are reviewed and installed **by hand**; the code installs nothing itself. Run every command as root.
Paths are the defaults of `agentic/config.py`; each can be moved with its `AGENTIC_*` environment variable.

Nothing below was run on a target machine while this file was written: the commands and modes follow
SPEC.md and SPEC-C.md through SPEC-F.md. Whatever is marked **(unverified)** must be checked on the machine.

## 1. Accounts

Accounts with no sudo rights and none of them for logging in. The model-calling accounts must never be able to
read the hidden checks or each other's files.

| account | purpose | home |
|---|---|---|
| `agwork` | the worker (the coding agent runs as this user, SPEC-F.md) | `/var/lib/agwork` (0700) |
| `agplan` | planner and reviewer model calls (holds the hidden checks while they are written) | `/var/lib/agplan` |
| `agbot` | the Telegram bot (talks to the internet, parses messages) | `/var/lib/agbot` |

```
useradd --system --create-home --home-dir /var/lib/agwork --shell /usr/sbin/nologin agwork
chmod 0700 /var/lib/agwork
useradd --system --create-home --home-dir /var/lib/agplan --shell /usr/sbin/nologin agplan
useradd --system --create-home --home-dir /var/lib/agbot  --shell /usr/sbin/nologin agbot
```

`runuser -u agwork -- mkdir -p ...` runs the command directly, not through the login shell, so `nologin` should not
get in the way **(unverified)**. Do not add any of them to `sudo`/`wheel`. Until Stage E the worker was the
interactive `agent` account; Stage F moves it to `agwork`, which cannot see `/home` at all (`ProtectHome=tmpfs`).

### The worker's omp (SPEC-F.md section 6)

- The worker runs the root-owned, pinned binary `/opt/omp/omp`, never an account's own copy:
  `install -d -m 0755 /opt/omp && install -m 0755 -o root -g root <omp binary> /opt/omp/omp`.
- `/var/lib/agwork/.omp/agent/models.yml` and `config.yml` (owned by `agwork`, 0600) hold **agwork's own gateway
  token** (section 3). `config.yml` sets `retry.modelFallback: false` and turns the advisor off.
- The worker unit mounts `/var/lib/agwork/.omp` read-only and re-mounts only omp's state writable:
  `logs`, `run`, `natives`, `cache`, `agent/cache`, `agent/sessions`, the SQLite files
  `agent/{agent,models,history,skill-descriptions}.db` and `stats.db` with their `-wal`/`-shm` files. A path that
  does not exist is skipped (`BindPaths=-…`), and a bind mount needs an existing source, so run omp once as
  `agwork` **before** the first mission to create them:
  `runuser -u agwork -- env HOME=/var/lib/agwork /opt/omp/omp -p --config /etc/agentic/omp-overlay.yml "Reply OK"`.
  If SQLite later fails to create a `-wal` file inside the read-only `agent/` folder, run that command again
  **(unverified: whether omp keeps the `-wal`/`-shm` files between runs)**.
- `user-<uid of agwork>.slice` limits (CPU, memory, tasks) are the operator's; the worker unit is placed in it.

## 2. Folders and modes

```
install -d -m 0755 -o root  -g root  /opt/agentic             # the code, owned by root
cp -a agentic /opt/agentic/agentic                            # from the repository root
chown -R root:root /opt/agentic && chmod -R go-w /opt/agentic

install -d -m 0700 -o root  -g root  /var/lib/agentic         # ledgers, mission files, hidden checks
install -d -m 0700 -o agwork -g agwork /var/lib/agwork/missions  # worker folders (made and copied as `agwork`)

install -d -m 0755 -o root  -g root  /var/spool/agentic       # bot <-> supervisor spool (the units set ReadWritePaths here)
install -d -m 0700 -o agbot -g agbot /var/spool/agentic/inbox   # the bot writes, root reads
install -d -m 0700 -o agbot -g agbot /var/spool/agentic/outbox  # root writes, the bot reads and deletes
install -d -m 0700 -o agbot -g agbot /var/lib/agbot           # the bot's offset and pending confirmations

install -d -m 0755 -o root  -g root  /etc/agentic
install -m 0640 -o root -g agbot /dev/null /etc/agentic/telegram.json   # then fill it in, see below
install -m 0644 -o root -g root  omp-overlay.yml /etc/agentic/omp-overlay.yml
```

- `/etc/agentic/telegram.json` (root:agbot, 0640): `{"token": "<bot token from @BotFather>", "owner_id": <your numeric Telegram user id>}`.
  `owner_id` is your Telegram user id and also the private chat id. Every other sender is ignored.
- `/etc/agentic/omp-overlay.yml` is the omp configuration overlay used by the worker unit. It is not part of this
  repository: use the one from the existing installation **(not verified, file not in the repository)**.
- Root writes into the spool folders as a different owner: the processor and the supervisor `fchown` every file
  to `agbot`. The inbox and outbox must exist before the bot starts; a missing outbox silently disables
  notifications.
- The lock file `/run/agentic-inbox.lock` is created by the processor (0600).

## 3. Gateway tokens

Provider credentials never reach the worker or the planner. They stay in the `omp-auth-gateway` broker, which
listens on `127.0.0.1:4000`. Its clients are listed in `/var/lib/ompauth/gateway-clients.json`.

1. Add **one client token for `agwork`** and **one for `agplan`** to `gateway-clients.json` (a separate token per
   account, so one can be revoked alone). The exact file format is defined by the gateway; follow its own
   documentation **(unverified here)**.
2. Put each token into that account's omp configuration (`/var/lib/agwork/.omp/agent/` and `/var/lib/agplan/...`,
   mode 0600, owned by the account), pointing the provider base URL at `http://127.0.0.1:4000`.
3. The real provider tokens stay only in the broker. Check afterwards that `agwork` and `agplan` cannot read
   `/var/lib/ompauth/`.
4. `agentic@.service` starts after `omp-auth-gateway.service`, so that unit has to exist.

## 4. `/proc` with `hidepid=2`

The worker must not see other users' processes (their command lines hold prompts and file names). Mount `/proc`
with `hidepid=2` and give a group the right to see everything:

```
groupadd --system procview
# members of procview see all processes: only add accounts that are allowed to (not agwork, agplan, agbot)
```

`/etc/fstab`:

```
proc  /proc  proc  defaults,hidepid=2,gid=<procview gid>  0  0
```

Use the numeric group id (`getent group procview`). Apply with `mount -o remount,hidepid=2,gid=<gid> /proc`.
systemd itself and `loginctl`/`systemctl` need to keep working: if a service stops working after the remount,
check that it is not running under a restricted `/proc` view, and that the `procview` group is what you meant
**(unverified)**.

## 5. Launchers and units

```
install -m 0700 -o root -g root deploy/bin/agentic /usr/local/sbin/agentic
install -m 0700 -o root -g root deploy/bin/mission /usr/local/sbin/mission

install -m 0644 deploy/systemd/*.service deploy/systemd/*.path deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agentic-telegram.service agentic-inbox.path agentic-inbox.timer
systemctl enable --now agentic-daily.timer agentic-health.timer
```

| file | what it is |
|---|---|
| `agentic@.service` | one supervised mission, `%i` is the mission id; `OnFailure=` starts `agentic-failed@%i.service` |
| `agentic-failed@.service` | oneshot `agentic notify-failure %i`: tells the owner that a supervisor crashed |
| `agentic-telegram.service` | the bot, as `agbot`, sandboxed (`ProtectSystem=strict`, `ProtectHome=yes`, ...) |
| `agentic-inbox.path` | runs the processor when the inbox folder changes |
| `agentic-inbox.service` | oneshot `agentic inbox`, as root |
| `agentic-inbox.timer` | runs the processor every minute as a backup for the path unit |
| `agentic-daily.timer` | `07:30:00 Asia/Seoul` every day (`Persistent=true`) |
| `agentic-daily.service` | oneshot (`UMask=0077`): `agentic honesty-canary` (its exit status is ignored, so the backup still runs), then `agentic backup` |
| `agentic-health.timer` | `08:00:00 Asia/Seoul` every day (`Persistent=true`) |
| `agentic-health.service` | oneshot (`UMask=0077`): `agentic health` runs the self-repairs and queues the morning card |

`agentic@<id>.service` units are not enabled by hand: `mission new`, `mission start` and the bot's `시작` button
do that (`systemctl enable --now agentic@<id>.service`). Each worker run is a transient unit
(`systemd-run --uid=agwork`) with `ProtectHome=tmpfs`, `BindPaths=<worker folder>`, the omp binds above,
`PrivateTmp=yes`, `NoNewPrivileges=yes` and `InaccessiblePaths=/var/lib/agentic /var/lib/agplan /var/spool/agentic
/etc/agentic/telegram.json`.

### Moving worker folders from Stage E

Stage B–E kept worker folders in `/home/agent/missions/<id>/work`. After creating `agwork` and
`/var/lib/agwork/missions`, and with no mission running:

```
agentic migrate-workspaces            # or: agentic migrate-workspaces /some/other/old/root
```

It prints one line per mission: `moved`, `already moved`, `missing: no folder …` or `failed: …`. Root packs each
old folder into a root-only archive under `/var/lib/agentic/scratch/`; `agwork` unpacks it, so every new file
belongs to `agwork`. The old folder is deleted only after the new one exists. Running it again is safe.

## 6. Using it

```
mission new REQUEST_FILE [ID]   # plan a mission from a plain-language request, start its supervisor
mission start FILE              # install a hand-written mission.json and start its supervisor
mission list                    # one line per mission: id, unit state, last record, iteration count
mission status ID               # that line, the last 5 events and the report
mission report ID               # regenerate report.md from the ledger
mission stop ID                 # systemctl disable --now agentic@ID.service
```

`mission` refuses to run unless the user is root. In Telegram, send a request as plain text and press `시작`; use
`/list`, `/status <id>`, `/report <id>`, `/files <id>`, `/continue <id> <text>` (or `<id> 이어서: <text>`) and
`/stop <id>` (`/continue` and `/stop` ask for confirmation).

## 7. Checking the installation

- `systemctl status agentic-telegram.service agentic-inbox.path agentic-inbox.timer`
- Send `/list` to the bot: it should answer within about a minute (instantly when the path unit works).
- `journalctl -u agentic-inbox.service -u agentic-telegram.service` for errors.
- `ls -l /var/spool/agentic/` must show both folders owned by `agbot` with mode `drwx------`.
- `runuser -u agbot -- cat /var/lib/agentic/missions/<id>/mission.json` must fail (the bot cannot read mission state).
