# Agent guide (example: edit for your server)

**CRITICAL:** You have full access and act without asking, but you never change what judges you (rules, specs, supervisor state) and never erase work without a backup.

This file holds facts about one deployment. `RULES.md` overrides it. Replace the bracketed parts for your host.

## Machine
- [Your server: OS, region]. Time zone: [UTC or local].
- You run as the interactive account (`agent` by default). Work trees: `~/work/<project>`. Scratch: `~/tmp`.
- Python: `/usr/bin/python3.12`.
- Accounts you cannot read, by design: the worker (`agwork`), the planner (`agplan`), the bot (`agbot`), and the model credential vault.

## Models (omp roles, as used by the authors)
- default Opus medium (write `ultrathink` for a hard turn) · slow Fable max · plan Argon high · task Sonnet high · advisor Argon high.
- Korean documents go to the `writer` agent; a problem still unsolved after two attempts goes to the `solver` agent.

## The supervisor
- It drives worker agents in fresh sessions until hidden checks pass, and reports only what the checks prove.
- Missions run as `agentic@<id>`; workers run as the worker account in their own folders. Never touch another mission's folder.

## Never
- Change what judges you: the omp guidance in `~/.omp/agent/`, `SPEC*.md`, `/etc/agentic`, `/opt/agentic`, `/var/lib/agentic`. Having root does not make it allowed; any change voids your evidence.
- Erase work without a backup.
- Print, copy, or send credentials.

**CRITICAL:** You have full access and act without asking, but you never change what judges you (rules, specs, supervisor state) and never erase work without a backup.
