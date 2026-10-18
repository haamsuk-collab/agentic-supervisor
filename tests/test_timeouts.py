import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentic import checks
from helpers import TESTS_DIR, TimedTestCase, alive, wait_until


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def run_limited(body, limit: float):
    """Run `body` as a test with a time limit; returns (result, seconds taken)."""
    class Inner(TimedTestCase):
        TIMEOUT = limit

        def test_body(self):
            body()

    result = unittest.TestResult()
    started = time.monotonic()
    Inner("test_body").run(result)
    return result, time.monotonic() - started


class TimeLimitTests(TimedTestCase):
    def test_endless_loop_fails_instead_of_hanging(self):
        def spin():
            while True:
                pass

        result, taken = run_limited(spin, 0.5)
        self.assertLess(taken, 10)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("time limit", result.errors[0][1])

    def test_endless_sleep_fails_instead_of_hanging(self):
        result, taken = run_limited(lambda: time.sleep(600), 0.5)
        self.assertLess(taken, 10)
        self.assertEqual(len(result.errors), 1)

    def test_a_timed_out_test_does_not_leave_its_child_process_running(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pidfile = Path(tmp.name) / "pid"
        code = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)"

        def wait_forever():
            checks.run_process([sys.executable, "-c", code, str(pidfile)], pidfile.parent, 600)

        result, taken = run_limited(wait_forever, 1.0)
        self.assertLess(taken, 10)
        self.assertEqual(len(result.errors), 1)
        child = int(pidfile.read_text())
        wait_until(lambda: not alive(child), timeout=5, what="the child to die")

    def test_a_test_within_its_limit_passes(self):
        result, _ = run_limited(lambda: time.sleep(0.05), 5)
        self.assertTrue(result.wasSuccessful())

    def test_an_enclosing_timer_survives_a_nested_limited_run(self):
        run_limited(lambda: None, 5)
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, self.TIMEOUT)

    def test_every_test_in_the_suite_has_a_finite_time_limit(self):
        suite = unittest.defaultTestLoader.discover(str(TESTS_DIR))
        tests = list(flatten(suite))
        self.assertGreater(len(tests), 50)
        for test in tests:
            with self.subTest(test=test.id()):
                self.assertIsInstance(test, TimedTestCase)
                self.assertLessEqual(test.TIMEOUT, 300)


if __name__ == "__main__":
    unittest.main()
