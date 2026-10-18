"""SPEC-G section 6: the repository scan finds host names, IPs, tokens, chat ids, API keys and personal home paths."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, TESTS_DIR, TimedTestCase

sys.path.insert(0, str(TESTS_DIR))
import repo_scan  # noqa: E402

HOSTS = ("build-box-07",)
# Planted samples are assembled at run time, so this file itself passes the scan of the repository.
ADDR = ".".join(("10", "20", "30", "40"))
CHAT = "123456" + "789"


def rules(text: str, rel: str = "docs/x.md") -> list[str]:
    return [f.split(": ")[1] for f in repo_scan.scan_text(rel, text, HOSTS)]


class ScanRuleTests(TimedTestCase):
    def test_each_rule_fires_on_a_planted_sample(self):
        token = "1234567890:" + "A" * 35
        samples = {
            "banned string": ["ssh agent@" + "t1-" + "naver", "server 223" + ".130.9.9 x", "mail haamsuk" + "@example.org"],
            "host name": ["deployed on build-box-07 yesterday"],
            "IP address": ["listen " + ".".join(("10", "0", "0", "7")), "peer " + ".".join(("8",) * 4)],
            "Telegram token": [f'"token": "{token}"'],
            "chat id": ['{"owner_id": ' + CHAT + "}", "chat_id = -100" + CHAT],
            "API key": ["key sk-" + "a" * 30, "AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "x" * 36,
                        "-----BEGIN OPENSSH " + "PRIVATE KEY-----"],
            "personal home path": ["cd /home/" + "alice/src", "/Users/" + "bob/.ssh"],
        }
        for rule, lines in samples.items():
            for line in lines:
                with self.subTest(rule=rule, line=line):
                    self.assertIn(rule, rules(line))

    def test_what_names_no_host_and_no_person_passes(self):
        clean = [
            "listen 127.0.0.1:4000 and 0.0.0.0",
            "example 192.0.2.10, 198.51.100.1, 203.0.113.5",
            "version 3.12.1 and 1.0.0",
            "the interactive user's home /home/agent/.omp/agent",
            '{"token": "<bot token>", "owner_id": <your numeric Telegram user id>}',
            "FakeTelegram(owner=4242)",
            "Copyright (c) 2026 haamsuk-collab",
        ]
        for line in clean:
            with self.subTest(line=line):
                self.assertEqual(rules(line), [])

    def test_only_spec_files_may_quote_the_banned_strings(self):
        line = "the strings `" + "t1-" + "naver" + "`"
        self.assertEqual(rules(line, "docs/specs/SPEC-G.md"), [])
        self.assertEqual(rules(line, "SPEC-G.md"), [])
        self.assertEqual(rules(line, "docs/SPEC-NOTES.md"), ["banned string"])
        self.assertEqual(rules("ip " + ADDR, "docs/specs/SPEC-G.md"), ["IP address"])

    def test_a_tree_without_git_is_walked_and_the_exit_code_reports_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("fine\n")
            ok = subprocess.run([sys.executable, str(TESTS_DIR / "repo_scan.py"), str(root)],
                                capture_output=True, text=True, timeout=60)
            self.assertEqual((ok.returncode, ok.stdout), (0, ""))
            (root / "sub").mkdir()
            (root / "sub" / "b.toml").write_text(f"x = 1\nhost = '{ADDR}'\n")
            (root / "bin.dat").write_bytes(b"\0" + ADDR.encode())
            bad = subprocess.run([sys.executable, str(TESTS_DIR / "repo_scan.py"), str(root)],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(bad.stdout, f"sub/b.toml:2: IP address: {ADDR}\n")


class RepositoryScanTests(TimedTestCase):
    def test_the_published_files_are_clean(self):
        findings = repo_scan.scan(ROOT)
        self.assertEqual(findings, [], "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
