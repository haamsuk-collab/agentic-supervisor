# Security Policy and Threat Model

`agentic-supervisor` runs untrusted, LLM-generated code and shell commands on a Linux host while accepting mission requests from Telegram. Security relies on Linux account separation, `systemd` namespace and filesystem sandboxing, `/proc` process hiding, an external credential broker (`omp-auth-gateway`), and strict input validation at every privilege boundary.

---

## 1. Threat Model

### 1.1 Dishonest or Prompt-Injected Worker (`agwork`)

The coding worker (`omp` running as `agwork`) executes arbitrary code inside its mission workspace (`/var/lib/agwork/missions/<id>/work`) and may read untrusted files or web pages containing prompt injections.

**What the worker CAN do:**
- Read, write, execute, symlink, or delete files inside its own current mission workspace (`/var/lib/agwork/missions/<id>/work`) and its own earlier workspaces under `/var/lib/agwork/missions/` (since all worker runs share the `agwork` UID).
- Make outbound network requests during worker iterations and send model requests through `omp-auth-gateway` (`127.0.0.1:4000`) using `agwork`'s client token.
- Write arbitrary text, symlinks, fifos, or oversized files into its workspace (including `DECISIONS.md`) in an attempt to confuse the supervisor, reviewer, report renderer, or `/files` zip builder.
- Spawn detached background processes (`nohup`, `setsid`) during an iteration.

**What the worker CANNOT do:**
- **Read hidden checks or ledgers:** `/var/lib/agentic` is mode `0700` (`root:root`) and listed in the worker unit's `InaccessiblePaths=`.
- **Read planner or bot files:** `/var/lib/agplan`, `/var/spool/agentic`, and `/etc/agentic/telegram.json` are mode `0700`/`0640` and blocked by `InaccessiblePaths=`.
- **Read `/home/agent` or other user homes:** the worker unit sets `ProtectHome=tmpfs`.
- **Modify its own `omp` binary or configuration:** `/opt/omp/omp` and `/etc/agentic/omp-overlay.yml` are owned by `root:root` (`0755`/`0644`); `/var/lib/agwork/.omp` is mounted `BindReadOnlyPaths=`, with only specific state subdirectories and SQLite files bind-mounted writable.
- **Extract model provider API keys or OAuth credentials:** real credentials stay inside `omp-auth-broker` (`/var/lib/ompauth`, unreadable by `agwork`); `agwork` holds only a revocable local gateway client token.
- **Inspect other users' processes:** `/proc` is mounted with `hidepid=2,gid=procview`, and `agwork` is not a member of `procview`.
- **Survive across iterations:** the worker runs inside a transient unit with `KillMode=control-group` and `TimeoutStopSec=10`; `systemd` terminates every process in the control group when the iteration ends or hits `RuntimeMaxSec`.
- **Exploit symlink, fifo, or hard-link traversal against `root`:**
  - `checks.py` copies workspaces without following symlinks, skips fifos/sockets/devices, and writes check files with `O_NOFOLLOW`.
  - `results.py` (`/files`) walks the workspace using directory file descriptors (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW`), opens files with `O_NOFOLLOW|O_NONBLOCK`, verifies `st_uid == agwork` when run as `root` (blocking hard links to root-owned files), and caps compressed output at 20 MiB.
  - `report.py` reads `DECISIONS.md` with `O_NOFOLLOW` (regular file only, `<= 64 KB`), escapes all Markdown syntax (`\`, `` ` ``, `*`, `_`, `[`, `]`, `<`, `>`, `#`, `|`), and wraps every line in `> `.

---

### 1.2 Compromised Telegram Account or Unauthorized Telegram Sender

The Telegram bot daemon (`agentic.telegram`) runs as the unprivileged `agbot` user (`ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`, `ReadWritePaths=/var/spool/agentic /var/lib/agbot`).

**What an unauthorized Telegram user (any chat or user ID other than `owner_id`) CAN and CANNOT do:**
- Every update where `chat.type != "private"`, `from.id != owner_id`, or `chat.id != owner_id` is ignored before command parsing. Unauthorized senders receive no reply, cannot create pending confirmations, and cannot write to `/var/spool/agentic/inbox`.

**What a compromised owner Telegram account (`owner_id`) CAN do:**
- Submit new mission requests (`10` to `20,000` UTF-8 bytes) and follow-up requests (`/continue`), up to `max_active_missions` (default 3) concurrent active missions.
- List missions (`/list`), view mission status (`/status <id>`), download mission reports (`/report <id>`), download workspace archives (`/files <id>`), and stop running missions (`/stop <id>`).

