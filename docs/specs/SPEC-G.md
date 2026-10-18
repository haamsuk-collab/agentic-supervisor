# agentic supervisor — Stage G specification: public release (MIT)

Stages B–F are built, installed and in daily use on one server. Stage G turns the repository into a
project anyone can install and trust. Nothing from Stages B–F may be weakened. Record every open
choice in DECISIONS.md. The rules still hold: Python 3.12 standard library only, no `shell=True`,
and no network or real model in tests.

## 1. One configuration file instead of host-specific constants

- Load an optional `/etc/agentic/agentic.toml` (override with `AGENTIC_CONFIG`) with `tomllib`. Every
  value has the current behaviour as its default, so the running server keeps working with no file.
- Move these into it, with validation and a clear error that names the key:
  - **accounts:** `interactive_user` (agent), `worker_user` (agwork), `planner_user` (agplan),
    `bot_user` (agbot);
  - **paths:** state, workspaces, spool, the omp binary for workers and planners, the worker overlay,
    the guidance sync and check commands;
  - **models:** planner step lists, review models, the default worker ladder, the alternate-provider
    table;
  - **limits:** max active missions, the outage backoff, the canary and mutation budgets;
  - **display:** `timezone` (default `UTC`) and `language` (`en` or `ko`; default `en`).
- `agentic config check` prints the effective configuration, never secrets, and exits 1 on an invalid
  file.

## 2. Two languages for everything a person reads

- Move every user-facing string (report, briefing, cards, Telegram replies, health card, CLI messages)
  into `agentic/i18n/en.py` and `agentic/i18n/ko.py`. Both expose the same keys.
- The Korean texts stay exactly as they are now.
- The English texts follow the same rules as the Korean ones (TEMPLATE-RULES.md, applied to English):
  conclusion first, no filler, numbers with their conditions.
- A test asserts that both catalogs have the same keys and that no user-facing string is hard-coded
  outside `i18n/`.
- The banned-phrase test covers both languages, using the English list from
  `guidance/user/rules/english-writing.md`.

## 3. Repository layout for the public

```
agentic/            the package (unchanged structure)
tests/
deploy/             systemd units, launchers, sudoers and fstab examples
guidance/           the omp guidance: user/ (RULES.md, AGENTS.md, WATCHDOG.md, rules/, skills/, extensions/, agents/), project/
docs/               ARCHITECTURE.md, HONESTY.md, SECURITY.md, CONFIG.md, OPERATIONS.md, specs/ (SPEC*.md as design history)
install.sh  uninstall.sh
README.md (English)  README.ko.md (Korean)
LICENSE (MIT)  NOTICE  CHANGELOG.md  DECISIONS.md
.github/workflows/test.yml
```

- **guidance/:** The operator will copy the current guidance tree into `guidance/` (section 7). It
  includes `skills/insane-search`, which is MIT-licensed by its own author: keep its LICENSE file
  and list it in NOTICE.
- **NOTICE:** credit the writing-guidance sources (epoko77-ai/im-not-ai, Nanako0129/sepia,
  blader/humanizer; all MIT) and fivetaku/insane-search, each with its URL and license.
- **LICENSE:** MIT, `Copyright (c) 2026 haamsuk-collab`.

## 4. install.sh and uninstall.sh

- `install.sh` targets systemd Linux with Python 3.12 (RHEL/Rocky 8–9 and Debian/Ubuntu). It is
  idempotent, prints each step, and stops at the first error. It must:
  - check its prerequisites: root, systemd, `/usr/bin/python3.12` (or `--python`), git, and an omp
    binary given by `--omp /path/to/omp`;
  - create the four accounts with the documented homes and modes;
  - create the state, spool and workspace folders;
  - install the package to `/opt/agentic`, the launchers, the systemd units and the timers;
  - write a default `agentic.toml` from the flags `--timezone`, `--language` and `--telegram`;
  - set up `/proc` hidepid with the procview group, behind `--hidepid` (default on);
  - set up interactive-agent sudo, behind `--agent-sudo` (default off; documented as the owner's
    choice);
  - sync the guidance into the interactive user's omp folder and lock it (chattr +i);
  - print the remaining manual step: the omp auth gateway, which needs the owner's own provider
    logins. Link docs/OPERATIONS.md.
- `uninstall.sh` stops and removes the units, the launchers and `/opt/agentic`. It keeps the state
  and the ledgers unless `--purge` is given.
- `--dry-run` prints every action without changing anything.
- The tests run `install.sh --dry-run` against a fake root (`AGENTIC_ROOT_PREFIX`) and check the
  action list.

## 5. Documentation (English and Korean README, English docs)

- **README:**
  - what it is in three sentences;
  - the honesty guarantee ("done means a hidden check, run by the supervisor, exited 0");
  - a diagram of the flow (Telegram → planner → worker → hidden checks → canary → mutation →
    review → report);
  - requirements;
  - quick install;
  - a first mission;
  - what it does NOT guarantee: checks prove only what they test, and the model providers are
    outside its control.
- **HONESTY.md:** each mechanism and the attack it stops:
  - red baseline, hidden checks, separate accounts and hidepid;
  - canary, mutation score, cheat review;
  - the append-only hash-chained ledger;
  - the deterministic briefings;
  - the honesty canary.
- **SECURITY.md:** the threat model (a dishonest or prompt-injected worker; a compromised Telegram
  account; a curious interactive agent), what is out of scope, and how to report issues.
- **CONFIG.md:** every key, its type, its default, and an example.
- **OPERATIONS.md:** the auth gateway setup, Telegram bot creation, the daily jobs, backups, upgrades
  and migration.

## 6. Hygiene gates

- **Repository scan test.** Fail on any of:
  - host names or IPs;
  - Telegram tokens (`\d{8,10}:[A-Za-z0-9_-]{35}`) or numeric chat ids in config examples;
  - API key patterns;
  - absolute paths to a person's home other than the documented account homes;
  - the deployment's own host name, IP prefix and e-mail (kept outside the repository).
- **CI:** `.github/workflows/test.yml` runs `make test` on ubuntu-latest with Python 3.12 and the
  repository scan.
- **CHANGELOG.md:** the stages B–G as release 1.0.0.

## 7. Operator steps (not code)

The operator copies the guidance into `guidance/`, creates the GitHub repository, and reviews the
scan before publishing. Tests must not depend on these steps.

## 8. Done means

- `make test` passes (report only the summary).
- `install.sh --dry-run` works.
- Both READMEs and all docs exist.
- The scan test passes.
- Commit with a clear message.
- SPEC files are not modified; copies under `docs/specs/` are made by the operator.
