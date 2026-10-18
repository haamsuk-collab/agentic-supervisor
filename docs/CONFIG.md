# Configuration Reference (`agentic.toml`)

`agentic` reads an optional TOML configuration file from `/etc/agentic/agentic.toml` (or the path in `AGENTIC_CONFIG`) at import time using Python 3.12 `tomllib`. Every section and key is optional; when `/etc/agentic/agentic.toml` is absent, all settings keep their built-in defaults (`UTC` display time zone, English `en` catalog, and the standard Linux paths and model tables).

If `AGENTIC_CONFIG` is set to a missing path, or if the TOML file contains an unknown section, unknown key, or invalid value, `cli.main` (every subcommand) and `agentic.telegram` refuse to run, print `<path>: <key>: <problem>` to `stderr`, and exit with code `1`.

## Validation Command

```bash
/usr/bin/python3.12 -m agentic config check
```

`agentic config check` validates the active configuration file, applies environment overrides, prints the effective configuration as TOML (`[section]` and `key = value` lines, never secrets), and exits `0`. On an invalid configuration file it prints `<path>: <error>` to `stderr` and exits `1`.

---

## Sections and Keys

### `[accounts]`

System accounts used to isolate workers, planners, and the Telegram bot. All values must be valid POSIX user names (`^[a-z_][a-z0-9_-]{0,31}$`).

| Key | Type | Default | Description |
|---|---|---|---|
| `interactive_user` | string | `"agent"` | Interactive operator account whose `~/.omp/agent` guidance tree is checked and synced. |
| `worker_user` | string | `"agwork"` | Account that owns and runs worker iterations (`HOME=/var/lib/<worker_user>`). |
| `planner_user` | string | `"agplan"` | Account that runs planner and reviewer model calls (`HOME=/var/lib/<planner_user>`). |
| `bot_user` | string | `"agbot"` | Account that runs `agentic.telegram` and owns the inbox/outbox spools (`HOME=/var/lib/<bot_user>`). |

### `[paths]`

Filesystem locations and pinned binaries. Every value must be a non-empty absolute path starting with `/` without `..` segments.

| Key | Type | Default | Description |
|---|---|---|---|
| `state` | string | `"/var/lib/agentic"` | Root-only supervisor state folder (missions, hidden checks, hash-chained ledgers, scratch snapshots). |
| `workspaces` | string | `"/var/lib/agwork/missions"` | Parent folder of per-mission worker folders (`<workspaces>/<id>/work`). |
| `spool` | string | `"/var/spool/agentic"` | Spool root; sets default inbox `<spool>/inbox` and outbox `<spool>/outbox`. |
| `worker_omp` | string | `"/opt/omp/omp"` | Pinned `omp` binary executed inside the worker systemd unit. |
| `planner_omp` | string | `"/opt/omp/omp"` | Pinned `omp` binary executed inside the planner/reviewer systemd unit. |
| `worker_overlay` | string | `"/etc/agentic/omp-overlay.yml"` | Read-only `omp` configuration overlay passed via `--config` to worker runs. |
| `guidance_sync` | string | `"/usr/local/sbin/agentic-guidance-sync"` | Executable run by `agentic health` to repair guidance drift. |
| `guidance_check` | string | `"/usr/local/sbin/agentic-guidance-check"` | Executable run by `agentic health` to detect guidance drift (exit `0` when clean). |

Note: `WORKER_INACCESSIBLE` is derived automatically from `(state, "/var/lib/" + planner_user, spool, "/etc/agentic/telegram.json")`.

### `[models]`

Model lists, worker escalation ladder, and provider fallback table.

| Key | Type | Default | Description |
|---|---|---|---|
| `planner_steps` | table of `step = ["model:thinking", ...]` | `brief = ["google-antigravity/gemini-4-argon:high", "anthropic/claude-opus-5-5:high"]`, `design = ["anthropic/claude-fable-5-1:high", "anthropic/claude-opus-5-5:high"]`, `checks = ["anthropic/claude-opus-5-5:high", "anthropic/claude-fable-5-1:high"]`, `critique = ["google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"]` | Ordered `"model:thinking"` lists for the four planning steps (`brief`, `design`, `checks`, `critique`). Partial overrides keep defaults for omitted steps. |
| `review` | list of `"model:thinking"` strings | `["google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"]` | Ordered `"model:thinking"` list for the post-pass cheat review. |
| `worker_ladder` | non-empty list of tables | `[{model = "anthropic/claude-sonnet-5-5", thinking = "high", iteration_minutes = 45}, {model = "anthropic/claude-opus-5-5", thinking = "high", iteration_minutes = 45}, {model = "anthropic/claude-fable-5-1", thinking = "high", iteration_minutes = 90}]` | Default escalation ladder for missions that omit `ladder`. Each entry requires `model` (non-empty string) and accepts optional `thinking` (default `"high"`) and `iteration_minutes` (number `> 0`, default `45`). |
| `alternate_provider` | table of `string = string` | `{"anthropic/claude-sonnet-5-5" = "google-antigravity/claude-sonnet-5-5", "anthropic/claude-opus-5-5" = "google-antigravity/claude-opus-5-5"}` | Maps primary model IDs to an alternate provider model tried once on the first worker outage before backoff waits begin. |

### `[limits]`

Concurrency, backoff, canary retention, and mutation testing budgets. All integer limits must be positive integers (`>= 1`).

