import hashlib
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentic import checks, config
from agentic.mission import Check
from helpers import TimedTestCase, alive


class CheckTests(TimedTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.cfg = config.load({"AGENTIC_STATE": str(self.tmp / "state"), "AGENTIC_CHECK_MODE": "local",
                                "AGENTIC_WORKSPACES": str(self.tmp / "ws")})

    def run_check(self, argv, timeout=30, files=None, network=False):
        check = Check(tuple(argv), timeout, network, files or {})
        return checks.run_check(self.cfg, "m", 1, "c1", check, self.work)

    def py(self, code, *args, **kwargs):
        return self.run_check([sys.executable, "-c", code, *args], **kwargs)

    def test_exit_zero_passes_and_nonzero_fails(self):
        self.assertTrue(self.py("import sys; sys.exit(0)").passed)
        failed = self.py("import sys; sys.exit(3)")
        self.assertFalse(failed.passed)
        self.assertEqual(failed.exit_code, 3)

    def test_output_never_decides_the_result(self):
        result = self.py("import sys; print('ALL TESTS PASSED'); sys.exit(1)")
        self.assertFalse(result.passed)
        self.assertIn("ALL TESTS PASSED", result.stdout)

    def test_missing_program_fails(self):
        result = self.run_check(["/nonexistent/program"])
        self.assertFalse(result.passed)
        self.assertIsNone(result.exit_code)

    def test_timeout_fails_and_kills_the_whole_process_group(self):
        pidfile = self.tmp / "child.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        result = self.py(code, str(pidfile), timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.passed)
        child = int(pidfile.read_text())
        for _ in range(100):
            if not alive(child):
                break
            time.sleep(0.05)
        self.assertFalse(alive(child), "grandchild survived the timeout")

    def test_argv_is_not_run_through_a_shell(self):
        tricky = "x; touch MARK && echo $HOME *"
        code = (
            "import os, sys\n"
            f"sys.exit(0 if sys.argv[1] == {tricky!r} and not os.path.exists('MARK') else 1)\n"
        )
        self.assertTrue(self.py(code, tricky).passed)

    def test_stdin_is_dev_null(self):
        code = "import os, sys; sys.exit(0 if os.path.samestat(os.fstat(0), os.stat('/dev/null')) else 1)"
        self.assertTrue(self.py(code).passed)

    def test_given_stdin_bytes_reach_the_child_in_full(self):
        data = ("한글 $HOME\n" * 20_000).encode()  # far over a pipe buffer
        code = "import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"
        proc = checks.run_process([sys.executable, "-c", code], self.tmp, 30, stdin=data)
        self.assertEqual(proc.exit_code, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), hashlib.sha256(data).hexdigest())

    def test_a_child_that_ignores_its_stdin_does_not_block_the_caller(self):
        started = time.monotonic()
        proc = checks.run_process([sys.executable, "-c", "pass"], self.tmp, 30, stdin=b"x" * 1_000_000)
        self.assertEqual(proc.exit_code, 0, proc.stderr)
        self.assertLess(time.monotonic() - started, 10)

    def test_output_is_cut_to_the_last_4kb(self):
        result = self.py("print('a' * 10000 + 'END')")
        self.assertEqual(len(result.stdout.encode()), 4096)
        self.assertTrue(result.stdout.rstrip().endswith("END"))

    def test_runs_on_a_snapshot_and_deletes_it(self):
        (self.work / "keep.txt").write_text("x")
        (self.work / ".git").mkdir()
        (self.work / ".git" / "config").write_text("x")
        (self.work / "pkg" / "node_modules").mkdir(parents=True)
        code = (
            "import os, sys\n"
            "ok = os.path.exists('keep.txt') and not os.path.exists('.git')\n"
            "ok = ok and not os.path.exists('pkg/node_modules') and open('extra/data.txt').read() == 'hi'\n"
            "open('junk.txt', 'w').write('written by the check')\n"
            "print(os.getcwd())\n"
            "sys.exit(0 if ok else 1)\n"
        )
        result = self.py(code, files={"extra/data.txt": "hi"})
        self.assertTrue(result.passed, result.stderr)
        snapshot = Path(result.stdout.strip())
        self.assertEqual(snapshot.parent, self.cfg.scratch_dir("m", 1))
        self.assertRegex(snapshot.name, r"^c1-[0-9a-f]{32}$")
        self.assertFalse(snapshot.exists())
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), [".git", "keep.txt", "pkg"])

    def test_snapshot_folder_names_are_random_and_not_reused(self):
        names = set()
        for _ in range(5):
            result = self.py("import os; print(os.getcwd())")
            names.add(Path(result.stdout.strip()).name)
        self.assertEqual(len(names), 5)
        iteration_dir = self.cfg.scratch_dir("m", 1)
        self.assertEqual(list(iteration_dir.parent.iterdir()) if iteration_dir.parent.exists() else [], [])

    def test_check_cannot_find_its_snapshot_by_guessing_the_path(self):
        code = (
            "import os, sys\n"
            "guess = os.path.join(os.path.dirname(os.getcwd()), 'c1')\n"
            "sys.exit(0 if not os.path.exists(guess) else 1)\n"
        )
        self.assertTrue(self.py(code).passed)

    def test_check_files_never_follow_a_symlink_planted_by_the_worker(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.work / "checks").symlink_to(outside)
        result = self.py("import sys; sys.exit(0 if open('checks/t.py').read() == 'secret' else 1)",
                         files={"checks/t.py": "secret"})
        self.assertTrue(result.passed, result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.work / "checks").is_symlink())

    def test_fifo_in_the_worker_folder_does_not_hang_the_snapshot(self):
        os.mkfifo(self.work / "pipe")
        self.assertTrue(self.py("import os, sys; sys.exit(0 if not os.path.exists('pipe') else 1)").passed)

    def test_systemd_command_wrapping(self):
        check = Check(("prog", "arg"), 77, False, {})
        cmd = checks.build_command(check, "/s/snap", "systemd")
        self.assertEqual(cmd[:6], ["systemd-run", "--wait", "--pipe", "--collect", "--quiet", "-p"])
        props = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
        self.assertEqual(props, [
            "DynamicUser=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes",
            "NoNewPrivileges=yes", "RuntimeMaxSec=77", "WorkingDirectory=/s/snap",
            "ReadWritePaths=/s/snap", "PrivateNetwork=yes"])
        self.assertEqual(cmd[-3:], ["--", "prog", "arg"])
        with_network = checks.build_command(Check(("prog",), 5, True, {}), "/s", "systemd")
        self.assertNotIn("PrivateNetwork=yes", with_network)
        self.assertEqual(checks.build_command(check, "/s/snap", "local"), ["prog", "arg"])


if __name__ == "__main__":
    unittest.main()
