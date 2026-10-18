import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import TimedTestCase

from agentic import config, results


class ResultsTestCase(TimedTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # the mission's state folder sits next to the worker folder
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "mission.json").write_text('{"secret": 1}')
        (self.state / "ledger.jsonl").write_text('{"secret": 2}\n')
        (self.state / "checks").mkdir()
        (self.state / "checks" / "c1.sh").write_text("exit 0\n")
        (self.state / "report.md").write_text("old report\n")
        self.work = self.state / "work"
        self.work.mkdir()

    def put(self, rel: str, data: bytes | str = b"data") -> Path:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def build(self, **kwargs):
        data, count = results.build_zip(self.work, "report-demo.md", "# report\n", **kwargs)
        return zipfile.ZipFile(io.BytesIO(data)), count


class BuildZipTests(ResultsTestCase):
    def test_zip_holds_worker_files_and_report(self):
        self.put("main.py", b"print(1)\n")
        self.put("pkg/sub/mod.py", b"x = 2\n")
        self.put("한글.txt", "안녕".encode())
        archive, count = self.build()
        self.assertEqual(sorted(archive.namelist()),
                         sorted(["report-demo.md", "main.py", "pkg/sub/mod.py", "한글.txt"]))
        self.assertEqual(count, 4)
        self.assertIsNone(archive.testzip())
        self.assertEqual(archive.read("main.py"), b"print(1)\n")
        self.assertEqual(archive.read("pkg/sub/mod.py"), b"x = 2\n")
        self.assertEqual(archive.read("한글.txt"), "안녕".encode())
        self.assertEqual(archive.read("report-demo.md"), "# report\n".encode())
        for info in archive.infolist():
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(info.external_attr >> 16, 0o100644)
            self.assertGreaterEqual(info.date_time[0], 1980)

    def test_deterministic_order_and_mtime(self):
        self.put("b.txt")
        self.put("a.txt")
        old = self.put("old.txt")
        os.utime(old, (0, 0))  # 1970 is clamped to 1980-01-01
        archive, _ = self.build()
        names = archive.namelist()
        self.assertEqual(names[1:], sorted(names[1:]))
        self.assertEqual(archive.getinfo("old.txt").date_time, (1980, 1, 1, 0, 0, 0))
        recent = self.put("recent.txt")
        os.utime(recent, (1_700_000_000, 1_700_000_000))
        archive, _ = self.build()
        self.assertEqual(archive.getinfo("recent.txt").date_time[0], 2023)

    def test_nothing_from_state_folder(self):
        self.put("result.txt")
        (self.work / "to_mission").symlink_to(self.state / "mission.json")
        (self.work / "to_state").symlink_to(self.state)
        (self.work / "to_ledger").symlink_to("../ledger.jsonl")
        archive, count = self.build()
        self.assertEqual(sorted(archive.namelist()), ["report-demo.md", "result.txt"])
        self.assertEqual(count, 2)
        blob = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(b"secret", blob)
        self.assertNotIn(b"old report", blob)

    def test_symlinks_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private")
        self.put("real.txt")
        (self.work / "file_link").symlink_to(outside / "private.txt")
        (self.work / "dir_link").symlink_to(outside)
        (self.work / "dangling").symlink_to(self.root / "missing")
        self.put("sub/keep.txt")
        (self.work / "sub" / "inner_link").symlink_to(outside)
        archive, _ = self.build()
        self.assertEqual(sorted(archive.namelist()), ["real.txt", "report-demo.md", "sub/keep.txt"])

    def test_symlinked_workdir_is_not_followed(self):
        self.put("real.txt")
        link = self.root / "link_work"
        link.symlink_to(self.work)
        with self.assertRaises(results.NoResults):
            results.build_zip(link, "report-demo.md", "x")

    def test_fifo_does_not_hang_and_is_skipped(self):
        self.put("real.txt")
        os.mkfifo(self.work / "pipe")
        os.mkfifo(self.work / "another_pipe")
        archive, _ = self.build()
        self.assertEqual(sorted(archive.namelist()), ["real.txt", "report-demo.md"])

    def test_snapshot_skip_folders_at_any_depth(self):
        self.put("keep.txt")
        for skipped in sorted(config.SNAPSHOT_SKIP):
            self.put(f"{skipped}/inside.txt")
            self.put(f"deep/{skipped}/inside.txt")
        self.put("deep/ok.txt")
        archive, _ = self.build()
        self.assertEqual(sorted(archive.namelist()), ["deep/ok.txt", "keep.txt", "report-demo.md"])

    def test_worker_file_named_like_report_is_replaced(self):
        self.put("report-demo.md", "forged by worker")
        self.put("other.txt")
        archive, count = self.build()
        self.assertEqual(archive.namelist().count("report-demo.md"), 1)
        self.assertEqual(archive.read("report-demo.md"), b"# report\n")
        self.assertEqual(count, 2)

    def test_report_name_only_matches_root_path(self):
        self.put("sub/report-demo.md", "nested")
        archive, count = self.build()
        self.assertEqual(archive.read("sub/report-demo.md"), b"nested")
        self.assertEqual(count, 2)

    def test_files_not_owned_by_owner_are_skipped(self):
        self.put("mine.txt")
        archive, _ = self.build(owner_uid=os.stat(self.work / "mine.txt").st_uid)
        self.assertIn("mine.txt", archive.namelist())
        with self.assertRaises(results.NoResults):
            self.build(owner_uid=os.getuid() + 12345)

    def test_non_utf8_names_are_skipped(self):
        self.put("fine.txt")
        try:
            bad = os.path.join(os.fsencode(self.work), b"bad-\xff.txt")
            with open(bad, "wb") as handle:
                handle.write(b"x")
        except OSError:
            self.skipTest("filesystem refuses non-UTF-8 names")
        archive, _ = self.build()
        self.assertEqual(sorted(archive.namelist()), ["fine.txt", "report-demo.md"])

    def test_empty_files_and_binary_content(self):
        blob = os.urandom(300_000)
        self.put("empty.txt", b"")
        self.put("blob.bin", blob)
        archive, count = self.build()
        self.assertEqual(archive.read("empty.txt"), b"")
        self.assertEqual(archive.read("blob.bin"), blob)
        self.assertEqual(count, 3)