**What a compromised owner Telegram account (or a compromised `agbot` process) CANNOT do:**
- **Execute shell commands as `root` or read arbitrary host files:** `agbot` can only write JSON request files (`<= 32 KB`) into `/var/spool/agentic/inbox`. `agentic inbox` (`root`) validates every field strictly (`type` in `{"new", "list", "status", "report", "stop", "files", "continue"}`, `id` matching `^[a-z0-9][a-z0-9-]{0,40}$`, no symlinks, regular file owned by `agbot`) and never passes user input to a shell.
- **Read hidden checks, ledgers, or other files directly:** `agbot` has no access to `/var/lib/agentic`, `/var/lib/agwork`, `/var/lib/agplan`, or `/home/agent`.
- **Bypass mission verification:** missions started from Telegram run through the exact same planner, red baseline, hidden checks, canary, mutation score, and cheat review as CLI missions.

---

### 1.3 Curious Interactive Agent (`agent`) with or without `sudo`

The interactive account (`agent`, home `/home/agent`) is used for day-to-day interactive work on the server. Its `~/.omp/agent/` guidance files (`RULES.md`, `AGENTS.md`, `WATCHDOG.md`, `rules/`, `skills/`, `extensions/`, `agents/`) are synced from `/opt/agentic/guidance/user` and locked with the Linux immutable flag (`chattr +i`).

**Without `--agent-sudo` (the default of `install.sh`):**
- `agent` is an unprivileged user.
- `agent` **cannot** read `/var/lib/agentic` (`0700 root:root`), `/var/lib/agwork` (`0700 agwork:agwork`), `/var/lib/agplan` (`0700 agplan:agplan`), `/var/lib/agbot` (`0700 agbot:agbot`), `/var/spool/agentic/{inbox,outbox}` (`0700 agbot:agbot`), or `/etc/agentic/telegram.json` (`0640 root:agbot`).
- `agent` **cannot** remove `chattr +i` from `/home/agent/.omp/agent/RULES.md` (requires `CAP_LINUX_IMMUTABLE`) or run `agentic mission ...` (requires UID `0`).
- If `agent` deletes or replaces parent directories to bypass immutable files, the daily `agentic health` check (`08:00 Asia/Seoul`) detects the drift via `/usr/local/sbin/agentic-guidance-check`, restores the tree via `/usr/local/sbin/agentic-guidance-sync`, re-applies `chattr +i`, and reports the repair on the morning health card.

**With `--agent-sudo` (`deploy/sudoers/agentic-agent.example` installed to `/etc/sudoers.d/agentic-agent`):**
- Installing `agent ALL=(ALL) NOPASSWD: ALL` is an explicit owner choice (`install.sh --agent-sudo`, default **off**).
- With unrestricted `sudo`, `agent` has full `root` equivalence on the host: it **can** read `/var/lib/agentic`, inspect hidden checks, run `chattr -i` or `chattr -a`, and control `systemd` units.
- Do not enable `--agent-sudo` if the interactive `agent` account is untrusted with supervisor secrets. Remember that the mission worker never runs as `agent`; workers always run as `agwork`, which never has `sudo` rights.

---

## 2. Out of Scope

The following threats are outside the security boundary of `agentic-supervisor`:

1. **A malicious `root` user (or kernel compromise):** `root` owns the supervisor code (`/opt/agentic`), the state directory (`/var/lib/agentic`), the check runner, and `CAP_LINUX_IMMUTABLE` (`chattr`). `agentic-supervisor` defends `root` against untrusted workers, planners, and Telegram inputs; it cannot defend a mission against `root` itself.
2. **Compromised or dishonest model providers:** prompts, request texts, and workspace code (up to 300 KB during review) are transmitted to the configured model providers (`anthropic`, `google-antigravity`, or others configured in `omp`). A provider that logs data or returns degraded outputs is outside the supervisor's control, though a provider cannot forge a passing supervisor check on the host.
3. **Host OS hardening outside `agentic-supervisor`:** SSH configuration (`sshd`), firewall rules (`firewalld`), intrusion prevention (`fail2ban`), audit rules (`auditd`), and hardware/kernel security are the host administrator's responsibility (though `agentic health` monitors whether those services remain active).
4. **Internals of `omp` and `omp-auth-gateway`:** `agentic-supervisor` pins `/opt/omp/omp` and isolates per-account gateway client tokens, but the internal implementation of the `omp` binary and the `omp-auth-broker` / `omp-auth-gateway` daemon are separate components.

---

## 3. Reporting Security Issues

Report security vulnerabilities privately through **GitHub Security Advisories** on the repository (`Security` tab → `Report a vulnerability`). Do not open a public issue or pull request for undisclosed vulnerabilities, and do not include live tokens, private keys, or host-identifying information in the report.
