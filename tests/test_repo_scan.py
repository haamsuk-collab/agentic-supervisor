"""SPEC-G section 6: the repository scan finds host names, IPs, tokens, chat ids, API keys and personal home paths."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, TESTS_DIR, TimedTestCase  # noqa: E402
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

    def test_link_local_metadata_addresses_pass_but_the_next_range_does_not(self):
        for line in ("blocked = {'169.254.169.254'}", "range 169.254.0.0/16 up to 169.254.255.255"):
            with self.subTest(line=line):
                self.assertEqual(rules(line, "agentic/safety.py"), [])
        self.assertEqual(rules("peer " + ".".join(("169", "255", "0", "1")), "agentic/safety.py"), ["IP address"])

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

    def test_vendored_folders_skip_only_ip_addresses(self):
        vendored_file = "guidance/user/skills/insane-search/engine/safety.py"
        outside_file = "guidance/user/rules/other.md"
        self.assertEqual(rules("ip " + ADDR, vendored_file), [])
        self.assertEqual(rules("ip " + ADDR, outside_file), ["IP address"])
        self.assertEqual(rules("ssh agent@" + "t1-" + "naver", vendored_file), ["banned string"])
        self.assertEqual(rules("host build-box-07", vendored_file), ["host name"])
        self.assertEqual(rules("key sk-" + "a" * 30, vendored_file), ["API key"])

    def test_every_vendored_folder_is_credited_in_notice(self):
        self.assertIn("guidance/user/skills/insane-search/", repo_scan.VENDORED)
        notice_text = (ROOT / "NOTICE").read_text(encoding="utf-8")
        for prefix in repo_scan.VENDORED:
            with self.subTest(prefix=prefix):
                self.assertTrue(prefix.endswith("/"))
                self.assertIn(prefix, notice_text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "NOTICE").write_text("no third-party credits here\n", encoding="utf-8")
            self.assertEqual(
                repo_scan.scan(root, HOSTS),
                [f"NOTICE:1: uncredited vendored path: {p}" for p in repo_scan.VENDORED],
            )

    def test_empty_git_checkout_is_reported_while_empty_non_git_dir_passes(self):
        with tempfile.TemporaryDirectory() as non_git:
            self.assertEqual(repo_scan.scan(Path(non_git), HOSTS), [])
        with tempfile.TemporaryDirectory() as git_tmp:
            root = Path(git_tmp)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
            (root / "untracked.txt").write_text("fine\n", encoding="utf-8")
            findings = repo_scan.scan(root, HOSTS)
            self.assertEqual(findings, [".git:0: empty repository: no tracked files to scan"])
            cli = subprocess.run([sys.executable, str(TESTS_DIR / "repo_scan.py"), str(root)],
                                 capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(cli.returncode, 1)
            self.assertIn("empty repository", cli.stdout)

    def test_git_log_authors_are_scanned_for_banned_strings_and_host_names(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

        def make_repo(author_name: str, author_email: str) -> tuple[str, list[str]]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
                (root / "a.txt").write_text("clean content\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, env=env)
                subprocess.run(
                    ["git", "-C", str(root), "-c", f"user.name={author_name}",
                     "-c", f"user.email={author_email}", "commit", "-q", "-m", "initial"],
                    check=True, env=env,
                )
                short = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, check=True, env=env,
                ).stdout.strip()
                return short, repo_scan.scan(root, HOSTS)

        _, clean_findings = make_repo("Clean Author", "clean@example.org")
        self.assertEqual(clean_findings, [])

        banned_word = "t1-" + "naver"
        short_banned, banned_findings = make_repo("Agent", f"agent@{banned_word}.local")
        self.assertEqual(banned_findings, [f"commit {short_banned} author: banned string: {banned_word}"])

        short_host, host_findings = make_repo("dev on build-box-07", "clean@example.org")
        self.assertEqual(host_findings, [f"commit {short_host} author: host name: build-box-07"])


class RepositoryScanTests(TimedTestCase):
    def test_the_published_files_are_clean(self):
        findings = repo_scan.scan(ROOT)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_published_guidance_is_the_generic_user_and_project_set_without_vendored_tests(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "guidance"], capture_output=True, text=True,
                                 check=True).stdout.splitlines()
        for path in ("guidance/user/AGENTS.md", "guidance/user/RULES.md", "guidance/project/AGENTS.md",
                     "guidance/project/TEMPLATE-RULES.md", "guidance/user/skills/insane-search/SKILL.md"):
            self.assertIn(path, tracked)
        self.assertEqual([p for p in tracked if p.startswith("guidance/user/skills/insane-search/engine/tests/")], [])
        self.assertEqual({p.split("/")[1] for p in tracked}, {"user", "project"})
        agents = (ROOT / "guidance/user/AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents.startswith("# Agent guide (example: edit for your server)\n"))
        self.assertEqual(repo_scan.scan_text("guidance/user/AGENTS.md", agents, repo_scan.host_names()), [])

    def test_stage_specs_are_published_only_under_docs_specs(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                                 check=True).stdout.splitlines()
        specs = [p for p in tracked if repo_scan.SPEC_FILE_RE.search(p)]
        self.assertEqual([p for p in specs if not p.startswith("docs/specs/")], [])
        for name in ("SPEC.md", "SPEC-C.md", "SPEC-D.md", "SPEC-E.md", "SPEC-F.md", "SPEC-G.md"):
            self.assertIn(f"docs/specs/{name}", specs)


if __name__ == "__main__":
    unittest.main()
