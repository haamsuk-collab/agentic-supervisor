"""SPEC-H sections 1 and 3: the free mechanical filter before the critique, and isolated Python checks."""

import re
import subprocess
import sys

from agentic import planner
from helpers import ROOT, PlannedTestCase, fix

PY = "/usr/bin/python3.12"


def inline(code: str, argv=(PY, "-I")) -> dict:
    return {"argv": [*argv, "-c", code], "timeout": 30}


NO_ASSERT = {"argv": [PY, "-I", "_agentic_check_c1.py"], "timeout": 30,
             "files": {"_agentic_check_c1.py": "print(open('c1.txt').read())\n"}}


class CheckProblemTests(PlannedTestCase):
    def test_scripts_that_can_fail_pass_the_filter(self):
        for code in ("assert open('c1.txt').read() == 'ok'",
                     "import sys\nif open('c1.txt').read() != 'ok':\n    sys.exit(1)",
                     "import sys\nsys.exit(0 if open('c1.txt').read() == 'ok' else 1)",
                     "import os\nos._exit(2)",
                     "raise SystemExit(3)",
                     "raise RuntimeError('wrong')",
                     "import unittest\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertTrue(1)"):
            with self.subTest(code=code):
                self.assertIsNone(planner.check_problem(inline(code)))

    def test_scripts_that_cannot_fail_are_rejected(self):
        for code in ("print('ALL TESTS PASSED')", "import sys\nsys.exit(0)", "raise SystemExit", "exit()",
                     "this is not python ("):
            with self.subTest(code=code):
                self.assertEqual(planner.check_problem(inline(code)), planner.NO_ASSERTION_PROBLEM)
        self.assertEqual(planner.check_problem(NO_ASSERT), planner.NO_ASSERTION_PROBLEM)

    def test_only_isolated_python_is_accepted(self):
        self.assertEqual(planner.check_problem({"argv": ["true"], "timeout": 5}), planner.TRUE_ONLY_PROBLEM)
        self.assertEqual(planner.check_problem({"argv": ["/bin/true", "x"], "timeout": 5}), planner.TRUE_ONLY_PROBLEM)
        for argv in ((PY,), ("python3.12", "-I"), (PY, "-E"), ("/usr/bin/python3", "-I"), ("sh", "-c")):
            with self.subTest(argv=argv):
                self.assertEqual(planner.check_problem(inline("assert False", argv)), planner.NOT_ISOLATED_PROBLEM)

    def test_vague_words_are_found_as_words(self):
        for text, word in (("출력을 적절히 정리한다", "적절히"), ("검색이 잘 동작한다", "잘"),
                           ("Handles errors properly", "properly"), ("Retries APPROPRIATELY", "appropriately"),
                           ("Log as  needed", "as needed")):
            with self.subTest(text=text):
                self.assertEqual(planner.vague_problem(text), planner.VAGUE_PROBLEM.format(word=word))
        for text in ("잘못된 입력은 종료 코드 2로 거부한다", "길이를 잘라 80자로 맞춘다", "properlyish", "improperly formed"):
            with self.subTest(text=text):
                self.assertIsNone(planner.vague_problem(text))

    def test_the_checks_prompt_asks_for_isolated_python(self):
        self.assertIn("/usr/bin/python3.12 -I", planner.CHECKS_TASK)
        self.assertIn('"/usr/bin/python3.12", "-I"', planner.CHECKS_SCHEMA)
        self.assertIn("never by importing from the work folder", planner.CHECKS_TASK)


def reviewed(call: dict) -> list[str]:
    found = re.search(r"^Review exactly these criterion ids: (.+)$", call["prompt"], re.MULTILINE)
    return [cid.strip() for cid in found.group(1).split(",")]


