import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helpers import TimedTestCase

from agentic import spool


class OrderTests(TimedTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agentic-spool-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def listed(self) -> list[str]:
        dfd = spool.open_dir(self.dir)
        try:
            return spool.list_names(dfd)
        finally:
            os.close(dfd)

    def test_items_made_in_one_nanosecond_with_one_mtime_are_listed_in_the_order_they_were_made(self):
        frozen = time.time_ns() + 10 ** 12
        with mock.patch("time.time_ns", return_value=frozen):
            made = [spool.write_item(self.dir, {"n": n}) for n in range(30)]
            document = spool.write_document(self.dir, b"x")
        for name in made + [document]:
            os.utime(os.path.join(self.dir, name), ns=(frozen, frozen))
        for name in made:
            self.assertRegex(name, r"^\d{20}-[0-9a-f]{32}$")
        self.assertEqual(made, sorted(made))
        self.assertEqual(self.listed(), made)


if __name__ == "__main__":
    unittest.main()
