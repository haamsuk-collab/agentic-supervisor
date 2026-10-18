import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from agentic import config, worker
from helpers import TimedTestCase

OMP = [  # SPEC-F section 1: the pinned omp binary, run with the worker account's home
    "env", "HOME=/var/lib/agwork", "/opt/omp/omp",
    "-p", "--no-session", "--no-rules", "--no-skills", "--no-extensions", "--no-title",
    "--config", "/etc/agentic/omp-overlay.yml",
]
OMP_STATE = [f"/var/lib/agwork/.omp/{rel}" for rel in (
    "logs", "run", "natives", "cache", "agent/cache", "agent/sessions",
    *(f"agent/{db}{suffix}" for db in ("agent.db", "models.db", "history.db", "skill-descriptions.db")
      for suffix in ("", "-wal", "-shm")),
    "stats.db", "stats.db-wal", "stats.db-shm")]


def sandbox(workdir: str) -> list[str]:
    """The SPEC-F section 1 unit properties, as "-p" arguments."""
    props = ["ProtectHome=tmpfs", f"BindPaths={workdir}", "BindReadOnlyPaths=/var/lib/agwork/.omp",
             "BindPaths=" + " ".join(f"-{path}" for path in OMP_STATE), "PrivateTmp=yes", "NoNewPrivileges=yes",
             "InaccessiblePaths=/var/lib/agentic /var/lib/agplan /var/spool/agentic /etc/agentic/telegram.json"]
    return [arg for prop in props for arg in ("-p", prop)]


def fake_agent_uid(uid: int = 4242):
    return mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=uid))


class DefaultWorkerCommandTests(TimedTestCase):
    def default_command(self, prompt="PROMPT", minutes=45, uid=4242):
        cfg = config.load({})
        self.assertIsNone(cfg.worker_cmd)
        with fake_agent_uid(uid):
            return worker.build_command(cfg, "m-x", "high", minutes, prompt, "/w/work")

    def test_default_command_is_a_one_off_sandboxed_systemd_unit_run_as_agwork(self):
        self.assertEqual(self.default_command(), [
            "systemd-run", "--uid=agwork", "--wait", "--pipe", "--collect", "--quiet",
            "-p", "KillMode=control-group", "-p", "TimeoutStopSec=10",
            "-p", "RuntimeMaxSec=3000", "-p", "WorkingDirectory=/w/work",
            "-p", "Slice=user-4242.slice", *sandbox("/w/work"),
            "--", *OMP,
            "--model", "m-x", "--thinking", "high", "--max-time", "45m", "PROMPT",
        ])

    def test_slice_is_named_after_the_uid_looked_up_for_the_worker_user(self):
        cfg = config.load({})
        with fake_agent_uid(1001) as getpwnam:
            cmd = worker.build_command(cfg, "m", "t", 1, "P", "/w")
        getpwnam.assert_called_once_with("agwork")
        self.assertIn("Slice=user-1001.slice", cmd)
        self.assertNotIn("Slice=system.slice", cmd)
        with fake_agent_uid(70000):
            self.assertIn("Slice=user-70000.slice", worker.build_command(cfg, "m", "t", 1, "P", "/w"))

    def test_missing_worker_user_is_a_clear_config_error(self):
        with mock.patch("agentic.worker.pwd.getpwnam", side_effect=KeyError("agwork")):
            with self.assertRaisesRegex(config.ConfigError, "'agwork' does not exist"):
                worker.build_command(config.load({}), "m", "t", 1, "P", "/w")

    def test_unit_runtime_limit_follows_the_ladder_step(self):
        for minutes, seconds in ((45, 3000), (90, 5700), (0.5, 330)):
            with self.subTest(minutes=minutes):
                self.assertIn(f"RuntimeMaxSec={seconds}", self.default_command(minutes=minutes))

    def test_prompt_stays_one_argument_whatever_it_contains(self):
        prompt = "line one\n`x` --model evil; rm -rf /"
        cmd = self.default_command(prompt)
        self.assertEqual(cmd[-1], prompt)
        self.assertEqual(cmd.count(prompt), 1)

    def test_dollars_are_doubled_in_every_argument_after_the_separator(self):
        cmd = self.default_command("keep ${HOME} and $$ and $(x) and 100%")
        self.assertEqual(cmd[-1], "keep $${HOME} and $$$$ and $$(x) and 100%")
        tail = cmd[cmd.index("--") + 1:]
        self.assertEqual(tail[:len(OMP)], OMP, "arguments without a $ are untouched")
        self.assertEqual(worker.systemd_escape("a$b"), "a$$b")
        self.assertEqual(worker.systemd_escape("no dollar, 50% %h"), "no dollar, 50% %h")

    def test_dollars_in_the_unit_properties_are_not_touched(self):
        with fake_agent_uid():
            cmd = worker.build_command(config.load({}), "m", "t", 1, "P", "/w/$x")
        self.assertIn("WorkingDirectory=/w/$x", cmd)

    def test_override_is_used_verbatim_without_escaping(self):
        stub = ["python3.12", "/tmp/stub.py"]
        cfg = config.load({"AGENTIC_WORKER_CMD": json.dumps(stub)})
        cmd = worker.build_command(cfg, "m", "t", 7, "cost ${X} $$", "/w")
        self.assertEqual(cmd, [*stub, "--model", "m", "--thinking", "t", "--max-time", "7m", "cost ${X} $$"])

    def test_bad_override_is_rejected(self):
        for bad in ("not json", "[]", '["ok", 1]', '"python"'):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.load({"AGENTIC_WORKER_CMD": bad})


class SystemdExpansionTests(TimedTestCase):
    """Runs the real systemd (user manager) when this machine has one; skipped otherwise."""

    def run_unit(self, argv):
        return subprocess.run(["systemd-run", "--user", "--wait", "--pipe", "--collect", "--quiet", "--", *argv],
                              capture_output=True, text=True, timeout=30)

    def setUp(self):
        try:
            probe = self.run_unit(["/bin/true"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f"no usable systemd-run --user: {exc}")
        if probe.returncode != 0:
            self.skipTest(f"no usable systemd user manager: {probe.stderr.strip()}")

    def test_prompt_reaches_the_program_unchanged_only_because_of_the_escaping(self):
        prompt = "pay ${X_SURELY_UNSET} or $$5 or ${HOME} or $HOME\nsecond line 100% %h"
        with fake_agent_uid():
            cmd = worker.build_command(config.load({}), "m", "t", 1, prompt, "/w")
        escaped_tail = cmd[cmd.index("--") + 1 + len(OMP):]
        original_tail = ["--model", "m", "--thinking", "t", "--max-time", "1m", prompt]
        dump = [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))"]

        escaped = self.run_unit([*dump, *escaped_tail])
        self.assertEqual(escaped.returncode, 0, escaped.stderr)
        self.assertEqual(json.loads(escaped.stdout), original_tail)

        # Control: without the escaping systemd does rewrite the arguments, so the check above means something.
        plain = self.run_unit([*dump, *original_tail])
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertNotEqual(json.loads(plain.stdout), original_tail)


if __name__ == "__main__":
    unittest.main()