class PipelineFilterTests(PlannedTestCase):
    def filters(self, step: str) -> list[dict]:
        return [r["data"] for r in self.records() if r["type"] == "plan_filter" and r["data"]["step"] == step]

    def test_a_rejected_check_skips_the_critique_and_goes_back_to_the_checks_step(self):
        self.plan_script(script={"checks": [{"checks": {"c1": NO_ASSERT}}, {}]})
        self.play(fix("c1", "c2"))
        self.assertEqual(self.new(), 0, self.err)
        first, second = self.filters("critique")
        self.assertEqual((first["round"], first["checked"], first["rejected"]), (0, 2, 1))
        self.assertEqual(first["results"], {"c1": planner.NO_ASSERTION_PROBLEM, "c2": None})
        self.assertEqual((second["round"], second["checked"], second["rejected"]), (1, 1, 0))
        self.assertEqual([reviewed(c) for c in self.planner_calls("critique")], [["c2"], ["c1"]],
                         "the model reviews only what passed the free filter")
        rerun = self.planner_calls("checks")[1]["prompt"]
        self.assertIn(planner.NO_ASSERTION_PROBLEM, rerun, "the rejection goes into the veto round")
        self.assertEqual([c["id"] for c in self.mission_json()["criteria"]], ["c1", "c2"])

    def test_when_nothing_passes_the_filter_no_critique_call_is_made(self):
        bad = {"argv": ["true"], "timeout": 5}
        self.plan_script(script={"checks": [{"checks": {"c1": bad, "c2": bad}}, {}]})
        self.play(fix("c1", "c2"))
        self.assertEqual(self.new(), 0, self.err)
        self.assertEqual(self.filters("critique")[0]["rejected"], 2)
        critiques = [s for s in self.steps("critique")]
        self.assertEqual([s["round"] for s in critiques], [1], "round 0 needed no model call")

    def test_a_check_rejected_in_every_round_is_dropped(self):
        self.plan_script(script={"checks": [{"checks": {"c1": NO_ASSERT}}]})
        self.play(fix("c2"))
        self.assertEqual(self.new(), 0, self.err)
        dropped = next(r["data"]["dropped"] for r in self.records() if r["type"] == "plan_finished")
        self.assertEqual([(d["id"], d["problem"]) for d in dropped], [("c1", planner.NO_ASSERTION_PROBLEM)])

    def test_a_vague_criterion_goes_back_to_the_design_step_once(self):
        self.plan_script(script={"design": [{"texts": {"c1": "결과를 적절히 보여 준다"}}, {}]})
        self.play(fix("c1", "c2"))
        self.assertEqual(self.new(), 0, self.err)
        self.assertEqual(len(self.planner_calls("design")), 2)
        self.assertIn("VAGUE CRITERIA", self.planner_calls("design")[1]["prompt"])
        first, second = self.filters("design")
        self.assertEqual((first["rejected"], second["rejected"]), (1, 0))
        self.assertEqual([c["text"] for c in self.mission_json()["criteria"]], ["요구 사항 c1", "요구 사항 c2"])

    def test_a_criterion_still_vague_after_the_second_design_is_dropped(self):
        self.plan_script(script={"design": [{"texts": {"c1": "It works properly"}}]})
        self.play(fix("c2"))
        self.assertEqual(self.new(), 0, self.err)
        self.assertEqual(len(self.planner_calls("design")), 2, "back to the design step only once")
        self.assertEqual([c["id"] for c in self.mission_json()["criteria"]], ["c2"])
        dropped = next(r["data"]["dropped"] for r in self.records() if r["type"] == "plan_finished")
        self.assertEqual(dropped, [{"id": "c1", "text": "It works properly",
                                    "problem": planner.VAGUE_PROBLEM.format(word="properly")}])
        self.assertEqual([reviewed(c) for c in self.planner_calls("critique")], [["c2"]])

    def test_a_resumed_planning_does_not_record_the_filter_twice(self):
        self.plan_script(script={"checks": [{"checks": {"c1": NO_ASSERT}}, {}]}, kill_at_step="critique")
        self.play(fix("c1", "c2"))
        path = self.stub_dir / "request.txt"
        path.write_text("요청", encoding="utf-8")
        first = subprocess.run([sys.executable, "-m", "agentic", "new", str(path), "--id", "demo"], cwd=ROOT,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, -9, first.stderr)
        self.assertEqual(self.types()[-1], "plan_filter")
        second = subprocess.run([sys.executable, "-m", "agentic", "run", "demo"], cwd=ROOT, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([(f["step"], f["round"]) for f in self.filters("critique")], [("critique", 0),
                                                                                      ("critique", 1)])
