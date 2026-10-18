"""Stub worker for tests. Invoked like the real worker: <opts> --model M --thinking T --max-time Nm PROMPT.

Scenario (environment):
  AGENTIC_STUB_DIR      directory outside the worker folder holding calls.jsonl (one line per call)
  AGENTIC_STUB_PLAN     JSON list; call n plays plan[min(n-1, len-1)]. An entry may hold
                        "write": {path: text}, "delete": [path], "say": final text, "sleep": seconds,
                        "stderr": text written to stderr, "exit": exit code (default 0)
  AGENTIC_STUB_KILL_AT  if set to n: call n kills its parent (the supervisor) with SIGKILL, once
"""

import json
import os
import signal
import sys
import time
from pathlib import Path


def main() -> None:
    argv = sys.argv[1:]
    opts, i = {}, 0
    while argv[i].startswith("--"):
        opts[argv[i][2:]] = argv[i + 1]
        i += 2
    prompt = argv[i]

    stub_dir = Path(os.environ["AGENTIC_STUB_DIR"])
    plan = json.loads(os.environ["AGENTIC_STUB_PLAN"])
    calls = stub_dir / "calls.jsonl"
    n = (len(calls.read_text().splitlines()) if calls.exists() else 0) + 1

    kill_at, marker = os.environ.get("AGENTIC_STUB_KILL_AT"), stub_dir / "killed"
    if kill_at and int(kill_at) == n and not marker.exists():
        marker.touch()
        os.kill(os.getppid(), signal.SIGKILL)
        sys.exit(1)

    stdin_text = sys.stdin.read()
    record = {
        "n": n, "model": opts["model"], "thinking": opts["thinking"], "max_time": opts["max-time"],
        "prompt": prompt, "cwd": os.getcwd(), "stdin_len": len(stdin_text),
        "stdin_devnull": os.path.samestat(os.fstat(0), os.stat("/dev/null")),
    }
    with calls.open("a") as handle:
        handle.write(json.dumps(record) + "\n")

    entry = plan[min(n - 1, len(plan) - 1)]
    time.sleep(entry.get("sleep", 0))
    for rel, text in entry.get("write", {}).items():
        Path(rel).write_text(text)
    for rel in entry.get("delete", []):
        Path(rel).unlink(missing_ok=True)
    print(entry.get("say", "I changed some things."))
    sys.stderr.write(entry.get("stderr", ""))
    sys.exit(entry.get("exit", 0))


main()
