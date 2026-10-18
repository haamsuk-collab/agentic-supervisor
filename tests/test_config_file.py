"""SPEC-G section 1: optional TOML configuration file, apply_settings, and `agentic config check`."""

import contextlib
import io
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentic import admin, cli, commands, config, daily, health, llm, mission, mutation, outage, telegram, worker
from helpers import FakeCommands, SupervisorTestCase, TimedTestCase


class ConfigFileTestCase(TimedTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp(prefix="agentic-cfg-test-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        self.tmp = Path(tmp)
        self._saved_settings = config.SETTINGS
        self._saved_error = config.SETTINGS_ERROR
        self._saved_path = config.SETTINGS_PATH
        self._saved_lang = config.LANGUAGE
        self._saved_tz = config.DISPLAY_TIMEZONE
        self.addCleanup(self._restore_config)

    def _restore_config(self) -> None:
        config.apply_settings(self._saved_settings)
        config.SETTINGS_ERROR = self._saved_error
        config.SETTINGS_PATH = self._saved_path
        config.LANGUAGE = self._saved_lang
        config.DISPLAY_TIMEZONE = self._saved_tz

    def write_toml(self, content: str, name: str = "agentic.toml") -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path


class DefaultsAndOverridesTests(ConfigFileTestCase):
    def test_no_file_matches_todays_defaults_and_utc_en(self):
        s = config.load_settings(None)
        config.apply_settings(s)

        self.assertEqual(config.INTERACTIVE_USER, "agent")
        self.assertEqual(config.WORKER_USER, "agwork")
        self.assertEqual(config.WORKER_HOME, "/var/lib/agwork")
        self.assertEqual(config.PLANNER_USER, "agplan")
        self.assertEqual(config.PLANNER_HOME, "/var/lib/agplan")
        self.assertEqual(config.BOT_USER, "agbot")

        self.assertEqual(config.DEFAULT_STATE, "/var/lib/agentic")
        self.assertEqual(config.DEFAULT_WORKSPACES, "/var/lib/agwork/missions")
        self.assertEqual(config.DEFAULT_SPOOL, "/var/spool/agentic")
        self.assertEqual(config.DEFAULT_INBOX, "/var/spool/agentic/inbox")
        self.assertEqual(config.DEFAULT_OUTBOX, "/var/spool/agentic/outbox")
        self.assertEqual(config.OMP_BINARY, "/opt/omp/omp")
        self.assertEqual(config.PLANNER_OMP, "/opt/omp/omp")
        self.assertEqual(config.OMP_OVERLAY, "/etc/agentic/omp-overlay.yml")
        self.assertEqual(config.GUIDANCE_SYNC_CMD, "/usr/local/sbin/agentic-guidance-sync")
        self.assertEqual(config.GUIDANCE_CHECK_CMD, "/usr/local/sbin/agentic-guidance-check")
        self.assertEqual(config.OMP_ARGV, [
            "env", "HOME=/var/lib/agwork", "/opt/omp/omp",
            "-p", "--no-session", "--no-rules", "--no-skills", "--no-extensions", "--no-title",
            "--config", "/etc/agentic/omp-overlay.yml",
        ])
        self.assertEqual(config.PLANNER_OMP_ARGV, [
            "env", "HOME=/var/lib/agplan", "/opt/omp/omp",
            "-p", "--no-session", "--no-tools", "--no-rules", "--no-skills", "--no-extensions", "--no-title",
        ])
        self.assertEqual(config.WORKER_OMP_DIR, "/var/lib/agwork/.omp")
        self.assertTrue(all(p.startswith("/var/lib/agwork/.omp/") for p in config.WORKER_OMP_STATE))
        self.assertEqual(
            config.WORKER_INACCESSIBLE,
            ("/var/lib/agentic", "/var/lib/agplan", "/var/spool/agentic", "/etc/agentic/telegram.json"),
        )

        self.assertEqual(config.PLANNER_MODELS, {
            "brief": ["google-antigravity/gemini-4-argon:high", "anthropic/claude-opus-5-5:high"],
            "design": ["anthropic/claude-fable-5-1:high", "anthropic/claude-opus-5-5:high"],
            "checks": ["anthropic/claude-opus-5-5:high", "anthropic/claude-fable-5-1:high"],
            "critique": ["google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"],
        })
        self.assertEqual(
            config.REVIEW_MODELS,
            ["google-antigravity/gemini-4-argon:high", "anthropic/claude-sonnet-5-5:high"],
        )
        self.assertEqual(config.WORKER_LADDER, [
            {"model": "anthropic/claude-sonnet-5-5", "thinking": "high", "iteration_minutes": 45},
            {"model": "anthropic/claude-opus-5-5", "thinking": "high", "iteration_minutes": 45},
            {"model": "anthropic/claude-fable-5-1", "thinking": "high", "iteration_minutes": 90},
        ])
        self.assertEqual(config.ALTERNATE_PROVIDER, {
            "anthropic/claude-sonnet-5-5": "google-antigravity/claude-sonnet-5-5",
            "anthropic/claude-opus-5-5": "google-antigravity/claude-opus-5-5",
        })

        self.assertEqual(config.MAX_ACTIVE_MISSIONS, 3)
        self.assertEqual(config.OUTAGE_WAITS_MINUTES, (5, 10, 20, 40, 60))
        self.assertEqual(config.CANARY_MAX_AGE_DAYS, 7)
        self.assertEqual(config.MUTATION_MAX_MUTANTS, 6)
        self.assertEqual(config.MUTATION_BUDGET_SECONDS, 600)

        self.assertEqual(config.DISPLAY_TIMEZONE, "UTC")
        self.assertEqual(config.LANGUAGE, "en")
        self.assertTrue(config.TELEGRAM_ENABLED)

    def test_each_section_override_updates_constants_and_worker_planner_argv(self):
        path = self.write_toml("""
[accounts]
interactive_user = "devuser"
worker_user = "customwork"
planner_user = "customplan"
bot_user = "custombot"

[paths]
state = "/srv/agentic/state"
workspaces = "/srv/agentic/workspaces"
spool = "/srv/agentic/spool"
worker_omp = "/opt/custom/worker-omp"
planner_omp = "/opt/custom/planner-omp"
worker_overlay = "/etc/custom/overlay.yml"
guidance_sync = "/usr/local/bin/custom-sync"
guidance_check = "/usr/local/bin/custom-check"

[models]
planner_steps = { brief = ["provider/b:low"], design = ["provider/d:medium"], checks = ["provider/c:high"], critique = ["provider/q:high"] }
review = ["provider/rev:high"]
worker_ladder = [{ model = "provider/w1", thinking = "medium", iteration_minutes = 30 }]
alternate_provider = { "provider/w1" = "backup/w1" }

[limits]
max_active_missions = 5
outage_waits_minutes = [2, 4, 8]
canary_max_age_days = 14
mutation_max_mutants = 4
mutation_budget_seconds = 180

[display]
timezone = "Asia/Seoul"
language = "ko"

[telegram]
enabled = false
""")
        s = config.load_settings(path)
        config.apply_settings(s)

        self.assertEqual(config.INTERACTIVE_USER, "devuser")
        self.assertEqual(config.WORKER_USER, "customwork")
        self.assertEqual(config.WORKER_HOME, "/var/lib/customwork")
        self.assertEqual(config.PLANNER_USER, "customplan")
        self.assertEqual(config.PLANNER_HOME, "/var/lib/customplan")
        self.assertEqual(config.BOT_USER, "custombot")

        self.assertEqual(config.DEFAULT_STATE, "/srv/agentic/state")
        self.assertEqual(config.DEFAULT_WORKSPACES, "/srv/agentic/workspaces")
        self.assertEqual(config.DEFAULT_INBOX, "/srv/agentic/spool/inbox")
        self.assertEqual(config.DEFAULT_OUTBOX, "/srv/agentic/spool/outbox")
        self.assertEqual(config.OMP_BINARY, "/opt/custom/worker-omp")
        self.assertEqual(config.PLANNER_OMP, "/opt/custom/planner-omp")
        self.assertEqual(config.OMP_OVERLAY, "/etc/custom/overlay.yml")
        self.assertEqual(config.GUIDANCE_SYNC_CMD, "/usr/local/bin/custom-sync")
        self.assertEqual(config.GUIDANCE_CHECK_CMD, "/usr/local/bin/custom-check")
        self.assertEqual(config.WORKER_OMP_DIR, "/var/lib/customwork/.omp")
        self.assertTrue(all(p.startswith("/var/lib/customwork/.omp/") for p in config.WORKER_OMP_STATE))
        self.assertEqual(
            config.WORKER_INACCESSIBLE,
            ("/srv/agentic/state", "/var/lib/customplan", "/srv/agentic/spool", "/etc/agentic/telegram.json"),
        )

        self.assertEqual(config.PLANNER_MODELS["brief"], ["provider/b:low"])
        self.assertEqual(config.REVIEW_MODELS, ["provider/rev:high"])
        self.assertEqual(config.WORKER_LADDER, [
            {"model": "provider/w1", "thinking": "medium", "iteration_minutes": 30},
        ])
        self.assertEqual(config.ALTERNATE_PROVIDER, {"provider/w1": "backup/w1"})

        self.assertEqual(config.MAX_ACTIVE_MISSIONS, 5)
        self.assertEqual(config.OUTAGE_WAITS_MINUTES, (2, 4, 8))
        self.assertEqual(outage.wait_seconds(0), 120)
        self.assertEqual(outage.wait_seconds(99), 480)
        self.assertEqual(config.CANARY_MAX_AGE_DAYS, 14)
        self.assertEqual(config.MUTATION_MAX_MUTANTS, 4)
        self.assertEqual(config.MUTATION_BUDGET_SECONDS, 180)
        self.assertEqual(config.DISPLAY_TIMEZONE, "Asia/Seoul")
        self.assertEqual(config.LANGUAGE, "ko")
        self.assertFalse(config.TELEGRAM_ENABLED)

        # Verify generated worker and planner commands reflect the applied settings.
        cfg = config.load({})
        self.assertEqual(cfg.state, Path("/srv/agentic/state"))
        self.assertEqual(cfg.workspaces, Path("/srv/agentic/workspaces"))
        self.assertEqual(cfg.inbox, Path("/srv/agentic/spool/inbox"))
        self.assertEqual(cfg.outbox, Path("/srv/agentic/spool/outbox"))
        self.assertEqual(cfg.bot_user, "custombot")

        with mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=5001)) as getpwnam:
            w_cmd = worker.build_command(cfg, "provider/w1", "medium", 30, "PROMPT", "/srv/agentic/workspaces/m1/work")
        getpwnam.assert_called_once_with("customwork")
        self.assertIn("--uid=customwork", w_cmd)
        self.assertIn("Slice=user-5001.slice", w_cmd)
        self.assertIn("BindReadOnlyPaths=/var/lib/customwork/.omp", w_cmd)
        self.assertIn(
            "InaccessiblePaths=/srv/agentic/state /var/lib/customplan /srv/agentic/spool /etc/agentic/telegram.json",
            w_cmd,
        )
        self.assertIn("HOME=/var/lib/customwork", w_cmd)
        self.assertIn("/opt/custom/worker-omp", w_cmd)
        self.assertIn("/etc/custom/overlay.yml", w_cmd)

        with mock.patch("agentic.worker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=5002)) as getpwnam:
            p_cmd = llm.build_command(cfg, "provider/b", "low")
        getpwnam.assert_called_once_with("customplan")
        self.assertIn("--uid=customplan", p_cmd)
        self.assertIn("WorkingDirectory=/var/lib/customplan", p_cmd)
        self.assertIn("Slice=user-5002.slice", p_cmd)
        self.assertIn("HOME=/var/lib/customplan", p_cmd)
        self.assertIn("/opt/custom/planner-omp", p_cmd)

        # Environment variables still win over the TOML settings.
        env_cfg = config.load({
            "AGENTIC_STATE": "/tmp/env-state",
            "AGENTIC_WORKSPACES": "/tmp/env-ws",
            "AGENTIC_INBOX": "/tmp/env-inbox",
            "AGENTIC_OUTBOX": "/tmp/env-outbox",
            "AGENTIC_BOT_USER": "envbot",
        })
        self.assertEqual(env_cfg.state, Path("/tmp/env-state"))
        self.assertEqual(env_cfg.workspaces, Path("/tmp/env-ws"))
        self.assertEqual(env_cfg.inbox, Path("/tmp/env-inbox"))
        self.assertEqual(env_cfg.outbox, Path("/tmp/env-outbox"))
        self.assertEqual(env_cfg.bot_user, "envbot")


class ValidationErrorTests(ConfigFileTestCase):
    def assert_bad_toml(self, toml_text: str, expected_key: str) -> None:
        path = self.write_toml(toml_text)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_settings(path)
        self.assertIn(expected_key, str(ctx.exception))

    def test_unknown_section_and_unknown_key_are_rejected(self):
        self.assert_bad_toml('[unknown_section]\nfoo = "bar"\n', "unknown_section")
        self.assert_bad_toml('[accounts]\nunknown_key = "agent"\n', "accounts.unknown_key")
        self.assert_bad_toml('[paths]\nextra = "/opt/x"\n', "paths.extra")
        self.assert_bad_toml('[models]\nplanner_steps = { unknown = ["m:high"] }\n', "models.planner_steps.unknown")

    def test_wrong_types_empty_strings_and_invalid_values_name_the_key(self):
        cases = [
            ('[accounts]\nworker_user = 123\n', "accounts.worker_user"),
            ('[accounts]\nworker_user = ""\n', "accounts.worker_user"),
            ('[accounts]\nworker_user = "bad user!"\n', "accounts.worker_user"),
            ('[accounts]\ninteractive_user = "-bad"\n', "accounts.interactive_user"),
            ('[paths]\nstate = "relative/path"\n', "paths.state"),
            ('[paths]\nworkspaces = ""\n', "paths.workspaces"),
            ('[paths]\nspool = "/var/spool/../etc"\n', "paths.spool"),
            ('[paths]\nworker_omp = 42\n', "paths.worker_omp"),
            ('[models]\nreview = "not-a-list"\n', "models.review"),
            ('[models]\nreview = []\n', "models.review"),
            ('[models]\nreview = ["missing-thinking-colon"]\n', "models.review"),
            ('[models]\nplanner_steps = { brief = ["no-colon"] }\n', "models.planner_steps.brief"),
            ('[models]\nworker_ladder = []\n', "models.worker_ladder"),
            ('[models]\nworker_ladder = [{ thinking = "high" }]\n', "models.worker_ladder"),
            ('[models]\nworker_ladder = [{ model = "m", iteration_minutes = 0 }]\n', "models.worker_ladder"),
            ('[models]\nworker_ladder = [{ model = "m", extra = 1 }]\n', "models.worker_ladder"),
            ('[models]\nalternate_provider = { "m" = "" }\n', "models.alternate_provider"),
            ('[limits]\nmax_active_missions = 0\n', "limits.max_active_missions"),
            ('[limits]\nmax_active_missions = true\n', "limits.max_active_missions"),
            ('[limits]\nmax_active_missions = "three"\n', "limits.max_active_missions"),
            ('[limits]\noutage_waits_minutes = []\n', "limits.outage_waits_minutes"),
            ('[limits]\noutage_waits_minutes = [5, -1]\n', "limits.outage_waits_minutes"),
            ('[limits]\ncanary_max_age_days = 0\n', "limits.canary_max_age_days"),
            ('[limits]\nmutation_max_mutants = -2\n', "limits.mutation_max_mutants"),
            ('[limits]\nmutation_budget_seconds = false\n', "limits.mutation_budget_seconds"),
            ('[display]\ntimezone = "Not/A_Real_Timezone"\n', "display.timezone"),
            ('[display]\ntimezone = "/etc/localtime"\n', "display.timezone"),
            ('[display]\ntimezone = 123\n', "display.timezone"),
            ('[display]\nlanguage = "fr"\n', "display.language"),
            ('[display]\nlanguage = 1\n', "display.language"),
            ('[telegram]\nenabled = "yes"\n', "telegram.enabled"),
        ]
        for toml_text, expected_key in cases:
            with self.subTest(expected_key=expected_key, toml=toml_text):
                self.assert_bad_toml(toml_text, expected_key)


class CliConfigCheckAndGuardTests(ConfigFileTestCase):
    def run_cli_with_config(self, config_path: Path | str, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ("AGENTIC_STATE", "AGENTIC_WORKSPACES", "AGENTIC_INBOX", "AGENTIC_OUTBOX", "AGENTIC_BOT_USER")
        }
        clean_env["AGENTIC_CONFIG"] = str(config_path)
        with mock.patch.dict(os.environ, clean_env, clear=True):
            config.reload_settings()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_config_check_exits_0_and_prints_effective_toml(self):
        path = self.write_toml("""
[accounts]
worker_user = "wuser"

[limits]
max_active_missions = 7

[display]
timezone = "Asia/Seoul"
language = "ko"

[telegram]
enabled = false
""")
        code, out, err = self.run_cli_with_config(path, "config", "check")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        parsed = tomllib.loads(out)
        self.assertEqual(parsed["accounts"]["worker_user"], "wuser")
        self.assertEqual(parsed["accounts"]["interactive_user"], "agent")
        self.assertEqual(parsed["limits"]["max_active_missions"], 7)
        self.assertEqual(parsed["display"]["timezone"], "Asia/Seoul")
        self.assertEqual(parsed["display"]["language"], "ko")
        self.assertIs(parsed["telegram"]["enabled"], False)

    def test_config_check_and_other_commands_exit_1_on_invalid_or_missing_file(self):
        bad_path = self.write_toml('[display]\nlanguage = "de"\n', "bad.toml")
        code, out, err = self.run_cli_with_config(bad_path, "config", "check")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(str(bad_path), err)
        self.assertIn("display.language", err)

        # Other CLI commands also refuse to run with exit 1 when the config file is invalid.
        for cmd in (["verify", "demo"], ["report", "demo"], ["health"], ["mission", "list"]):
            with self.subTest(cmd=cmd):
                code2, out2, err2 = self.run_cli_with_config(bad_path, *cmd)
                self.assertEqual(code2, 1)
                self.assertEqual(out2, "")
                self.assertIn("display.language", err2)

        # telegram.main also exits 1 on an invalid file.
        with mock.patch.dict(os.environ, {"AGENTIC_CONFIG": str(bad_path)}):
            config.reload_settings()
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                self.assertEqual(telegram.main(), 1)
            self.assertIn("display.language", err_buf.getvalue())

        # A missing AGENTIC_CONFIG file is also an error (exit 1).
        missing_path = self.tmp / "does-not-exist.toml"
        code3, out3, err3 = self.run_cli_with_config(missing_path, "config", "check")
        self.assertEqual(code3, 1)
        self.assertEqual(out3, "")
        self.assertIn(str(missing_path), err3)


class TelegramDisabledHealthTests(SupervisorTestCase):
    def test_health_skips_agentic_telegram_when_disabled_in_settings(self):
        seen_units: list[str] = []

        def respond(argv: list[str]) -> commands.CommandResult:
            if argv[:2] == ["systemctl", "is-active"]:
                seen_units.append(argv[2])
                return commands.CommandResult(0, "active\n", "")
            return commands.CommandResult(0, "", "")

        meminfo = self.stub_dir / "meminfo"
        meminfo.write_text("MemTotal: 8000000 kB\nMemAvailable: 6000000 kB\n", encoding="utf-8")
        aide_log = self.stub_dir / "aide.log"
        aide_log.write_text("Added entries: 0\nRemoved entries: 0\nChanged entries: 0\n", encoding="utf-8")
        DiskUsage = __import__("collections").namedtuple("DiskUsage", ["total", "used", "free"])

        fake = FakeCommands(respond)
        with mock.patch.object(config, "TELEGRAM_ENABLED", False), \
                mock.patch("agentic.commands.run_command", fake):
            h = health.run(
                self.cfg,
                disk_usage=lambda _: DiskUsage(100, 30, 70),
                meminfo_path=meminfo,
                aide_log=aide_log,
                backup_dir=self.stub_dir,
                latest_release=lambda: "v1.0.0",
            )
        self.assertNotIn("agentic-telegram.service", seen_units)
        self.assertNotIn("agentic-telegram.service", [u for u, _, _ in h.services])


if __name__ == "__main__":
    unittest.main()