| Key | Type | Default | Description |
|---|---|---|---|
| `max_active_missions` | integer (`>= 1`) | `3` | Maximum concurrent `active` or `activating` `agentic@<id>.service` units before `new_mission` and `continue_mission` refuse new work. |
| `outage_waits_minutes` | non-empty list of integers (`>= 1`) | `[5, 10, 20, 40, 60]` | Backoff schedule in minutes between model outage retries; the last entry repeats once reached. |
| `canary_max_age_days` | integer (`>= 1`) | `7` | Retention window in days (in `display.timezone`) before `agentic honesty-canary` deletes old `canary-<yyyymmdd>` missions. |
| `mutation_max_mutants` | integer (`>= 1`) | `6` | Maximum single-point AST mutants sampled per mission when scoring check strength. |
| `mutation_budget_seconds` | integer (`>= 1`) | `600` | Total wall-clock budget in seconds (10 minutes by default) for one mutation score run; unstarted mutants after the deadline are recorded as `skipped`. |

### `[display]`

Human-facing output formatting. Ledger timestamps always remain in UTC ISO-8601 format (`YYYY-MM-DDTHH:MM:SS.mmmZ`).

| Key | Type | Default | Description |
|---|---|---|---|
| `timezone` | string (IANA time zone) | `"UTC"` | Time zone used by `display.display_time` and `display.zone_label` in reports, Telegram cards, and CLI output (validated via `zoneinfo.ZoneInfo`, e.g. `"UTC"` or `"Asia/Seoul"`). |
| `language` | string (`"en"` or `"ko"`) | `"en"` | Message catalog in `agentic/i18n/` used for reports, briefings, Telegram cards, CLI messages, and health cards. |

### `[telegram]`

Telegram bot service supervision.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | When `false`, `install.sh` does not enable `agentic-telegram.service` and `agentic health` does not monitor or restart `agentic-telegram.service`. |

---

## Precedence: `AGENTIC_CONFIG` and Environment Overrides

Configuration values are resolved in three layers (highest precedence first):

1. **Per-setting environment variables** (read on every `config.load()` call; used by tests and one-off CLI invocations):
   - `AGENTIC_STATE` overrides `[paths].state`.
   - `AGENTIC_WORKSPACES` overrides `[paths].workspaces`.
   - `AGENTIC_INBOX` and `AGENTIC_OUTBOX` override `<[paths].spool>/inbox` and `<[paths].spool>/outbox`.
   - `AGENTIC_INBOX_LOCK` overrides `/run/agentic-inbox.lock`.
   - `AGENTIC_BOT_USER` overrides `[accounts].bot_user`.
   - `AGENTIC_CHECK_MODE` (`systemd` or `local`, default `systemd`).
   - `AGENTIC_WORKER_CMD` and `AGENTIC_PLANNER_CMD` (JSON argv lists replacing the systemd-wrapped `omp` prefix in tests).
   - `AGENTIC_TELEGRAM_CONFIG` (default `/etc/agentic/telegram.json`), `AGENTIC_BOT_STATE` (default `/var/lib/agbot`), and `AGENTIC_TELEGRAM_API` (default `https://api.telegram.org`) on the bot side.
2. **Configuration file** at `AGENTIC_CONFIG` (if set in the environment) or `/etc/agentic/agentic.toml`.
   - If `AGENTIC_CONFIG` is set and the file does not exist, startup fails with exit code `1`.
   - If `AGENTIC_CONFIG` is unset and `/etc/agentic/agentic.toml` does not exist, built-in defaults apply.
3. **Built-in defaults** in `agentic/config.py`.

---

## Example `/etc/agentic/agentic.toml`

```toml
[accounts]
interactive_user = "agent"
worker_user = "agwork"
planner_user = "agplan"
bot_user = "agbot"

[paths]
state = "/var/lib/agentic"
workspaces = "/var/lib/agwork/missions"
spool = "/var/spool/agentic"
worker_omp = "/opt/omp/omp"
planner_omp = "/opt/omp/omp"
worker_overlay = "/etc/agentic/omp-overlay.yml"
guidance_sync = "/usr/local/sbin/agentic-guidance-sync"
guidance_check = "/usr/local/sbin/agentic-guidance-check"

[models]
planner_steps = { brief = ["google-antigravity/gemini-4-argon:high", "anthropic/claude-opus-5-5:high"], design = ["anthropic/claude-fable-5-1:high", "anthropic/claude-opus-5-5:high"], checks = ["anthropic/claude-opus-5-5:high", "anthropic/claude-fable-5-1:high"], critique = ["google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"] }
review = ["google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"]
worker_ladder = [{ model = "anthropic/claude-sonnet-5-5", thinking = "high", iteration_minutes = 45 }, { model = "anthropic/claude-opus-5-5", thinking = "high", iteration_minutes = 45 }, { model = "anthropic/claude-fable-5-1", thinking = "high", iteration_minutes = 90 }]
alternate_provider = { "anthropic/claude-sonnet-5-5" = "google-antigravity/claude-sonnet-5-5", "anthropic/claude-opus-5-5" = "google-antigravity/claude-opus-5-5" }

[limits]
max_active_missions = 3
outage_waits_minutes = [5, 10, 20, 40, 60]
canary_max_age_days = 7
mutation_max_mutants = 6
mutation_budget_seconds = 600

[display]
timezone = "UTC"
language = "en"

[telegram]
enabled = true
```
