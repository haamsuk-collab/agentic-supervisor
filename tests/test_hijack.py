"""SPEC-H section 1: a worker must not be able to hijack a check."""

import json
import os
import sys
from unittest import mock

from agentic import checks, report, review
from agentic.mission import parse_check
from helpers import SupervisorTestCase, fix, make_mission, notices

# Exits 1 unless something makes `import json` stop the process with 0 first.
IMPORTS_JSON = {"argv": [sys.executable, "-c", "import json\nraise SystemExit(1)"], "timeout": 30}
HIJACK = "import sys\nsys.exit(0)\n"
PRINT_ENV = {"argv": [sys.executable, "-c", "import json, os\nprint(json.dumps(dict(os.environ)))"], "timeout": 30}


class SnapshotHygieneTests(SupervisorTestCase):
    def workdir(self, files: dict[str, str]):
        work = self.cfg.workdir("demo")
        work.mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text, encoding="utf-8")
        return work

    def test_a_planted_standard_library_module_cannot_make_a_check_pass(self):
        work = self.workdir({"json.py": HIJACK})
        result = checks.run_check(self.cfg, "demo", 1, "c1", parse_check(IMPORTS_JSON, "c1"), work)
        self.assertFalse(result.passed, "json.py is removed from the snapshot, so the real json is imported")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual((work / "json.py").read_text(), HIJACK, "the worker folder itself is untouched")

    def test_every_kind_of_hijacking_file_is_found_and_ordinary_files_are_kept(self):
        work = self.workdir({
            "conftest.py": "", "sitecustomize.py": "", "usercustomize.py": "", "evil.pth": "",
            "sub/subprocess.py": "", "unittest/__init__.py": "", "cache/json.cpython-312.pyc": "",
            "solution.py": "", "myjson.py": "", "notes/json.txt": "", "email/readme.md": "", "pytest.ini": "",
        })
        found = checks.hijacking_files(work)
        self.assertEqual(found, ["cache/json.cpython-312.pyc", "conftest.py", "evil.pth", "sitecustomize.py",
                                 "sub/subprocess.py", "unittest", "usercustomize.py"])
        copy = self.stub_dir / "copy"
        checks.snapshot_tree(work, copy)
        self.assertEqual(checks.remove_hijacking_files(copy), found)
        self.assertEqual(checks.hijacking_files(copy), [])
        for kept in ("solution.py", "myjson.py", "notes/json.txt", "email/readme.md", "pytest.ini"):
            self.assertTrue((copy / kept).exists(), kept)
        self.assertTrue((work / "conftest.py").exists(), "only the copy loses them")

    def test_check_files_are_written_after_the_cleanup(self):
        work = self.workdir({})
        own = {"argv": [sys.executable, "conftest.py"], "timeout": 30, "files": {"conftest.py": "raise SystemExit(0)"}}
        self.assertTrue(checks.run_check(self.cfg, "demo", 1, "c1", parse_check(own, "c1"), work).passed)

    def test_a_check_gets_a_clean_environment(self):
        work = self.workdir({".home/.profile": "planted"})
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil.py", "SECRET": "x"}):
            result = checks.run_check(self.cfg, "demo", 1, "c1", parse_check(PRINT_ENV, "c1"), work)
        env = json.loads(result.stdout)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertTrue(env["HOME"].endswith("/.home"))
        self.assertIn(f"{self.cfg.state}/scratch/", env["HOME"], "HOME lies inside the snapshot")
        self.assertEqual([k for k in env if k.startswith("PYTHON") or k == "SECRET"], [])
        self.assertLessEqual(set(env) - {"PATH", "HOME"}, {"LC_CTYPE"}, "only Python's own locale coercion")

    def test_the_planted_home_folder_is_replaced_by_an_empty_one(self):
        work = self.workdir({".home/.profile": "planted"})
        probe = {"argv": [sys.executable, "-c", "import os, sys\nsys.exit(len(os.listdir(os.environ['HOME'])))"],
                 "timeout": 30}
        self.assertTrue(checks.run_check(self.cfg, "demo", 1, "c1", parse_check(probe, "c1"), work).passed)

    def test_the_systemd_unit_gets_the_same_clean_environment(self):
        argv = checks.build_command(parse_check(PRINT_ENV, "c1"), "/snap/x", "systemd")
        self.assertIn("--setenv=PATH=/usr/bin:/bin", argv)
        self.assertIn("--setenv=HOME=/snap/x/.home", argv)
        self.assertLess(argv.index("--setenv=HOME=/snap/x/.home"), argv.index("--"))


class HygieneRecordTests(SupervisorTestCase):
    def test_hijacking_files_are_recorded_once_and_listed_in_the_report(self):
        mission = make_mission(("c1",), checks={"c1": IMPORTS_JSON}, limits={"escalate_after": 1, "give_up_after": 1},
                               ladder=[{"model": "m", "thinking": "high", "iteration_minutes": 5}])
        self.play({"write": {"json.py": HIJACK, "conftest.py": ""}}, {"write": {"json.py": HIJACK}})
        self.assertNotEqual(self.run_mission(mission), "complete")
        hygiene = [r["data"] for r in self.records() if r["type"] == "hygiene"]
        self.assertEqual(hygiene, [{"iteration": 1, "files": ["conftest.py", "json.py"]}],
                         "the unchanged list of the second iteration is not recorded again")
        results = [r["data"]["passed"] for r in self.records() if r["type"] == "check_result"]
        self.assertEqual(results, [False] * len(results))
        line = "- ⚠️ 검사를 가로챌 수 있는 파일이라 검사 때 제외함: conftest.py, json.py"
        self.assertIn(line, notices(self.report_text()))
        self.assertTrue((self.cfg.workdir("demo") / "json.py").exists())

    def test_a_clean_folder_records_nothing(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(), "complete")
        self.assertNotIn("hygiene", self.types())
        self.assertNotIn("검사를 가로챌", self.report_text())

    def test_the_english_report_names_the_files_too(self):
        from agentic import config
        self.play({"write": {"c1.txt": "ok", "c2.txt": "ok", "sitecustomize.py": ""}})
        self.assertEqual(self.run_mission(), "complete")
        with mock.patch.object(config, "LANGUAGE", "en"):
            text = report.render(self.records(), [])
        self.assertIn("Could hijack a check, so left out when checking: sitecustomize.py", text)


class ReviewPromptTests(SupervisorTestCase):
    def test_the_reviewer_looks_for_files_that_shadow_tooling(self):
        self.assertIn("files that shadow standard or test tooling", review.TASK)
