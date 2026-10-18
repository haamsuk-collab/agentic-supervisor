"""SPEC-H section 4: every worker session starts truly fresh."""

import io
import json
import os
import stat
import sys
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentic import cards, checks, commands, config, daily, health, loop, mission, worker
from agentic.ledger import Ledger
from helpers import FakeCommands, SupervisorTestCase, TimedTestCase, make_mission

NOW = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])

RESUME_FLAGS = frozenset({"--resume", "--continue", "-r", "-c", "--session", "--session-id"})


def _fake_health_respond(argv: list[str]) -> commands.CommandResult:
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(0, "active\n", "")
    if argv[:2] == ["systemctl", "--failed"]:
        return commands.CommandResult(0, "", "")
    if argv == [config.GUIDANCE_CHECK_CMD]:
        return commands.CommandResult(0, "", "")
    if argv == [config.OMP_BINARY, "--version"]:
        return commands.CommandResult(0, "omp 1.2.3\n", "")
    return commands.CommandResult(0, "", "")


class FreshAgentDirTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.worker_home = self.stub_dir / "worker-home"
        self.agent_src = self.worker_home / ".omp" / "agent"
        self.agent_src.mkdir(parents=True, mode=0o700)
        self.secret_token = "GW_SECRET_TOKEN_DO_NOT_LEAK_987654321"
        (self.agent_src / "models.yml").write_text(f"gatewayToken: {self.secret_token}\n", encoding="utf-8")
        (self.agent_src / "config.yml").write_text("retry:\n  modelFallback: false\n", encoding="utf-8")
        # Extra state in the source dir must never be copied into the fresh temp dir.
        (self.agent_src / "history.db").write_text("old-history", encoding="utf-8")
        (self.agent_src / "sessions").mkdir()
        (self.agent_src / "sessions" / "prev.jsonl").write_text("{}", encoding="utf-8")

    def test_copies_models_and_config_into_fresh_0700_temp_dir_and_removes_it_after_run(self):
        inspect_file = self.stub_dir / "inspected.json"
        script = (
            "import json, os, pathlib, stat, sys\n"
            "d = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])\n"
            "info = {\n"
            "    'dir': str(d),\n"
            "    'exists': d.is_dir(),\n"
            "    'mode': stat.S_IMODE(d.stat().st_mode),\n"
            "    'files': sorted(p.name for p in d.iterdir()),\n"
            "    'models': (d / 'models.yml').read_text(encoding='utf-8'),\n"
            "    'config': (d / 'config.yml').read_text(encoding='utf-8'),\n"
            "    'models_mode': stat.S_IMODE((d / 'models.yml').stat().st_mode),\n"
            "}\n"
            "(d / 'session-state.json').write_text('ephemeral', encoding='utf-8')\n"
            f"pathlib.Path({str(inspect_file)!r}).write_text(json.dumps(info), encoding='utf-8')\n"
        )
        workdir = self.cfg.workdir("demo")
        workdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load({
            **os.environ,
            "AGENTIC_WORKER_HOME": str(self.worker_home),
            "AGENTIC_WORKER_CMD": json.dumps([sys.executable, "-c", script]),
        })
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out_buf), mock.patch("sys.stderr", err_buf):
            res = worker.run_worker(cfg, "m", "high", 5, "PROMPT", workdir)
        self.assertEqual(res.exit_code, 0, res.stderr)
        data = json.loads(inspect_file.read_text(encoding="utf-8"))
        self.assertTrue(data["exists"])
        self.assertEqual(data["mode"], 0o700)
        self.assertEqual(data["files"], ["config.yml", "models.yml"])
        self.assertEqual(data["models"], f"gatewayToken: {self.secret_token}\n")
        self.assertEqual(data["config"], "retry:\n  modelFallback: false\n")
        self.assertEqual(data["models_mode"], 0o600)
        # Deleted after the run.
        self.assertFalse(Path(data["dir"]).exists())
        # Never printed or logged.
        self.assertNotIn(self.secret_token, out_buf.getvalue())
        self.assertNotIn(self.secret_token, err_buf.getvalue())
        self.assertNotIn(self.secret_token, res.note)
        self.assertNotIn(self.secret_token, res.stderr)

    def test_temp_agent_dir_is_removed_when_worker_exits_nonzero_or_raises(self):
        seen_dir_file = self.stub_dir / "seen_dir.txt"
        fail_script = (
            "import os, pathlib, sys\n"
            f"pathlib.Path({str(seen_dir_file)!r}).write_text(os.environ['PI_CODING_AGENT_DIR'], encoding='utf-8')\n"
            "sys.exit(17)\n"
        )
        workdir = self.cfg.workdir("demo")
        workdir.mkdir(parents=True, exist_ok=True)
        cfg = config.load({
            **os.environ,
            "AGENTIC_WORKER_HOME": str(self.worker_home),
            "AGENTIC_WORKER_CMD": json.dumps([sys.executable, "-c", fail_script]),
        })
        res = worker.run_worker(cfg, "m", "high", 5, "PROMPT", workdir)
        self.assertEqual(res.exit_code, 17)
        seen_dir = Path(seen_dir_file.read_text(encoding="utf-8"))
        self.assertFalse(seen_dir.exists())

        # Also removed if run_process itself raises an exception.
        captured_dirs: list[Path] = []

        def boom(argv, cwd, timeout, **kwargs):
            env = kwargs.get("env") or {}
            d = Path(env["PI_CODING_AGENT_DIR"])
            self.assertTrue(d.is_dir())
            captured_dirs.append(d)
            raise RuntimeError("simulated process failure")

        with mock.patch("agentic.worker.run_process", side_effect=boom):
            with self.assertRaisesRegex(RuntimeError, "simulated process failure"):
                worker.run_worker(cfg, "m", "high", 5, "PROMPT", workdir)
        self.assertEqual(len(captured_dirs), 1)
        self.assertFalse(captured_dirs[0].exists())

    def test_each_iteration_gets_a_distinct_fresh_agent_dir_and_root_chowns_to_worker(self):
        dirs_seen: list[str] = []
        chown_calls: list[tuple[str, int, int]] = []
        fchown_calls: list[tuple[int, int]] = []

        def fake_run(argv, cwd, timeout, **kwargs):
            env = kwargs.get("env") or {}
            agent_dir = Path(env["PI_CODING_AGENT_DIR"])
            self.assertTrue(agent_dir.is_dir())
            self.assertFalse((agent_dir / "marker.txt").exists(), "must start fresh every iteration")
            (agent_dir / "marker.txt").write_text("tainted", encoding="utf-8")
            dirs_seen.append(str(agent_dir))
            return checks.ProcResult(0, False, "ok", "")

        workdir = self.cfg.workdir("demo")
        workdir.mkdir(parents=True, exist_ok=True)
        production = {k: v for k, v in os.environ.items() if k != "AGENTIC_WORKER_CMD"}
        cfg = config.load({**production, "AGENTIC_WORKER_HOME": str(self.worker_home)})
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=4242, pw_gid=4243)), \
                mock.patch("os.chown", side_effect=lambda p, u, g: chown_calls.append((str(p), u, g))), \
                mock.patch("os.fchown", side_effect=lambda fd, u, g: fchown_calls.append((u, g))), \
                mock.patch("agentic.worker.run_process", side_effect=fake_run):
            worker.run_worker(cfg, "m", "high", 5, "P1", workdir)
            worker.run_worker(cfg, "m", "high", 5, "P2", workdir)

        self.assertEqual(len(dirs_seen), 2)
        self.assertNotEqual(dirs_seen[0], dirs_seen[1])
        self.assertFalse(Path(dirs_seen[0]).exists())
        self.assertFalse(Path(dirs_seen[1]).exists())
        self.assertEqual(chown_calls, [(dirs_seen[0], 4242, 4243), (dirs_seen[1], 4242, 4243)])
        self.assertEqual(fchown_calls, [(4242, 4243)] * 4)

    def test_production_command_and_sandbox_bind_the_temp_agent_dir_writable(self):
        cfg = config.load({"AGENTIC_WORKER_HOME": str(self.worker_home)})
        self.assertIsNone(cfg.worker_cmd)
        agent_dir = "/var/lib/agwork/.omp-agent-abc123"
        with mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=4242)):
            cmd = worker.build_command(cfg, "m-x", "high", 45, "PROMPT", "/w/work", agent_dir=agent_dir)
        props = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
        self.assertIn(f"BindPaths={agent_dir}", props)
        # BindPaths for the throw-away agent dir comes after BindReadOnlyPaths on .omp so it stays writable.
        ro_idx = props.index(f"BindReadOnlyPaths={config.WORKER_OMP_DIR}")
        rw_idx = props.index(f"BindPaths={agent_dir}")
        self.assertGreater(rw_idx, ro_idx)
        # Inside the unit argv, env sets PI_CODING_AGENT_DIR to the temp folder.
        after_sep = cmd[cmd.index("--") + 1:]
        self.assertEqual(after_sep[:4], [
            "env", f"HOME={config.WORKER_HOME}", f"PI_CODING_AGENT_DIR={agent_dir}", config.OMP_BINARY,
        ])


