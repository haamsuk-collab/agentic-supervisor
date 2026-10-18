"""Tests for install.sh, uninstall.sh, and guidance sync/check scripts (SPEC-G section 4)."""

import os
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from helpers import ROOT, TimedTestCase

INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"
GUIDANCE_SYNC = ROOT / "deploy" / "bin" / "agentic-guidance-sync"
GUIDANCE_CHECK = ROOT / "deploy" / "bin" / "agentic-guidance-check"

MUTATING_COMMANDS = (
    "systemctl",
    "useradd",
    "groupadd",
    "chattr",
    "install",
    "chmod",
    "chown",
    "mount",
    "visudo",
)


class InstallScriptTests(TimedTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.prefix = self.tmp / "prefix"
        self.prefix.mkdir()
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.called_log = self.tmp / "called.log"
        self.fake_omp = self.tmp / "fake-omp"
        self.fake_omp.write_text("#!/bin/sh\necho 'omp 1.0.0'\n", encoding="utf-8")
        self.fake_omp.chmod(0o755)
        self._install_fake_commands()

    def tearDown(self):
        self._tmp.cleanup()
        super().tearDown()

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _install_fake_commands(self) -> None:
        for cmd in MUTATING_COMMANDS:
            self._write_executable(
                self.bin_dir / cmd,
                f"#!/usr/bin/env bash\n"
                f"echo '{cmd} '\"$*\" >> {str(self.called_log)!r}\n"
                f"echo 'FAIL: mutating command {cmd} was executed during dry-run' >&2\n"
                f"exit 99\n",
            )
        # Read-only probe: getent
        self._write_executable(
            self.bin_dir / "getent",
            "#!/usr/bin/env bash\n"
            "db=\"${1:-}\"\n"
            "key=\"${2:-}\"\n"
            "existing=\":${FAKE_EXISTING-agent}:\"\n"
            "if [[ \"$db\" == \"passwd\" && \"$existing\" == *\":${key}:\"* ]]; then\n"
            "    if [[ \"$key\" == \"agent\" ]]; then\n"
            "        echo 'agent:x:1001:1001:Interactive Agent:/home/agent:/bin/bash'\n"
            "    else\n"
            "        echo \"${key}:x:980:980:system:/var/lib/${key}:/usr/sbin/nologin\"\n"
            "    fi\n"
            "    exit 0\n"
            "fi\n"
            "if [[ \"$db\" == \"group\" && \"$existing\" == *\":${key}:\"* ]]; then\n"
            "    echo \"${key}:x:980:\"\n"
            "    exit 0\n"
            "fi\n"
            "exit 2\n",
        )
        # Read-only probe: id
        self._write_executable(
            self.bin_dir / "id",
            "#!/usr/bin/env bash\n"
            "if [[ \"${1:-}\" == \"-u\" ]]; then\n"
            "    echo \"${FAKE_UID:-1001}\"\n"
            "    exit 0\n"
            "fi\n"
            "echo 'uid=1001(agent) gid=1001(agent)'\n",
        )

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["AGENTIC_ROOT_PREFIX"] = str(self.prefix)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
        if extra:
            env.update(extra)
        return env

    def _run_install(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(INSTALL_SH), *args],
            env=self._env(extra_env),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _run_uninstall(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(UNINSTALL_SH), *args],
            env=self._env(extra_env),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_dry_run_default_flags_prints_steps_and_actions_without_touching_disk(self):
        res = self._run_install("--dry-run", "--omp", str(self.fake_omp))
        self.assertEqual(res.returncode, 0, f"stderr: {res.stderr}\nstdout: {res.stdout}")
        self.assertFalse(self.called_log.exists(), "dry-run must not invoke any mutating command")
        self.assertEqual(list(self.prefix.iterdir()), [], "dry-run must not create anything under AGENTIC_ROOT_PREFIX")

        out = res.stdout
        steps = [line for line in out.splitlines() if line.startswith("==> ")]
        self.assertEqual(
            steps,
            [
                "==> prerequisites",
                "==> accounts",
                "==> folders",
                "==> package",
                "==> launchers",
                "==> units/timers",
                "==> config",
                "==> hidepid",
                "==> agent sudo",
                "==> guidance",
                "==> next steps",
            ],
        )

        # Accounts with homes and 0700 modes
        for acct in ("agwork", "agplan", "agbot"):
            home = f"{self.prefix}/var/lib/{acct}"
            self.assertIn(
                f"+ useradd --system --create-home --home-dir {home} --shell /usr/sbin/nologin {acct}",
                out,
            )
            self.assertIn(f"+ chmod 0700 {home}", out)

        # Folders and modes
        self.assertIn(f"+ install -d -m 0700 -o root -g root {self.prefix}/var/lib/agentic", out)
        self.assertIn(f"+ install -d -m 0700 -o agwork -g agwork {self.prefix}/var/lib/agwork/missions", out)
        self.assertIn(f"+ install -d -m 0755 -o root -g root {self.prefix}/var/spool/agentic", out)
        self.assertIn(f"+ install -d -m 0700 -o agbot -g agbot {self.prefix}/var/spool/agentic/inbox", out)
        self.assertIn(f"+ install -d -m 0700 -o agbot -g agbot {self.prefix}/var/spool/agentic/outbox", out)
        self.assertIn(f"+ install -d -m 0700 -o root -g root {self.prefix}/var/backups/agentic", out)

        # Package and pinned omp
        self.assertIn(f"{self.prefix}/opt/agentic", out)
        self.assertIn(f"{self.prefix}/opt/agentic/REVISION", out)
        self.assertIn(f"+ install -m 0755 -o root -g root {self.fake_omp} {self.prefix}/opt/omp/omp", out)

        # Launchers
        for launcher in ("agentic", "mission", "agentic-guidance-sync", "agentic-guidance-check"):
            self.assertIn(f"{self.prefix}/usr/local/sbin/{launcher}", out)

        # Units and timers (telegram disabled by default)
        for unit in (
            "agentic@.service",
            "agentic-failed@.service",
            "agentic-inbox.path",
            "agentic-inbox.service",
            "agentic-inbox.timer",
            "agentic-daily.service",
            "agentic-daily.timer",
            "agentic-health.service",
            "agentic-health.timer",
        ):
            self.assertIn(f"{self.prefix}/etc/systemd/system/{unit}", out)
        self.assertNotIn("agentic-telegram.service", out)
        self.assertIn("+ systemctl daemon-reload", out)
        self.assertIn(
            "+ systemctl enable --now agentic-inbox.path agentic-inbox.timer agentic-daily.timer agentic-health.timer",
            out,
        )

        # Config (default UTC, en, telegram = false) and worker overlay
        self.assertIn(f"{self.prefix}/etc/agentic/agentic.toml", out)
        self.assertIn('timezone = "UTC"', out)
        self.assertIn('language = "en"', out)
        self.assertIn("enabled = false", out)
        self.assertIn(f"{self.prefix}/etc/agentic/omp-overlay.yml", out)
        self.assertIn("modelFallback: false", out)

        # Hidepid on by default
        self.assertIn("+ groupadd --system procview", out)
        self.assertIn("proc /proc proc defaults,hidepid=2,gid=procview 0 0", out)
        self.assertIn("+ mount -o remount /proc", out)

        # Agent sudo off by default
        self.assertNotIn("sudoers", out)
        self.assertNotIn("visudo", out)

        # Guidance sync and chattr +i
        self.assertIn(f"+ {self.prefix}/usr/local/sbin/agentic-guidance-sync", out)
        self.assertIn(f"+ chattr +i {self.prefix}/home/agent/.omp/agent/RULES.md", out)

        # Final message
        self.assertIn("omp-auth-gateway", out)
        self.assertIn("docs/OPERATIONS.md", out)

    def test_dry_run_custom_flags_telegram_no_hidepid_agent_sudo_ko_kst(self):
        res = self._run_install(
            "--dry-run",
            "--omp",
            str(self.fake_omp),
            "--timezone",
            "Asia/Seoul",
            "--language",
            "ko",
            "--telegram",
            "--no-hidepid",
            "--agent-sudo",
        )
        self.assertEqual(res.returncode, 0, f"stderr: {res.stderr}\nstdout: {res.stdout}")
        self.assertFalse(self.called_log.exists())
        self.assertEqual(list(self.prefix.iterdir()), [])

        out = res.stdout
        self.assertIn('timezone = "Asia/Seoul"', out)
        self.assertIn('language = "ko"', out)
        self.assertIn("enabled = true", out)
        self.assertIn(f"{self.prefix}/etc/systemd/system/agentic-telegram.service", out)
        self.assertIn("agentic-telegram.service", out.split("+ systemctl enable --now", 1)[1].splitlines()[0])

        # --no-hidepid omits groupadd procview, fstab line, and mount remount
        self.assertNotIn("procview", out)
        self.assertNotIn("hidepid=2", out)
        self.assertNotIn("mount -o remount /proc", out)

        # --agent-sudo runs visudo -cf and installs 0440 sudoers file
        self.assertIn("+ visudo -cf", out)
        self.assertIn(
            f"+ install -m 0440 -o root -g root {ROOT}/deploy/sudoers/agentic-agent.example "
            f"{self.prefix}/etc/sudoers.d/agentic-agent",
            out,
        )

        # Extract the printed TOML block and verify it parses cleanly with tomllib
        toml_lines: list[str] = []
        in_toml = False
        for line in out.splitlines():
            if line.endswith("/etc/agentic/agentic.toml"):
                in_toml = True
                continue
            if in_toml:
                if line.startswith("+ ") or line.startswith("==> "):
                    break
                toml_lines.append(line)
        parsed_toml = tomllib.loads("\n".join(toml_lines))
        self.assertEqual(parsed_toml["display"], {"timezone": "Asia/Seoul", "language": "ko"})
        self.assertEqual(parsed_toml["telegram"], {"enabled": True})
        self.assertEqual(parsed_toml["accounts"]["worker_user"], "agwork")

    def test_idempotency_skips_existing_accounts_config_and_fstab(self):
        etc_agentic = self.prefix / "etc" / "agentic"
        etc_agentic.mkdir(parents=True)
        (etc_agentic / "agentic.toml").write_text("[display]\nlanguage = 'ko'\n", encoding="utf-8")
        (etc_agentic / "omp-overlay.yml").write_text("retry:\n  modelFallback: false\n", encoding="utf-8")
        (self.prefix / "etc" / "fstab").write_text(
            "proc /proc proc defaults,hidepid=2,gid=procview 0 0\n",
            encoding="utf-8",
        )

        res = self._run_install(
            "--dry-run",
            "--omp",
            str(self.fake_omp),
            extra_env={"FAKE_EXISTING": "agent:agwork:agplan:agbot:procview"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertNotIn("useradd", out)
        self.assertNotIn("groupadd", out)
        self.assertNotIn("/dev/stdin", out)
        self.assertNotIn("append ", out)

    def test_missing_or_invalid_arguments_fail_with_message(self):
        missing_omp = self._run_install("--dry-run")
        self.assertNotEqual(missing_omp.returncode, 0)
        self.assertIn("--omp", missing_omp.stderr)

        bad_omp = self._run_install("--dry-run", "--omp", str(self.tmp / "does-not-exist"))
        self.assertNotEqual(bad_omp.returncode, 0)
        self.assertIn("--omp", bad_omp.stderr)

        bad_lang = self._run_install("--dry-run", "--omp", str(self.fake_omp), "--language", "fr")
        self.assertNotEqual(bad_lang.returncode, 0)
        self.assertIn("--language", bad_lang.stderr)

        no_agent = self._run_install(
            "--dry-run",
            "--omp",
            str(self.fake_omp),
            extra_env={"FAKE_EXISTING": ""},
        )
        self.assertNotEqual(no_agent.returncode, 0)
        self.assertIn("agent", no_agent.stderr)

    def test_uninstall_keeps_state_unless_purge(self):
        res_keep = self._run_uninstall("--dry-run")
        self.assertEqual(res_keep.returncode, 0, res_keep.stderr)
        self.assertFalse(self.called_log.exists())
        out_keep = res_keep.stdout
        self.assertIn("+ systemctl disable --now", out_keep)
        self.assertIn(f"{self.prefix}/etc/systemd/system/agentic@.service", out_keep)
        self.assertIn(f"{self.prefix}/usr/local/sbin/agentic", out_keep)
        self.assertIn(f"+ rm -rf {self.prefix}/opt/agentic", out_keep)
        self.assertNotIn(f"rm -rf {self.prefix}/var/lib/agentic", out_keep)
        self.assertNotIn("chattr -a", out_keep)
        self.assertIn("Keeping state", out_keep)
        self.assertIn("accounts", out_keep)

        res_purge = self._run_uninstall("--dry-run", "--purge")
        self.assertEqual(res_purge.returncode, 0, res_purge.stderr)
        self.assertFalse(self.called_log.exists())
        out_purge = res_purge.stdout
        self.assertIn(f"+ chattr -a {self.prefix}/var/lib/agentic/missions/*/ledger.jsonl", out_purge)
        self.assertIn(f"{self.prefix}/var/lib/agentic", out_purge)
        self.assertIn(f"{self.prefix}/etc/agentic", out_purge)
        self.assertIn(f"{self.prefix}/var/spool/agentic", out_purge)
        self.assertIn(f"{self.prefix}/var/backups/agentic", out_purge)
        self.assertIn("accounts", out_purge)


class GuidanceScriptsTests(TimedTestCase):
    def test_guidance_sync_and_check_detect_and_repair_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            dst = tmp / "agent-omp"
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            chattr_log = tmp / "chattr.log"
            chattr = bin_dir / "chattr"
            chattr.write_text(
                f"#!/usr/bin/env bash\n"
                f"echo \"$1\" >> {str(chattr_log)!r}\n"
                f"exit 0\n",
                encoding="utf-8",
            )
            chattr.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
            env["AGENTIC_GUIDANCE_SRC"] = str(ROOT / "guidance" / "user")
            env["AGENTIC_GUIDANCE_DST"] = str(dst)

            # Sync copies files after chattr -i and locks with chattr +i
            sync_res = subprocess.run(
                ["bash", str(GUIDANCE_SYNC)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(sync_res.returncode, 0, sync_res.stderr)
            self.assertTrue((dst / "RULES.md").is_file())
            self.assertTrue((dst / "AGENTS.md").is_file())
            self.assertEqual(chattr_log.read_text(encoding="utf-8").splitlines(), ["-i", "+i"])

            # Check exits 0 when identical
            check_ok = subprocess.run(
                ["bash", str(GUIDANCE_CHECK)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual((check_ok.returncode, check_ok.stdout), (0, ""))

            # Introduce drift: modify RULES.md, remove WATCHDOG.md, add extra file in rules/
            (dst / "RULES.md").write_text("tampered\n", encoding="utf-8")
            (dst / "WATCHDOG.md").unlink()
            (dst / "rules" / "rogue.md").write_text("extra\n", encoding="utf-8")

            check_drift = subprocess.run(
                ["bash", str(GUIDANCE_CHECK)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(check_drift.returncode, 1)
            drift_lines = check_drift.stdout.strip().splitlines()
            self.assertEqual(len(drift_lines), 3, drift_lines)
            self.assertTrue(any("RULES.md" in line for line in drift_lines))
            self.assertTrue(any("WATCHDOG.md" in line for line in drift_lines))
            self.assertTrue(any("rules/rogue.md" in line for line in drift_lines))

            # Re-running sync repairs all three drifts
            sync_res2 = subprocess.run(
                ["bash", str(GUIDANCE_SYNC)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(sync_res2.returncode, 0, sync_res2.stderr)
            check_fixed = subprocess.run(
                ["bash", str(GUIDANCE_CHECK)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual((check_fixed.returncode, check_fixed.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
