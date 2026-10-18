# Changelog

## Unreleased

### Evidence bound to what was checked
- `mission_started` records the mission's `contract_sha256`; a resumed run refuses an edited mission file, and
  `checks_update` records the old and new contract.
- Every `check_result` records its snapshot digest, check and contract hashes, and the sha256 and size of the
  full stdout and stderr. Checks of one iteration that saw different snapshots run once more, then the mission
  stops as `stopped_unstable`.
- `mission_finished` lists the proofs behind every pass and the final snapshot; `verify` re-checks them, reports
  count passes only through proofs, and `agentic verify <id>` compares the worker folder with the final snapshot.
  Old ledgers verify as unbound.
- Concurrent missions no longer report each other's throw-away agent folders as worker leftovers.
- The release scan skips only the IP rule inside vendored folders credited in NOTICE, reports a git checkout
  with no files to scan, and checks commit author names and emails.
- Spool items are named `<20-digit time_ns sequence>-<random>` and read in that order, so items written in the
  same nanosecond or with the same mtime keep their order.
- The release scan allows link-local addresses (169.254.0.0/16).
- `guidance/user` and `guidance/project` are published as generic examples, without the vendored skill's tests;
  the stage specs are published under `docs/specs/`.

### Stage H: closing the cheat paths found in the t1 audits
- Checks run with `/usr/bin/python3.12 -I`, a fixed `PATH` and an empty `HOME`; files that could hijack a check
  (stdlib-named modules, `conftest.py`, `sitecustomize.py`, `*.pth`) are removed from the snapshot and reported.
- A free mechanical filter (no assertion, `true` only, not isolated, vague wording) runs before the critique.
- Strengthened checks may never get weaker: old files kept or a named bug, no empty-folder pass, no lost mutant;
  every accepted change is recorded as `checks_update`, every refusal as `checks_update_rejected`.
- Every worker run gets a fresh temporary agent folder and no inherited session environment; leftovers are
  reported.
- Daily card: ledger anchors (`agentic verify --anchors`), a guardrail self-test and extension hash pinning.
- Crash-point tests kill the run after each kind of transition and compare the resumed result.
- Briefings and reports tell a check that passed then from one that passes now.

## 1.0.0 — 2026-10-18

The first public release. It collects the six stages the supervisor was built in; each stage's design is kept
in `docs/specs/` (SPEC.md is Stage B, SPEC-C.md to SPEC-G.md the later ones).

### Stage B: the loop, the ledger and hidden checks
- Drive a coding agent in fresh sessions until every acceptance check passes or a limit is reached.
- A criterion counts as done only when a check run by the supervisor exits 0; the agent's words never count.
- Checks are hidden from the worker and run on a snapshot copy, in a systemd sandbox in production.
- Red baseline: a check that already passes on the empty folder is marked `invalid_check`.
- Model ladder with escalation after stalls, oscillation detection, give-up and a mission time limit.
- Append-only, hash-chained JSON Lines ledger in UTC; every run resumes by replaying it.
- A report generated from the ledger only.

### Stage C: automatic planning and the cheat review
- `agentic new <request>`: a fixed pipeline of planner models (brief, design, checks, critique) turns a
  plain-language request into a mission with hidden checks, run as the separate `agplan` account.
- Mechanical vetoes: a check that passes on an empty folder is rejected; criteria still vetoed after two
  rounds are dropped and reported.
- A reviewer from another vendor reads the worker's files for special-casing; a finding counts only when its
  quote is found verbatim in the named file.

### Stage D: Telegram, model outages, operations
- A Telegram bot as the unprivileged `agbot` account, relaying through a root-read inbox and a root-written
  outbox; only the owner's private chat is served.
- Rate limits and provider errors are recorded as outages, tried once on an alternate provider, then waited out
  with backoff; they never count as a stall.
- The `mission` admin command, notifications, and the systemd units in `deploy/`.

### Stage E: decision briefings, result delivery, follow-ups
- One deterministic briefing model (no model writes it) behind `report.md` and every Telegram card: what
  happened, how much is achieved, what to do, which decisions are open, what to send next, what is left.
- HTML cards with strict escaping, `/files <id>` as a zip of the worker folder, and follow-up missions that
  continue from a finished one.
- Every time a person reads is shown in the display time zone; the ledger stays UTC.

### Stage F: worker isolation, proof that checks bite, daily operations
- Workers run as their own account `agwork` in a sandboxed unit that cannot read the state, the planner's
  home, the spool or the bot token.
- Canary: a pass counts only when the check fails on a copy where the worker's files are emptied.
- Mutation score: up to 6 deterministic mutants; a weak score makes the supervisor strengthen the checks once.
- Ledgers are made append-only (`chattr +a`); `agentic health`, the daily honesty canary and `agentic backup`.

### Stage G: public release
- One optional configuration file, `/etc/agentic/agentic.toml` (`AGENTIC_CONFIG`), for accounts, paths,
  models, limits, display and Telegram; `agentic config check`.
- Every text a person reads comes from `agentic/i18n/en.py` or `agentic/i18n/ko.py`; English is the default.
  Default time zone UTC. A server that used the Korean texts and KST sets `language = "ko"` and
  `timezone = "Asia/Seoul"` (docs/OPERATIONS.md).
- `install.sh` and `uninstall.sh` with `--dry-run`; the guidance tree under `guidance/`.
- README in English and Korean; docs for architecture, honesty, security, configuration and operations.
- A repository scan for host names, addresses, tokens, keys and personal paths, run by the tests and by CI.
- MIT license.
