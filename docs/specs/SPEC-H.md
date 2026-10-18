# agentic supervisor — Stage H specification: closing cheat paths found in the t1 audits

Stages B–G still apply. Stage H closes the cheat paths and blind spots that an older harness (t1)
proved in its own audits. Record every open choice in DECISIONS.md. Standard library only, no
`shell=True`, and no network or real model in tests.

## 1. A worker must not be able to hijack a check (highest priority)

A check runs inside a copy of the worker folder. If the worker plants a module with a standard-library
name (`json.py`, `subprocess.py`, `unittest.py`), or `conftest.py`, `sitecustomize.py`,
`usercustomize.py`, a `*.pth` file, `pytest.ini` / `tox.ini` / `setup.cfg` addopts, or a `bin/` shim
earlier on PATH, then a check that imports or runs those can be made to exit 0. The canary cannot
see this, because it empties those files and the check then fails for another reason.

- **Planner (checks step):** every Python check `argv` must start `/usr/bin/python3.12 -I`. Validation
  rejects any other form. Checks call the worker's code only through a subprocess or an explicit file
  path, never by importing from the work folder.
- **Snapshot hygiene, before every check run:**
  - Inside the snapshot only, delete or rename any worker file whose basename is a standard-library
    top-level module name (`sys.stdlib_module_names`), or one of `conftest.py`, `sitecustomize.py`,
    `usercustomize.py`, or any `*.pth`.
  - Record each such file in a `hygiene` ledger record.
  - The worker folder itself is untouched. The report lists the files under ⚠️ with
    `검사를 가로챌 수 있는 파일이라 검사 때 제외함`.
- **Environment:** check processes get a clean environment: fixed `PATH=/usr/bin:/bin`,
  `HOME=<snapshot>/.home`, and no `PYTHON*` variables.
- **Cheat review:** the reviewer prompt adds "files that shadow standard or test tooling".

## 2. Checks may never get weaker

- Every change to a mission's checks is recorded as `checks_update` with `{criterion, old_sha256,
  new_sha256, reason}`. Changes come from automatic strengthening (SPEC-F) and from any future planner
  change.
- **Ratchet, checked mechanically before the new checks are accepted:**
  1. every criterion still has a check;
  2. the old check's files are still present in the new check, unchanged, or a recorded reason names
     the bug in the old check and the critique accepted that reason;
  3. the new check still fails on an empty folder;
  4. the new check fails on every surviving mutant that the old check killed.
- A violation rejects the change. The old checks stay, recorded as `checks_update_rejected`.

## 3. A free filter before the critique

Before the Argon critique round, reject planner checks mechanically:
- a check whose script contains no assertion: no `assert`, no comparison that leads to `sys.exit(1)`,
  no `exit(` with a non-zero value;
- a check whose only command is `true`, or whose argv does not run Python with `-I` (section 1);
- a criterion text with vague words that no check can test: `적절히`, `잘`, `appropriately`,
  `properly`, `as needed`. The criterion goes back to the design step once.

Rejections go into the same veto rounds as the empty-folder veto. The Argon call then reviews only
what passed. Record the counts so token savings can be measured.

## 4. Every worker session starts truly fresh

- Each worker iteration gets a throw-away omp agent directory:
  - copy `models.yml` and `config.yml` from `/var/lib/agwork/.omp/agent` into a new temp folder;
  - point omp at it with `PI_CODING_AGENT_DIR`;
  - delete the folder after the run.
- After each run, classify anything omp wrote outside the worker folder and the temp agent folder. An
  unknown leftover gives a `notice` record and a health-card line.
- A test proves that the worker command never receives resume or continue flags, and never inherits
  session environment variables.

## 5. Context byte budgets enforced by tests

A repository test fails when any of these limits is exceeded:
- `guidance/user/RULES.md` 4,500 bytes;
- the workspace-state line 200 bytes;
- each TTSR rule body 1,500 bytes;
- each skill `description` 600 bytes;
- `guidance/user/AGENTS.md` 3,000 bytes;
- each `SKILL.md` 280 lines.

## 6. Off-box anchors and daily self-tests

- **Ledger anchors.** The daily health card lists, for every mission changed in the last 24 h, the
  last ledger seq and the first 12 hex digits of its head hash. The owner's Telegram history thereby
  becomes an off-server witness.
  - `agentic verify --anchors <file>` compares the ledgers against anchors pasted from Telegram.
- **Guardrail self-test.** The honesty-canary job also proves that the interactive agent's guards are
  live:
  - run the guardrails extension's tool_call handler on three forbidden actions and two allowed
    ones, using the real installed file through `node`, which is installed;
  - record the outcome;
  - give a 🚨 line when a forbidden action is allowed.
- **Extension pinning.** The sha256 of each installed extension is recorded at every health run. A
  change since the last run gives a health line.

## 7. Crash-point tests

Add a fault-injection hook to the ledger append path and the state transitions, active only in
tests. For each transition (plan step, worker record, check result, canary, mutation, review, veto,
finish), kill the run right after it and resume. Assert that the final ledger and report equal those
of an uninterrupted run, timestamps excepted.

## 8. "Passed then" vs "passes now"

- The briefing states when the evidence is from: `감독자 검사 기준 <KST>`.
- When a follow-up mission exists, the parent's report and `/status` mark its results as past
  (`이후 이어서 한 미션: <id>`).

## 9. Done means

- `make test` passes (summary only).
- DECISIONS.md records every choice.
- Commit with a clear message.
- SPEC files are not modified.
