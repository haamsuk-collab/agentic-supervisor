"""SPEC-F section 3: the mutation score, and the one automatic strengthening of weak checks."""

import json
import sys

from agentic import briefing, config, mutation
from agentic.mission import parse_mission
from helpers import PlannedTestCase, StepClock, SupervisorTestCase, fix, make_mission

GOOD = "def grade(x):\n    if x < 10:\n        return 'low'\n    return 'high'\n"
OFF_BY_ONE = "def grade(x):\n    if x <= 10:\n        return 'low'\n    return 'high'\n"


def check(code: str) -> dict:
    """A check as SPEC-H section 1 wants it: isolated Python that loads calc.py by its explicit path."""
    load = ("import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('calc', 'calc.py')\n"
            "calc = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(calc)\n"
            "grade = calc.grade\n")
    return {"argv": ["/usr/bin/python3.12", "-I", "-c", load + f"sys.exit(0 if {code} else 1)"], "timeout": 30}


# Reads calc.py, but only one of its four mutants (return 'low' -> return None) changes grade(5).
WEAK = check("grade(5) is not None")
# Kills all four: the flipped comparison, 10 -> 11, and both returns -> None.
STRONG = check("grade(9) == 'low' and grade(10) == 'high' and grade(5) == 'low' and grade(20) == 'high'")
ONE_STEP = [{"model": "m-one", "thinking": "high", "iteration_minutes": 5}]
MANY_POINTS = "".join(f"def f{i}(x):\n    return x < {i} and x != {i + 1}\n" for i in range(5))


def write_calc(source: str) -> dict:
    return {"write": {"calc.py": source}}


class GenerateTests(SupervisorTestCase):
    def folder(self, files: dict[str, str]):
        work = self.stub_dir / "work"
        work.mkdir(exist_ok=True)
        for name, text in files.items():
            (work / name).write_text(text, encoding="utf-8")
        return work

    def test_the_same_mission_id_gives_the_same_mutants(self):
        work = self.folder({"a.py": MANY_POINTS, "b.py": GOOD})
        first = mutation.generate(work, ["a.py", "b.py"], "weather")
        self.assertEqual(len(first), config.MUTATION_MAX_MUTANTS)
        self.assertEqual(mutation.generate(work, ["b.py", "a.py"], "weather"), first)
        others = {tuple(mutation.generate(work, ["a.py", "b.py"], f"m{i}")) for i in range(10)}
        self.assertGreater(len(others), 1, "the choice depends on the mission id")

    def test_each_operator_changes_exactly_one_point(self):
        source = "def f(x):\n    return x < 3 and x == True\n"  # exactly six points, so all are chosen
        expected = {
            mutation.COMPARE_FLIP: ["return x >= 3 and x == True", "return x < 3 and x != True"],
            mutation.AND_OR: ["return x < 3 or x == True"],
            mutation.TRUE_FALSE: ["return x < 3 and x == False"],
            mutation.INT_PLUS_ONE: ["return x < 4 and x == True"],
            mutation.RETURN_NONE: ["return None"],
        }
        work = self.folder({"f.py": source})
        found: dict[str, list[str]] = {}
        for m in mutation.generate(work, ["f.py"], "id"):
            found.setdefault(m.operator, []).append(mutation.mutated_source(source, m.index))
        self.assertEqual(sorted(found), sorted(expected))
        for operator, fragments in expected.items():
            for fragment in fragments:
                self.assertTrue(any(fragment in text for text in found[operator]), (operator, fragment))

    def test_a_strong_check_kills_a_flipped_comparison_and_a_weak_one_does_not(self):
        work = self.folder({"calc.py": GOOD})
        (flip,) = [m for m in mutation.generate(work, ["calc.py"], "demo") if m.operator == mutation.COMPARE_FLIP]
        self.assertEqual(flip.line, 2)
        for checks, killed in ((STRONG, True), (WEAK, False)):
            with self.subTest(killed=killed):
                mission = parse_mission(make_mission(("c1",), checks={"c1": checks}))
                self.assertEqual(mutation.killed(self.cfg, mission, work, ["c1"], flip), killed)
        self.assertEqual((work / "calc.py").read_text(), GOOD, "mutants never touch the worker folder")

    def test_the_time_budget_records_the_rest_as_skipped(self):
        work = self.folder({"calc.py": GOOD})
        mission = parse_mission(make_mission(("c1",), checks={"c1": STRONG}))
        data = mutation.run(self.cfg, mission, work, ["c1"], ["calc.py"], StepClock(minutes=6))
        self.assertEqual((data["mutants"], data["killed"], data["skipped"]), (4, 1, 3))
        self.assertEqual([d["killed"] for d in data["details"]], [True, None, None, None])
        self.assertEqual([d["skipped"] for d in data["details"]], [False, True, True, True])
        self.assertFalse(mutation.weak(data), "one mutant that ran is too few to judge")
        self.assertNotIn("grade(9)", json.dumps(data), "no check content in the record")


