# agentic supervisor

`agentic-supervisor` runs an autonomous coding agent (`omp`) in fresh, memoryless iterations until every hidden acceptance check for a mission passes. Each iteration runs in an isolated Linux account and `systemd` sandbox that cannot read the checks, the supervisor state, or other users' processes, while the supervisor records every step in an append-only, hash-chained ledger. When the mission finishes, the supervisor renders a deterministic report and Telegram briefing from the ledger alone, without calling a language model.

## Honesty Guarantee

In `agentic-supervisor`, **done means a hidden check, run by the supervisor, exited 0**. The worker's claims, commit messages, and notes are never treated as evidence. Before a criterion counts as achieved, its check must fail on an empty folder at the baseline, exit `0` on a snapshot of the worker's output, fail again on a canary copy where the worker's modified files are truncated to zero bytes, survive mutation testing (or trigger automatic check strengthening), and pass a cross-vendor cheat review where every cited shortcut is verified verbatim in the file.

```mermaid
flowchart LR
    TG["Telegram / CLI Request"] --> PL["Planner (agplan)<br/>brief → design → checks → critique"]
    PL --> WK["Worker (agwork)<br/>fresh omp session per iteration"]
    WK --> HC["Hidden Checks<br/>DynamicUser sandbox on snapshot"]
    HC -->|exit != 0| WK
    HC -->|all exit 0| CN["Canary<br/>truncate changed files to 0 B"]
    CN -->|still passes: canary_failed| WK
    CN -->|fails as required| MU["Mutation Score<br/>up to 6 AST mutants / 10 min"]
    MU -->|"< 50% killed (n >= 3): strengthen checks"| HC
    MU -->|holds| RV["Cheat Review (agplan)<br/>cross-vendor verbatim quote check"]
    RV -->|verified veto (max 2)| WK
    RV -->|clean| RP["Report & Telegram Card<br/>deterministic briefing from ledger"]
```

## Requirements

- **OS:** `systemd`-based Linux (RHEL/Rocky Linux 8–9 or Debian/Ubuntu)
- **Privileges:** `root` for installation, service management, and supervisor execution
- **Runtime:** `/usr/bin/python3.12` (Python 3.12 standard library only; no third-party Python packages) and `git`
- **Agent binary:** an `omp` executable (pinned by the installer at `/opt/omp/omp`)
- **Model access:** the owner's own model-provider logins served through `omp-auth-gateway` on `127.0.0.1:4000` with separate client tokens for `agwork` and `agplan`

## Quick Install

Run `install.sh` as `root` with the path to your `omp` binary, then validate the configuration:

```bash
sudo ./install.sh --omp /path/to/omp [--timezone Asia/Seoul --language ko --telegram]
agentic config check
```

By default, `install.sh` configures `timezone = "UTC"` and `language = "en"` in `/etc/agentic/agentic.toml`, creates the isolated system accounts (`agwork`, `agplan`, `agbot`), installs `/opt/agentic` and the `systemd` units, mounts `/proc` with `hidepid=2` (`procview` group), and locks the interactive user's guidance files (`/home/agent/.omp/agent`) with `chattr +i`. Servers upgraded from earlier stages that used Korean output and KST timestamps should pass `--timezone Asia/Seoul --language ko` (or set both under `[display]` in `/etc/agentic/agentic.toml`). After `install.sh` completes, configure the `omp-auth-gateway` client tokens as described in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Running Your First Mission

Start a mission from a plain-language request file (UTF-8, 10 to 20,480 bytes) as `root`, or send the request as a plain text message to your private Telegram bot and press the confirmation button:

```bash
sudo agentic mission new request.md
sudo agentic mission status <id>
sudo agentic report <id>
```

`agentic mission status <id>` prints the unit state, the last 5 ledger events, and the rendered report (`<state>/missions/<id>/report.md`). `agentic report <id>` regenerates `report.md` directly from the hash-chained ledger, and `agentic verify <id>` verifies the ledger's SHA-256 hash chain and the proofs that bind each pass to the final check snapshot, checks for the `chattr +a` append-only inode flag, and says whether the worker folder is unchanged, changed (up to 10 paths) or missing since that snapshot.

## Testing and Hygiene Scan

Run the unit test suite and the repository hygiene scan with Python 3.12:

```bash
make test
/usr/bin/python3.12 tests/repo_scan.py
```

`make test` runs all unit tests offline using local check mode, stub worker and planner processes (`tests/stub_worker.py`, `tests/stub_planner.py`), and an in-process fake Telegram server on `127.0.0.1` (60-second timeout per test). `python3.12 tests/repo_scan.py` scans tracked files for host names, non-loopback IPv4 addresses (not inside the vendored folders credited in `NOTICE`), Telegram tokens, numeric chat IDs, API keys, and personal home paths outside `/home/agent`, and scans commit author names and emails for host names and banned strings.

## What It Does Not Guarantee

- **Checks prove only what they test:** a passing check guarantees that its command exited `0` on the snapshot, failed on the emptied canary copy, and survived the sampled AST mutants and cheat review; it does not prove absence of bugs outside those tests.
- **Model providers are outside its control:** model availability, rate limits, output quality, and data retention at external model providers depend on the providers configured behind `omp-auth-gateway`.
- **Root and host kernel are trusted:** `root` owns the state directory (`/var/lib/agentic`), the hidden checks, and the ledger; a malicious `root` user (or an interactive account granted `NOPASSWD: ALL` sudo via `--agent-sudo`) can read checks or remove `chattr +a`.
- **AI notes are unverified:** planner assumptions and lines written by the worker in `DECISIONS.md` are isolated in the unverified `AI notes` section of the report and are never checked by the supervisor.

## Documentation and Project Files

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — package modules, accounts, planning and iteration data flow, ledger records, spool protocol, `systemd` units, and `i18n`
- [`docs/HONESTY.md`](docs/HONESTY.md) — every anti-cheating mechanism, the attack it blocks, how to observe it, and its limits
- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model, account boundaries, out-of-scope threats, and vulnerability reporting
- [`docs/CONFIG.md`](docs/CONFIG.md) — `/etc/agentic/agentic.toml` sections, keys, types, defaults, and environment overrides
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — `omp-auth-gateway` setup, Telegram bot setup, daily canary and health jobs, backups, upgrades, and workspace migration
- [`docs/specs/`](docs/specs/) — stage specifications (`SPEC.md` through `SPEC-G.md`) preserved as design history
- [`DECISIONS.md`](DECISIONS.md) — implementation choices recorded across Stages B–G
- [`README.ko.md`](README.ko.md) — Korean overview and quick-start guide
- [`CHANGELOG.md`](CHANGELOG.md) — release history for version `1.0.0` (Stages B–G)
- [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE) — MIT license and third-party attributions