class NoResultsTests(ResultsTestCase):
    def test_missing_folder(self):
        with self.assertRaises(results.NoResults):
            results.build_zip(self.root / "nope", "report-demo.md", "x")

    def test_empty_folder(self):
        with self.assertRaises(results.NoResults):
            self.build()

    def test_only_skipped_things(self):
        self.put(".git/config")
        self.put("report-demo.md")
        os.mkfifo(self.work / "pipe")
        (self.work / "link").symlink_to(self.state / "mission.json")
        (self.work / "empty_dir").mkdir()
        with self.assertRaises(results.NoResults):
            self.build()

    def test_workdir_is_a_file(self):
        path = self.put("file.txt")
        with self.assertRaises(results.NoResults):
            results.build_zip(path, "report-demo.md", "x")


class TooLargeTests(ResultsTestCase):
    def test_small_limit_stops_early(self):
        self.put("a.bin", os.urandom(50_000))
        self.put("b.bin", os.urandom(50_000))
        with self.assertRaises(results.TooLarge) as caught:
            self.build(max_bytes=40_000)
        self.assertTrue(caught.exception.at_least)
        self.assertGreater(caught.exception.size, 40_000)
        # stopped as soon as the limit was passed, not after the whole input
        self.assertLess(caught.exception.size, 40_000 + 70_000)

    def test_limit_boundary(self):
        self.put("a.txt", "hello")
        data, _ = results.build_zip(self.work, "report-demo.md", "x")
        results.build_zip(self.work, "report-demo.md", "x", max_bytes=len(data))
        with self.assertRaises(results.TooLarge):
            results.build_zip(self.work, "report-demo.md", "x", max_bytes=len(data) - 1)

    def test_real_limit_with_21_mb_of_random_data(self):
        for index in range(3):
            self.put(f"data{index}.bin", os.urandom(7 * 1024 * 1024))
        with self.assertRaises(results.TooLarge) as caught:
            results.build_zip(self.work, "report-demo.md", "x")
        self.assertGreater(caught.exception.size, config.DOCUMENT_MAX_BYTES)
        self.assertTrue(caught.exception.at_least)
        self.assertRegex(results.format_size(caught.exception.size, True), r"^2\d\.\dMB 이상$")


class FormatSizeTests(TimedTestCase):
    def test_format(self):
        self.assertEqual(results.format_size(0), "0.0MB")
        self.assertEqual(results.format_size(1024 * 1024), "1.0MB")
        self.assertEqual(results.format_size(int(21.3 * 1024 * 1024)), "21.3MB")
        self.assertEqual(results.format_size(int(20.1 * 1024 * 1024), at_least=True), "20.1MB 이상")
        self.assertEqual(results.format_size(21 * 1024 * 1024 + 1), "21.0MB")


if __name__ == "__main__":
    unittest.main()