class MutationLoopTests(SupervisorTestCase):
    def test_a_mission_without_python_files_records_no_mutation(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(), "complete")
        self.assertNotIn("mutation", self.types())
        self.assertIn("canary", self.types())

    def test_strong_checks_get_one_score_and_no_strengthening(self):
        self.play(write_calc(GOOD))
        self.assertEqual(self.run_mission(make_mission(("c1",), checks={"c1": STRONG})), "complete")
        (data,) = [r["data"] for r in self.records() if r["type"] == "mutation"]
        self.assertEqual((data["iteration"], data["mutants"], data["killed"], data["skipped"]), (1, 4, 4, 0))
        self.assertNotIn("checks_strengthened", self.types())


class StrengtheningTests(PlannedTestCase):
    """Hand-written missions with a weak check; the stub planner writes the stronger check."""

    def run_weak(self, *worker_plan) -> str:
        self.plan_script(script={"checks": [{"checks": {"c1": STRONG}}]})
        self.play(*worker_plan)
        return self.run_mission(make_mission(("c1",), checks={"c1": WEAK}, ladder=ONE_STEP,
                                             limits={"escalate_after": 2, "give_up_after": 3}))

    def mutations(self) -> list[dict]:
        return [r["data"] for r in self.records() if r["type"] == "mutation"]

    def test_new_checks_that_pass_give_a_second_score_and_a_notice_but_no_decision(self):
        self.assertEqual(self.run_weak(write_calc(GOOD)), "complete")
        types = self.types()
        tail = types[types.index("baseline"):]
        self.assertEqual([t for t in tail if t not in ("plan_step", "plan_filter")], [
            "baseline", "iteration_worker", "check_result", "canary", "mutation", "checks_update",
            "checks_strengthened", "check_result", "canary", "mutation", "mission_finished"])
        first, second = self.mutations()
        self.assertEqual((first["mutants"], first["killed"]), (4, 1))
        self.assertTrue(mutation.weak(first))
        self.assertEqual((second["mutants"], second["killed"]), (4, 4))
        strengthened = next(r["data"] for r in self.records() if r["type"] == "checks_strengthened")
        self.assertEqual((strengthened["ok"], strengthened["replaced"], strengthened["kept"]), (True, ["c1"], []))
        (call,) = self.planner_calls("checks")
        self.assertIn("SURVIVING MUTANTS", call["prompt"])
        self.assertIn('"operator": "compare_flip"', call["prompt"])
        self.assertNotIn("return 'low'", call["prompt"], "the planner never sees the worker's code")
        self.assertEqual(call["model"], config.PLANNER_MODELS["checks"][0].split(":")[0])
        self.assertEqual(len(self.planner_calls("critique")), 1)
        mission = json.loads(self.cfg.mission_file("demo").read_text(encoding="utf-8"))
        self.assertEqual(mission["criteria"][0]["check"]["argv"], STRONG["argv"])
        b = briefing.build(self.records(), None, [], None)
        self.assertIn("검사 강화: 변형 4개 중 1개 → 4개 중 4개", b.notices)
        self.assertEqual(b.decisions, [])
        self.assertEqual(len(self.calls()), 1, "the worker was not needed again")

    def test_new_checks_that_fail_on_the_current_work_send_the_loop_back_to_the_worker(self):
        self.assertEqual(self.run_weak(write_calc(OFF_BY_ONE), write_calc(GOOD)), "complete")
        types = [t for t in self.types() if t not in ("plan_step", "plan_filter")]
        tail = types[types.index("checks_update"):]
        self.assertEqual(tail, ["checks_update", "checks_strengthened", "check_result", "iteration_worker",
                                "check_result", "canary", "mutation", "mission_finished"])
        rechecked = [r["data"] for r in self.records() if r["type"] == "check_result"][1]
        self.assertFalse(rechecked["passed"])
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.types().count("checks_strengthened"), 1, "once per mission")
        b = briefing.build(self.records(), None, [], None)
        self.assertIn("검사 강화: 변형 4개 중 1개 → 4개 중 4개", b.notices)
        self.assertEqual(b.decisions, [])

    def test_a_failed_strengthening_is_recorded_and_the_mission_still_ends(self):
        self.plan_script(script={"checks": [{"exit": 1, "stderr": "boom"}]})
        self.play(write_calc(GOOD))
        self.assertEqual(self.run_mission(make_mission(("c1",), checks={"c1": WEAK}, ladder=ONE_STEP)), "complete")
        strengthened = next(r["data"] for r in self.records() if r["type"] == "checks_strengthened")
        self.assertFalse(strengthened["ok"])
        self.assertEqual(len(self.mutations()), 1)
        b = briefing.build(self.records(), None, [], None)
        self.assertIn("검사 강화: 변형 4개 중 1개 → 새 검사를 만들지 못함", b.notices)
        self.assertEqual(b.decisions, [])
