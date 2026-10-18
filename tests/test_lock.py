import contextlib
import io
import subprocess
import sys
import unittest

from agentic import cli, loop
from agentic.ledger import LedgerError
from helpers import ROOT, SupervisorTestCase, fix, make_mission, wait_until

RUN = [sys.executable, "-m", "agentic", "run", "demo"]


class MissionLockTests(SupervisorTestCase):
    def spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(RUN, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def reap():
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()

        self.addCleanup(reap)
        return proc

    def test_second_run_is_refused_while_the_lock_is_held(self):
        mission_id = self.install(make_mission())
        self.play(fix("c1", "c2"))
        with loop.mission_lock(self.cfg, mission_id):
            with self.assertRaises(loop.MissionBusy):
                loop.run(mission_id, self.cfg)
        self.assertFalse(self.cfg.ledger_file(mission_id).exists(), "a refused run must not touch the ledger")
        self.assertEqual(self.calls(), [])
        self.assertEqual(loop.run(mission_id, self.cfg), "complete", "the lock must be free afterwards")

    def test_cli_reports_that_the_mission_is_already_running(self):
        mission_id = self.install(make_mission())
        out, err = io.StringIO(), io.StringIO()
        with loop.mission_lock(self.cfg, mission_id):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["run", mission_id]), 2)
                self.assertEqual(cli.main(["report", mission_id]), 2)
        self.assertIn("already running", err.getvalue())

    def test_lock_is_released_when_a_run_fails(self):
        mission_id = self.install(make_mission())
        self.cfg.ledger_file(mission_id).write_text("not a ledger\n")
        with self.assertRaises(LedgerError):
            loop.run(mission_id, self.cfg)
        with loop.mission_lock(self.cfg, mission_id):
            pass

    def test_two_supervisor_processes_cannot_run_the_same_mission(self):
        self.install(make_mission())
        self.play({"sleep": 3, **fix("c1", "c2")})
        first = self.spawn()
        wait_until(self.calls, what="the first supervisor's worker to start")
        second = subprocess.run(RUN, cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 2, second.stdout)
        self.assertIn("already running", second.stderr)
        out, err = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(self.types(), ["mission_started", "baseline", "iteration_worker",
                                        "check_result", "check_result", "canary", "mission_finished"])
        self.assertEqual(len(self.calls()), 1)

    def test_killing_the_supervisor_frees_the_lock_although_its_worker_keeps_running(self):
        self.install(make_mission())
        self.play({"sleep": 3, **fix("c1", "c2")})
        first = self.spawn()
        wait_until(self.calls, what="the worker to start")
        first.kill()
        first.wait(timeout=10)
        # The orphaned stub worker is still sleeping; it must not be holding the lock.
        with loop.mission_lock(self.cfg, "demo"):
            pass
        wait_until(lambda: (self.cfg.workdir("demo") / "c1.txt").exists(), what="the orphan worker to finish")

    def test_run_removes_snapshots_left_behind_by_a_killed_run(self):
        mission_id = self.install(make_mission())
        stale = self.cfg.scratch_root(mission_id) / "3" / "c1-deadbeef"
        stale.mkdir(parents=True)
        (stale / "leftover.txt").write_text("x")
        self.play(fix("c1", "c2"))
        loop.run(mission_id, self.cfg)
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
