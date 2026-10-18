# Agent guide (example: edit for your server)

**CRITICAL:** You have full root and act without asking, but you never change what judges you (rules, specs, supervisor state) and never erase work without a backup.

Version: <date>.<n> · Source: /etc/agentic/guidance (root-owned; edits here are overwritten)

Facts about this machine and how work is organised here. `RULES.md` overrides this file. Replace every "example" line with your setup.

## Machine (example: edit for your server)
- Example: a headless Linux server (`<distribution> <version>`), time zone `<your zone>`.
- You run as `agent` with full root through `sudo` (in omp, call the bash tool with `pty: true`; sessions without a terminal cannot elevate). Do the work yourself; never ask the user to approve or type anything. Work trees: `~/work/<project>`. Scratch: `~/tmp`.
- Example tools: Python `/usr/bin/python3.12` (omp `eval` uses it), Git, Node, a browser with a virtual display. List your versions.
- Example: the user reaches you over SSH from a phone. Keep replies short for a narrow screen.
- Other accounts you cannot read, by design: `agplan` (writes hidden checks), `agbot` (Telegram bot), `ompauth` (model credentials). `/proc` shows only your own processes.

## Models (example: edit for your omp roles)
- Example: the model and effort of each omp role (default, slow, plan, task, advisor) on your server.
- Route by the kind of reasoning: deep or long-range tracing stays on the default model; Korean documents go to the `writer` agent; routine and quick work goes to `task`. When a problem is still unsolved after two attempts, hand it to the `solver` agent: give it the goal, the constraints, the failing output, and what was tried.
- For a hard turn write `ultrathink`. For independent parallel work use `/vibe` or `orchestrate`. For a long objective use `/goal`.

## The supervisor (`~/work/agentic-supervisor`)
- It drives worker agents in fresh sessions until hidden checks pass, and reports only what the checks prove.
- Specs are root-owned: `docs/specs/SPEC*.md`. Read them; never edit them.
- The user and root operator review every commit before it is installed. Commit only when the user asks.
- Missions run under root as `agentic@<id>`; workers run as `agent` in `/home/agent/missions/<id>/work`. Never touch another mission's folder.

## Never
- Change what judges you: `~/.omp/agent/` rules, skills and extensions, `SPEC*.md`, `/etc/agentic`, `/opt/agentic`, `/var/lib/agentic`, `/var/lib/agplan`. Having root does not make it allowed; any change voids your evidence.
- Erase work without a backup: before `git reset --hard`, `git clean`, a forced push, or a large delete, save a copy yourself (`git stash -u`, a backup branch, or `cp -a`).
- Print, copy, or send credentials (`~/.omp/agent/models.yml` holds a gateway token).

**CRITICAL:** You have full root and act without asking, but you never change what judges you (rules, specs, supervisor state) and never erase work without a backup.
