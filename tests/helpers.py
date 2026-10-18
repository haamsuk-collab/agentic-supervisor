"""Shared test fixtures: isolated state, stub worker, file-based hidden checks."""

import contextlib
import io
import json
import os
import pwd
import shutil
import signal
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentic import cli, commands, config, loop
from agentic.ledger import Ledger


def _no_real_commands(argv, stdin_path=None):
    if list(argv[:2]) == ["chattr", "+a"]:
        # The supervisor makes every new ledger append-only (SPEC-F section 4); tests/test_ledger.py checks the call.
        return commands.CommandResult(0, "", "")
    raise AssertionError(f"a test tried to run a real system command: {argv}")


# Tests never run the real systemctl/runuser: a test that needs them installs a FakeCommands.
commands.run_command = _no_real_commands

# The suite was written for the installed server's display settings: Korean texts and KST times. Tests of the
# English catalog or another time zone patch these two attributes themselves.
config.LANGUAGE = "ko"
config.DISPLAY_TIMEZONE = "Asia/Seoul"


class FakeCommands:
    """Stand-in for commands.run_command: records every argv (and the stdin file of those that have one, in
    `stdin`) and answers with `respond(argv)` (default: exit 0, no output).
    Install with mock.patch("agentic.commands.run_command", fake)."""

    def __init__(self, respond=None):
        self.calls: list[list[str]] = []
        self.stdin: dict[int, str] = {}  # index in `calls` -> stdin_path
        self.respond = respond or (lambda argv: commands.CommandResult(0, "", ""))

    def __call__(self, argv, stdin_path=None):
        if stdin_path is not None:
            self.stdin[len(self.calls)] = stdin_path
        self.calls.append(list(argv))
        return self.respond(list(argv))


CURRENT_USER = pwd.getpwuid(os.getuid()).pw_name

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
STUB = TESTS_DIR / "stub_worker.py"
STUB_PLANNER = TESTS_DIR / "stub_planner.py"

# Each criterion cN passes when the worker folder holds cN.txt containing "ok".
# The script always prints "ALL TESTS PASSED": only the exit code may count.
CHECK_SCRIPT = """# SECRET_CONTENT_MARKER_{cid}
import sys
print("ALL TESTS PASSED")
try:
    text = open("{cid}.txt").read().strip()
except OSError:
    sys.exit(1)
sys.exit(0 if text == "ok" else 1)
"""


def file_check(cid: str) -> dict:
    name = f"checks/secret_check_{cid}.py"
    return {"argv": [sys.executable, name], "timeout": 30, "files": {name: CHECK_SCRIPT.format(cid=cid)}}


class TestTimeout(Exception):
    """A test ran past its time limit (for example an endless loop)."""


class TimedTestCase(unittest.TestCase):
    """Base class of every test: a test that exceeds TIMEOUT seconds fails instead of hanging.

    Uses SIGALRM, so it interrupts Python loops, sleeps and blocking waits in the main thread.
    Nested use keeps the outer timer: the previous timer is restored with its remaining time.
    """

    TIMEOUT = 60.0

    def run(self, result=None):
        def on_alarm(signum, frame):
            raise TestTimeout(f"{self.id()} exceeded its {self.TIMEOUT:g}s time limit")

        previous_handler = signal.signal(signal.SIGALRM, on_alarm)
        started = time.monotonic()
        previous_delay, _ = signal.setitimer(signal.ITIMER_REAL, self.TIMEOUT)
        try:
            return super().run(result)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_delay > 0:
                remaining = previous_delay - (time.monotonic() - started)
                signal.setitimer(signal.ITIMER_REAL, max(remaining, 0.001))


ALWAYS_PASSES = {"argv": [sys.executable, "-c", "pass"], "timeout": 30}


def make_mission(ids=("c1", "c2"), mission_id="demo", limits=None, ladder=None, checks=None) -> dict:
    checks = checks or {}
    mission = {
        "id": mission_id,
        "goal": "Build the demo.",
        "criteria": [{"id": cid, "text": f"Requirement {cid}", "check": checks.get(cid, file_check(cid))}
                     for cid in ids],
    }
    if limits is not None:
        mission["limits"] = limits
    if ladder is not None:
        mission["ladder"] = ladder
    return mission