class LeftoverClassificationTests(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.worker_home = self.stub_dir / "agwork-home"
        self.agent_src = self.worker_home / ".omp" / "agent"
        self.agent_src.mkdir(parents=True, mode=0o700)
        (self.agent_src / "models.yml").write_text("models: []\n", encoding="utf-8")
        (self.agent_src / "config.yml").write_text("config: {}\n", encoding="utf-8")
        os.environ["AGENTIC_WORKER_HOME"] = str(self.worker_home)
        self.cfg = config.load()

    def test_writes_to_workdir_temp_agent_dir_and_known_omp_state_are_not_leftovers(self):
        script = (
            "import os, pathlib\n"
            "pathlib.Path('c1.txt').write_text('ok', encoding='utf-8')\n"
            "pathlib.Path('c2.txt').write_text('ok', encoding='utf-8')\n"
            "agent_dir = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])\n"
            "(agent_dir / 'runtime.json').write_text('{}', encoding='utf-8')\n"
            "home = pathlib.Path(os.environ['HOME'])\n"
            "logs = home / '.omp' / 'logs'\n"
            "logs.mkdir(parents=True, exist_ok=True)\n"
            "(logs / 'omp.log').write_text('allowed log', encoding='utf-8')\n"
            "(home / '.omp' / 'stats.db').write_text('allowed db', encoding='utf-8')\n"
        )
        os.environ["AGENTIC_WORKER_CMD"] = json.dumps([sys.executable, "-c", script])
        self.cfg = config.load()
        self.assertEqual(self.run_mission(make_mission()), "complete")
        notices = [r for r in self.records() if r["type"] == "notice" and r["data"].get("kind") == "worker_leftover"]
        self.assertEqual(notices, [])

    def test_unknown_leftover_outside_allowed_paths_emits_notice_and_health_card_line(self):
        script = (
            "import os, pathlib\n"
            "pathlib.Path('c1.txt').write_text('ok', encoding='utf-8')\n"
            "pathlib.Path('c2.txt').write_text('ok', encoding='utf-8')\n"
            "home = pathlib.Path(os.environ['HOME'])\n"
            "(home / 'stray.txt').write_text('unexpected', encoding='utf-8')\n"
            "(home / '.omp' / 'agent' / 'rogue.json').write_text('unexpected', encoding='utf-8')\n"
        )
        os.environ["AGENTIC_WORKER_CMD"] = json.dumps([sys.executable, "-c", script])
        self.cfg = config.load()
        self.assertEqual(self.run_mission(make_mission()), "complete")

        recs = self.records()
        leftover_notices = [r for r in recs if r["type"] == "notice" and r["data"].get("kind") == "worker_leftover"]
        self.assertEqual(len(leftover_notices), 1)
        notice = leftover_notices[0]
        self.assertEqual(notice["data"]["files"], [".omp/agent/rogue.json", "stray.txt"])
        self.assertEqual(notice["data"]["iteration"], 1)
        # Only relative paths are recorded, never absolute paths.
        self.assertNotIn(str(self.worker_home), json.dumps(notice["data"]))
        # Notice is emitted in iterate() right before iteration_worker.
        worker_rec = next(r for r in recs if r["type"] == "iteration_worker")
        self.assertLess(notice["seq"], worker_rec["seq"])

        # Resuming the finished mission (or re-running after a crash after iteration_worker) does not duplicate it.
        self.assertEqual(loop.run("demo", self.cfg), "complete")
        leftover_notices_after = [
            r for r in self.records() if r["type"] == "notice" and r["data"].get("kind") == "worker_leftover"
        ]
        self.assertEqual(len(leftover_notices_after), 1)

        # Daily health card shows a line for the mission changed in the last 24 h.
        meminfo = self.stub_dir / "meminfo"
        meminfo.write_text("MemTotal: 16384000 kB\nMemAvailable: 12288000 kB\n", encoding="utf-8")
        aide_log = self.stub_dir / "aide.log"
        aide_log.write_text("Added entries: 0\nRemoved entries: 0\nChanged entries: 0\n", encoding="utf-8")
        backup_dir = self.stub_dir / "backups"
        backup_dir.mkdir()
        last_ts = datetime.fromisoformat(self.records()[-1]["ts"].replace("Z", "+00:00"))
        with mock.patch("agentic.commands.run_command", FakeCommands(_fake_health_respond)):
            h = health.run(
                self.cfg,
                now=last_ts + timedelta(hours=1),
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=meminfo,
                aide_log=aide_log,
                backup_dir=backup_dir,
                latest_release=lambda: "v1.2.3",
            )
        self.assertEqual(h.worker_leftovers, [("demo", [".omp/agent/rogue.json", "stray.txt"])])
        card_text = "\n".join(cards.plain_text(b) for b in health.card(h))
        self.assertIn("작업 폴더 밖에 남은 파일", card_text)
        self.assertIn("demo (.omp/agent/rogue.json, stray.txt)", card_text)

        # Missions older than 24 h are not included on the daily health card.
        with mock.patch("agentic.commands.run_command", FakeCommands(_fake_health_respond)):
            h_old = health.run(
                self.cfg,
                now=last_ts + timedelta(hours=25),
                disk_usage=lambda _: DiskUsage(100, 40, 60),
                meminfo_path=meminfo,
                aide_log=aide_log,
                backup_dir=backup_dir,
                latest_release=lambda: "v1.2.3",
            )
        self.assertEqual(h_old.worker_leftovers, [])


class NoResumeOrSessionEnvInheritanceTests(TimedTestCase):
    def test_worker_command_never_contains_resume_or_continue_flags(self):
        self.assertIn("--no-session", config.OMP_ARGV)
        self.assertTrue(RESUME_FLAGS.isdisjoint(config.OMP_ARGV))
        cfg = config.load({})
        with mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=4242)):
            cmd = worker.build_command(
                cfg, "anthropic/claude-sonnet-5-5", "high", 45, "Build the tool", "/w/work",
                agent_dir="/var/lib/agwork/.omp-agent-123",
            )
        after_sep = cmd[cmd.index("--") + 1:-1]  # exclude the prompt positional argument itself
        for token in after_sep:
            self.assertNotIn(token, RESUME_FLAGS)
            for flag in ("--resume=", "--continue=", "--session=", "--session-id="):
                self.assertFalse(token.startswith(flag), token)

    def test_worker_never_inherits_session_environment_variables_from_supervisor(self):
        tmp = Path(__import__("tempfile").mkdtemp(prefix="agentic-fresh-env-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        worker_home = tmp / "home"
        (worker_home / ".omp" / "agent").mkdir(parents=True, mode=0o700)
        workdir = tmp / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        env_dump = tmp / "worker_env.json"
        script = (
            "import json, os, pathlib\n"
            f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
        )
        poison = {
            "PI_CODING_AGENT_DIR": "/poisoned/parent/agent-dir",
            "PI_SESSION_ID": "poison-session-1",
            "PI_RESUME": "1",
            "OMP_SESSION": "poison-omp-session",
            "OMP_SESSION_ID": "poison-omp-session-id",
            "OMP_SESSION_DIR": "/poisoned/omp/sessions",
            "OMP_RESUME": "true",
            "SECRET_SUPERVISOR_VAR": "should-not-leak",
            "AGENTIC_WORKER_HOME": str(worker_home),
            "AGENTIC_WORKER_CMD": json.dumps([sys.executable, "-c", script]),
        }
        with mock.patch.dict(os.environ, poison, clear=False):
            cfg = config.load()
            with mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=4242)):
                prod_cfg = config.load({"AGENTIC_WORKER_HOME": str(worker_home)})
                prod_cmd = worker.build_command(
                    prod_cfg, "m", "high", 10, "PROMPT", workdir, agent_dir="/var/lib/agwork/.omp-agent-fresh"
                )
            for token in prod_cmd:
                self.assertNotIn("poison", token)
            res = worker.run_worker(cfg, "m", "high", 5, "PROMPT", workdir)

        self.assertEqual(res.exit_code, 0, res.stderr)
        child_env = json.loads(env_dump.read_text(encoding="utf-8"))
        self.assertNotEqual(child_env["PI_CODING_AGENT_DIR"], "/poisoned/parent/agent-dir")
        self.assertTrue(child_env["PI_CODING_AGENT_DIR"].startswith(str(worker_home / ".omp-agent-")))
        self.assertEqual(child_env["HOME"], str(worker_home))
        for key in ("PI_SESSION_ID", "PI_RESUME", "OMP_SESSION", "OMP_SESSION_ID",
                    "OMP_SESSION_DIR", "OMP_RESUME", "SECRET_SUPERVISOR_VAR"):
            self.assertNotIn(key, child_env)
        for key in child_env:
            if key != "PI_CODING_AGENT_DIR":
                self.assertFalse(key.startswith("PI_"), key)
                self.assertFalse(key.startswith("OMP_"), key)


if __name__ == "__main__":
    unittest.main()