def fix(*ids: str, say: str | None = None) -> dict:
    entry = {"write": {f"{cid}.txt": "ok" for cid in ids}}
    if say:
        entry["say"] = say
    return entry


def unfix(*ids: str) -> list[str]:
    return [f"{cid}.txt" for cid in ids]


NOTHING = {"say": "All done, everything works!"}


class StepClock:
    """A fake clock that advances a fixed step on every call."""

    def __init__(self, minutes: float):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(minutes=minutes)
        self.step = timedelta(minutes=minutes)

    def __call__(self) -> datetime:
        self.now += self.step
        return self.now


class Crash(BaseException):
    """Simulates the process being killed right after a ledger record became durable."""


class SupervisorTestCase(TimedTestCase):
    def setUp(self):
        self.new_world()

    def new_world(self) -> config.Config:
        """Fresh state/workspace/stub directories; environment points at them."""
        tmp = tempfile.mkdtemp(prefix="agentic-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.stub_dir = Path(tmp) / "stub"
        self.stub_dir.mkdir()
        # Supervisors started as subprocesses cannot see the in-process guard: a fake `chattr` (exit 0) comes
        # first on their PATH, so no test runs the real one (SPEC-F section 4 calls it on every new ledger).
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        (fake_bin / "chattr").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "chattr").chmod(0o755)
        patcher = mock.patch.dict(os.environ, {
            "AGENTIC_STATE": str(Path(tmp) / "state"),
            "AGENTIC_WORKSPACES": str(Path(tmp) / "workspaces"),
            "AGENTIC_CHECK_MODE": "local",
            "AGENTIC_WORKER_CMD": json.dumps([sys.executable, str(STUB)]),
            "AGENTIC_STUB_DIR": str(self.stub_dir),
            "AGENTIC_STUB_PLAN": json.dumps([NOTHING]),
            "AGENTIC_PLANNER_CMD": json.dumps([sys.executable, str(STUB_PLANNER)]),
            "AGENTIC_STUB_PLANNER": "{}",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            # Spool folders are not created: notifications are skipped unless a test creates the outbox.
            "AGENTIC_INBOX": str(Path(tmp) / "spool" / "inbox"),
            "AGENTIC_OUTBOX": str(Path(tmp) / "spool" / "outbox"),
            "AGENTIC_INBOX_LOCK": str(Path(tmp) / "inbox.lock"),
            "AGENTIC_BOT_USER": CURRENT_USER,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENTIC_STUB_KILL_AT", None)
        self.cfg = config.load()
        return self.cfg

    def install(self, mission: dict) -> str:
        path = self.cfg.mission_file(mission["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mission), encoding="utf-8")
        return mission["id"]

    def play(self, *plan: dict) -> None:
        os.environ["AGENTIC_STUB_PLAN"] = json.dumps(list(plan))

    def plan_script(self, **cfg) -> None:
        """Configure the stub planner (see stub_planner.py)."""
        os.environ["AGENTIC_STUB_PLANNER"] = json.dumps(cfg)

    def planner_calls(self, step: str | None = None) -> list[dict]:
        path = self.stub_dir / "planner_calls.jsonl"
        found = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [c for c in found if step is None or c["step"] == step]

    def cli(self, *args: str) -> int:
        """`python -m agentic ...` in-process; the output is kept in self.out / self.err."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def new(self, request: str = "날씨를 알려주는 명령줄 프로그램을 만들어 주세요.", mission_id: str = "demo") -> int:
        """`python -m agentic new` with the request written to a file."""
        path = self.stub_dir / "request.txt"
        path.write_text(request, encoding="utf-8")
        return self.cli("new", str(path), "--id", mission_id)

    def calls(self) -> list[dict]:
        path = self.stub_dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def records(self, mission_id="demo") -> list[dict]:
        return Ledger(self.cfg.ledger_file(mission_id)).read()

    def types(self, mission_id="demo") -> list[str]:
        return [r["type"] for r in self.records(mission_id)]

    def report_text(self, mission_id="demo") -> str:
        return self.cfg.report_file(mission_id).read_text(encoding="utf-8")

    def run_mission(self, mission: dict | None = None, **kwargs) -> str:
        mission_id = self.install(mission or make_mission())
        return loop.run(mission_id, self.cfg, **kwargs)


ARGON, OPUS, FABLE, SONNET = ("google-antigravity/gemini-4-argon", "anthropic/claude-opus-5-5",
                              "anthropic/claude-fable-5-1", "anthropic/claude-sonnet-5-5")


class PlannedTestCase(SupervisorTestCase):
    """Missions planned from a request with the stub planner (see stub_planner.py)."""

    def steps(self, step=None) -> list[dict]:
        return [r["data"] for r in self.records() if r["type"] == "plan_step"
                and (step is None or r["data"]["step"] == step)]

    def mission_json(self) -> dict:
        return json.loads(self.cfg.mission_file("demo").read_text(encoding="utf-8"))


def _block(report: str, start: str, ends_at) -> list[str]:
    """Lines after the first line starting with `start`, up to the next line for which `ends_at(line)` is true."""
    lines, inside = [], False
    for line in report.splitlines():
        if inside and ends_at(line):
            break
        if inside:
            lines.append(line)
        elif line.startswith(start):
            inside = True
    return lines


def results(report: str, mark: str) -> list[str]:
    """Lines under the '### <mark>' heading of '## 4. 결과' (mark is ✅, ❌ or ⚠️), up to the next heading."""
    inside4 = _block(report, "## 4. 결과", lambda line: line.startswith("## "))
    return _block("\n".join(inside4), f"### {mark}", lambda line: line.startswith("#"))


def notices(report: str) -> list[str]:
    """Lines of '## 5. 알아 두실 것'."""
    return _block(report, "## 5. ", lambda line: line.startswith("#"))


def ai_notes(report: str) -> list[str]:
    """Lines of '## 6. AI가 정한 것'."""
    return _block(report, "## 6. ", lambda line: line.startswith("#"))


def ledger_summary(records: list[dict]) -> list[tuple]:
    """The deterministic content of a ledger: no timestamps, hashes, outputs or durations."""
    out = []
    for rec in records:
        d = rec["data"]
        if rec["type"] == "check_result":
            out.append((rec["type"], d["iteration"], d["criterion"], d["passed"]))
        elif rec["type"] == "iteration_worker":
            out.append((rec["type"], d["iteration"], d["step"], d["model"], tuple(d["active"]), d["note"]))
        elif rec["type"] == "escalated":
            out.append((rec["type"], d["from_step"], d["to_step"]))
        elif rec["type"] == "gave_up":
            out.append((rec["type"], tuple(d["criteria"]), d["attempts"]))
        elif rec["type"] == "mission_finished":
            out.append((rec["type"], d["status"], d["iterations"]))
        elif rec["type"] == "baseline":
            out.append((rec["type"], tuple(d["invalid"])))
        elif rec["type"] == "plan_step":
            out.append((rec["type"], d["step"], d["round"], d["attempt"], d["model"], d["ok"],
                        tuple(sorted(d.get("empty_folder", {}).items()))))
        elif rec["type"] == "plan_finished":
            out.append((rec["type"], tuple((x["id"], x["problem"]) for x in d["dropped"])))
        elif rec["type"] == "review":
            out.append((rec["type"], d["iteration"], d["ok"],
                        tuple((f["criterion"], f["file"], f["verified"]) for f in d["findings"])))
        elif rec["type"] == "review_veto":
            out.append((rec["type"], d["iteration"], tuple((f["criterion"], f["problem"]) for f in d["findings"])))
        else:
            out.append((rec["type"],))
    return out


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie (a zombie is dead, just not yet reaped)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def wait_until(predicate, timeout: float = 20.0, what: str = "condition"):
    """Poll until predicate() is truthy; fail (never hang) after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(0.05)
