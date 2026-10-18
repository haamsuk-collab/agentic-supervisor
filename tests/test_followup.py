"""Follow-up missions (SPEC-E section 6): admin.continue_mission, the planner's follow-up prompt, the changed
baseline rule (inherited_pass) and the review at iteration 0."""

import json
import re
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentic import admin, cards, commands, config, loop, planner, report
from cardcheck import card_text
from agentic.admin import MissionActive, TooManyMissions, UnknownMission
from agentic.config import MAX_ACTIVE_MISSIONS, REQUEST_MAX_BYTES
from agentic.ledger import Ledger, LedgerError
from agentic.mission import Mission, MissionError, parse_mission
from helpers import (ALWAYS_PASSES, NOTHING, Crash, FakeCommands, PlannedTestCase, SupervisorTestCase, TimedTestCase,
                     fix, make_mission)
from test_review import CHEAT_FILE, FINDING, cheating_fix

PARAGRAPH = ("The work folder already contains the previous result. Write criteria for the complete final state "
             "the owner wants: keep what already works, and add or change what the new requirement asks.")
NEW_TEXT = "이제 c3 기능도 넣어 주세요."
ONE_STEP = [{"model": "m-one", "thinking": "high", "iteration_minutes": 5}]
GIVE_UP_FAST = {"escalate_after": 1, "give_up_after": 1}


class FollowUpCase(SupervisorTestCase):
    """continue_mission with a fake runuser/systemctl: `inactive` for every unit unless `states` says otherwise.
    The fake `cp -a <src>/. <dst>/` really copies, so a follow-up can run afterwards."""

    def setUp(self):
        super().setUp()
        self.states: dict[str, str] = {}
        self.fail_on: str | None = None
        self.fake = FakeCommands(self.respond)
        patcher = mock.patch("agentic.commands.run_command", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, argv):
        if argv[:2] == ["systemctl", "is-active"]:
            state = self.states.get(argv[2].removeprefix("agentic@").removesuffix(".service"), "inactive")
            return commands.CommandResult(0 if state == "active" else 3, state + "\n", "")
        if self.fail_on and self.fail_on in argv:
            return commands.CommandResult(1, "", "boom")
        if argv[:5] == ["runuser", "-u", "agwork", "--", "mkdir"]:
            Path(argv[6]).mkdir(parents=True, exist_ok=True)
        elif argv[:6] == ["runuser", "-u", "agwork", "--", "cp", "-a"]:
            shutil.copytree(argv[6].removesuffix("/."), argv[7], dirs_exist_ok=True)
        return commands.CommandResult(0, "", "")

    def launches(self) -> list[list[str]]:
        # chattr +a comes from the loop making a parent's ledger append-only (SPEC-F section 4), not from a launch.
        return [c for c in self.fake.calls if c[:2] not in (["systemctl", "is-active"], ["chattr", "+a"])]

    def parent_folder(self, mission_id: str, request: str | None = "원래 요청입니다.") -> None:
        """A finished-looking parent: a mission folder (with request.md unless `request` is None)."""
        if request is None:
            self.cfg.mission_dir(mission_id).mkdir(parents=True, mode=0o700)
        else:
            admin.create_request(self.cfg, mission_id, request.encode("utf-8"))
        self.cfg.workdir(mission_id).mkdir(parents=True, exist_ok=True)

    def missions(self) -> list[str]:
        return admin.mission_ids(self.cfg)


class RefusalTests(FollowUpCase):
    def test_unknown_parent_creates_nothing(self):
        with self.assertRaises(UnknownMission):
            admin.continue_mission(self.cfg, "nope", NEW_TEXT)
        self.assertEqual((self.fake.calls, self.missions()), ([], []))

    def test_a_bad_parent_id_is_refused(self):
        with self.assertRaises(MissionError):
            admin.continue_mission(self.cfg, "Bad Id", NEW_TEXT)
        self.assertEqual(self.fake.calls, [])

    def test_running_parent_is_refused_and_nothing_is_created(self):
        self.parent_folder("p")
        for state in ("active", "activating"):
            with self.subTest(state=state):
                self.states["p"] = state
                with self.assertRaises(MissionActive) as caught:
                    admin.continue_mission(self.cfg, "p", NEW_TEXT)
                self.assertEqual(str(caught.exception), "아직 진행 중인 미션입니다")
                self.assertEqual(self.missions(), ["p"])
                self.assertEqual(self.launches(), [])
        self.assertIsInstance(MissionActive(), MissionError)

    def test_a_bad_text_is_refused_and_nothing_is_created(self):
        self.parent_folder("p")
        for text in ("  \n", "a\0b", "x" * (REQUEST_MAX_BYTES + 1)):
            with self.subTest(text=text[:6]):
                with self.assertRaises(MissionError):
                    admin.continue_mission(self.cfg, "p", text)
        self.assertEqual((self.missions(), self.launches()), (["p"], []))

    def test_too_many_running_missions_refuse_the_follow_up(self):
        self.parent_folder("p")
        for n in range(MAX_ACTIVE_MISSIONS):
            self.parent_folder(f"busy{n}")
            self.states[f"busy{n}"] = "active"
        with self.assertRaises(TooManyMissions):
            admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertNotIn("p-c1", self.missions())
        self.assertEqual(self.launches(), [])

    def test_a_new_text_that_cannot_fit_is_refused_with_a_korean_message(self):
        self.parent_folder("p")
        with self.assertRaises(MissionError) as caught:
            admin.continue_mission(self.cfg, "p", "a" * (REQUEST_MAX_BYTES - 10))
        self.assertTrue(str(caught.exception).startswith("이어서 맡길 내용이 너무 깁니다."))
        self.assertEqual((self.missions(), self.launches()), (["p"], []))


class IdAndLaunchTests(FollowUpCase):
    def test_ids_count_up_and_a_follow_up_of_a_follow_up_shares_the_base(self):
        self.parent_folder("x")
        self.assertEqual(admin.continue_mission(self.cfg, "x", NEW_TEXT), "x-c1")
        self.assertEqual(admin.continue_mission(self.cfg, "x", NEW_TEXT), "x-c2")
        self.assertEqual(admin.continue_mission(self.cfg, "x-c1", NEW_TEXT), "x-c3")
        self.assertEqual(self.missions(), ["x", "x-c1", "x-c2", "x-c3"])

    def test_continuing_x_c1_gives_x_c2(self):
        self.parent_folder("x")
        self.parent_folder("x-c1")
        self.assertEqual(admin.continue_mission(self.cfg, "x-c1", NEW_TEXT), "x-c2")

    def test_a_gap_is_filled_with_the_smallest_free_number(self):
        for mission_id in ("x", "x-c2"):
            self.parent_folder(mission_id)
        self.assertEqual(admin.continue_mission(self.cfg, "x", NEW_TEXT), "x-c1")

    def test_only_one_trailing_suffix_is_dropped(self):
        self.parent_folder("a-c1-c2")
        self.assertEqual(admin.continue_mission(self.cfg, "a-c1-c2", NEW_TEXT), "a-c1-c1")

    def test_a_too_long_id_uses_the_default_scheme(self):
        parent = "a" * 39
        self.parent_folder(parent)
        new_id = admin.continue_mission(self.cfg, parent, NEW_TEXT)
        self.assertRegex(new_id, r"^m-\d{8}-\d{6}(-\d+)?$")
        self.assertTrue(config.valid_id(new_id))
        self.assertEqual(self.cfg.parent_file(new_id).read_text(), parent + "\n")
        second = admin.continue_mission(self.cfg, parent, NEW_TEXT)
        self.assertNotEqual(second, new_id)

    def test_id_that_just_fits_is_used(self):
        parent = "a" * 38  # a...a-c1 has 41 characters
        self.parent_folder(parent)
        self.assertEqual(admin.continue_mission(self.cfg, parent, NEW_TEXT), parent + "-c1")

    def test_commands_run_in_order_as_the_worker_user(self):
        self.parent_folder("p")
        new_id = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        old, new = self.cfg.workdir("p"), self.cfg.workdir(new_id)
        self.assertEqual(self.launches(), [
            ["runuser", "-u", "agwork", "--", "mkdir", "-p", str(new)],
            ["runuser", "-u", "agwork", "--", "cp", "-a", f"{old}/.", f"{new}/"],
            ["systemctl", "enable", "--now", f"agentic@{new_id}.service"]])

    def test_files_and_modes(self):
        self.parent_folder("p")
        new_id = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertEqual(stat.S_IMODE(self.cfg.mission_dir(new_id).stat().st_mode), 0o700)
        for path in (self.cfg.request_file(new_id), self.cfg.parent_file(new_id)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.cfg.parent_file(new_id).read_text(encoding="utf-8"), "p\n")

    def test_a_failing_command_stops_the_launch(self):
        self.parent_folder("p")
        self.fail_on = "cp"
        with self.assertRaises(admin.CommandFailed):
            admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertEqual([c[4] if c[0] == "runuser" else c[0] for c in self.launches()], ["mkdir", "cp"])

    def test_the_parent_folder_content_is_copied(self):
        self.parent_folder("p")
        (self.cfg.workdir("p") / "sub").mkdir()
        (self.cfg.workdir("p") / "sub" / "a.txt").write_text("hello")
        new_id = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertEqual((self.cfg.workdir(new_id) / "sub" / "a.txt").read_text(), "hello")


class ComposeTests(FollowUpCase):
    """request.md of the follow-up: the exact text."""

    def run_parent(self, mission: dict) -> None:
        self.install(mission)
        loop.run(mission["id"], self.cfg)

    def test_planned_parent_with_a_result_on_both_sides(self):
        self.play(fix("c1"), NOTHING, NOTHING)
        self.run_parent(make_mission(("c1", "c2"), mission_id="p", ladder=ONE_STEP, limits=GIVE_UP_FAST))
        self.parent_folder_request("p", "날씨 프로그램을 만들어 주세요.\n두 줄입니다.  \n")
        new_id = admin.continue_mission(self.cfg, "p", "  " + NEW_TEXT + "\n\n")
        text = self.cfg.request_file(new_id).read_text(encoding="utf-8")
        self.assertEqual(text, (
            "[이어서 하는 미션] 이전 미션: p\n"
            "[이전 요청]\n날씨 프로그램을 만들어 주세요.\n두 줄입니다.\n"
            "[이전 결과] 확인됨: c1 Requirement c1 / 안 됨: c2 Requirement c2 (3번 시도 후 포기)\n"
            "[작업 폴더] 이전 미션의 결과물이 그대로 들어 있습니다.\n"
            f"[새 요구]\n{NEW_TEXT}\n"))

    def parent_folder_request(self, mission_id: str, request: str) -> None:
        self.cfg.request_file(mission_id).write_text(request, encoding="utf-8")

    def test_hand_written_parent_and_nothing_achieved_or_open(self):
        self.play(fix("c1", "c2"))
        self.run_parent(make_mission(("c1", "c2"), mission_id="p"))
        new_id = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        text = self.cfg.request_file(new_id).read_text(encoding="utf-8")
        self.assertEqual(text, (
            "[이어서 하는 미션] 이전 미션: p\n"
            "[이전 요청]\n(직접 작성한 미션)\n"
            "[이전 결과] 확인됨: c1 Requirement c1; c2 Requirement c2 / 안 됨: 없음\n"
            "[작업 폴더] 이전 미션의 결과물이 그대로 들어 있습니다.\n"
            f"[새 요구]\n{NEW_TEXT}\n"))

    def test_a_parent_without_a_ledger_has_no_result_on_either_side(self):
        self.parent_folder("p", None)
        new_id = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertIn("[이전 결과] 확인됨: 없음 / 안 됨: 없음\n", self.cfg.request_file(new_id).read_text("utf-8"))

    def test_a_broken_parent_ledger_is_refused(self):
        self.play(fix("c1", "c2"))
        self.run_parent(make_mission(("c1", "c2"), mission_id="p"))
        self.cfg.ledger_file("p").write_text("garbage\n")
        with self.assertRaises(MissionError) as caught:
            admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertNotIsInstance(caught.exception, LedgerError)
        self.assertEqual((self.missions(), self.launches()), (["p"], []))


def item(mark: str, cid: str, text: str, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(mark=mark, id=cid, text=text, reason=reason)


def briefing_of(*items) -> SimpleNamespace:
    return SimpleNamespace(evidence=list(items), remaining=[i for i in items if i.mark != "✅"])


class ShorteningTests(TimedTestCase):
    HEAD, TAIL = "[이어서 하는 미션] 이전 미션: p\n[이전 요청]\n", "[작업 폴더] 이전 미션의 결과물이 그대로 들어 있습니다.\n"

    def size(self, text: str) -> int:
        return len(text.encode("utf-8"))

    def test_a_text_that_fits_is_not_changed(self):
        text = admin.compose_followup_request("p", "요청", briefing_of(item("✅", "c1", "하나")), "새 요구")
        self.assertEqual(text, f"{self.HEAD}요청\n[이전 결과] 확인됨: c1 하나 / 안 됨: 없음\n{self.TAIL}[새 요구]\n새 요구\n")
        self.assertLessEqual(self.size(text), REQUEST_MAX_BYTES)

    def test_a_long_previous_request_is_cut_first_and_ends_with_an_ellipsis(self):
        briefing = briefing_of(item("✅", "c1", "하나"), item("❌", "c2", "둘", "3번 시도 후 포기"))
        new = "새 요구 " * 100
        text = admin.compose_followup_request("p", "가나다라" * 6000, briefing, new)
        self.assertLessEqual(self.size(text), REQUEST_MAX_BYTES)
        previous = text.split("[이전 요청]\n", 1)[1].split("\n[이전 결과]", 1)[0]
        self.assertTrue(previous.endswith("…"))
        self.assertTrue(previous.startswith("가나다라가나다라"))
        self.assertIn("[이전 결과] 확인됨: c1 하나 / 안 됨: c2 둘 (3번 시도 후 포기)\n", text)
        self.assertTrue(text.endswith(f"[새 요구]\n{new.strip()}\n"))
        # The longest prefix that fits: one more character would not.
        longer = previous[:-1] + "가" + "…"
        self.assertGreater(self.size(text.replace(previous, longer)), REQUEST_MAX_BYTES)
        again = admin.compose_followup_request("p", "가나다라" * 6000, briefing, new)
        self.assertEqual(again, text, "deterministic")

    def test_a_long_result_is_cut_after_the_request_when_the_request_is_already_minimal(self):
        briefing = briefing_of(*[item("✅", f"c{n}", "긴 설명 " * 20) for n in range(120)])
        text = admin.compose_followup_request("p", "요청", briefing, "새 요구")
        self.assertLessEqual(self.size(text), REQUEST_MAX_BYTES)
        self.assertIn("[이전 요청]\n…\n", text)
        result = text.split("[이전 결과] ", 1)[1].split("\n[작업 폴더]", 1)[0]
        self.assertTrue(result.startswith("확인됨: c0 긴 설명"))
        self.assertTrue(result.endswith("…"))
        self.assertTrue(text.endswith("[새 요구]\n새 요구\n"))

    def test_a_new_text_that_alone_is_too_long_raises(self):
        with self.assertRaises(MissionError) as caught:
            admin.compose_followup_request("p", "요청", None, "a" * (REQUEST_MAX_BYTES - 10))
        self.assertTrue(str(caught.exception).startswith("이어서 맡길 내용이 너무 깁니다."))

    def test_the_cut_works_on_characters_not_bytes(self):
        text = admin.compose_followup_request("p", "😀" * 9000, None, "새 요구")
        self.assertLessEqual(self.size(text), REQUEST_MAX_BYTES)
        text.encode("utf-8")  # no broken character


class PlannedFollowUpCase(PlannedTestCase, FollowUpCase):
    """A parent folder with worker files, then a follow-up planned and run by the stub planner and worker."""

    def parent_done(self, *, worker_plan, mission=None, request=None) -> None:
        """Run mission `p` (hand-written, ladder of one step, gives up fast) and forget the stub's call logs."""
        self.play(*worker_plan)
        self.install(mission or make_mission(("c1", "c2"), mission_id="p", ladder=ONE_STEP, limits=GIVE_UP_FAST))
        loop.run("p", self.cfg)
        for name in ("calls.jsonl", "planner_calls.jsonl"):
            (self.stub_dir / name).unlink(missing_ok=True)

    def follow_up(self, text: str = NEW_TEXT) -> str:
        return admin.continue_mission(self.cfg, "p", text)

    def kinds(self, mission_id: str) -> list[str]:
        return self.types(mission_id)

    def baseline(self, mission_id: str) -> dict:
        return next(r["data"] for r in self.records(mission_id) if r["type"] == "baseline")


class PlannerTests(PlannedFollowUpCase):
    def test_follow_up_mission_json_has_the_parent_and_the_design_prompt_the_paragraph(self):
        self.parent_done(worker_plan=[fix("c1")] + [NOTHING] * 5)
        new_id = self.follow_up()
        self.play(fix("c2"))
        self.assertEqual(loop.run(new_id, self.cfg), "complete")
        mission = json.loads(self.cfg.mission_file(new_id).read_text(encoding="utf-8"))
        self.assertEqual(mission["parent"], "p")
        self.assertIn("[이어서 하는 미션] 이전 미션: p", mission["request"])
        (design,) = self.planner_calls("design")
        task_lines = design["prompt"].splitlines()
        self.assertEqual(sum(1 for line in task_lines if line == PARAGRAPH), 1)
        at = task_lines.index(PARAGRAPH)
        self.assertTrue(task_lines[at - 1].startswith("- The approach is advice for the worker."))
        started = next(r["data"] for r in self.records(new_id) if r["type"] == "mission_started")
        self.assertEqual(started["parent"], "p")
        # Other steps do not get the paragraph.
        for call in self.planner_calls():
            if call["step"] != "design":
                self.assertNotIn(PARAGRAPH, call["prompt"])

    def test_a_normal_mission_has_no_parent_and_no_paragraph(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.new(), 0, self.err)
        self.assertNotIn("parent", self.mission_json())
        self.assertNotIn(PARAGRAPH, self.planner_calls("design")[0]["prompt"])
        started = next(r["data"] for r in self.records() if r["type"] == "mission_started")
        self.assertNotIn("parent", started)

    def test_a_request_that_looks_like_a_follow_up_is_not_one(self):
        self.play(fix("c1", "c2"))
        self.assertEqual(self.new("[이어서 하는 미션] 이전 미션: other\n무엇인가 만들어 주세요."), 0, self.err)
        self.assertNotIn("parent", self.mission_json())
        self.assertNotIn(PARAGRAPH, self.planner_calls("design")[0]["prompt"])

    def test_the_empty_folder_veto_still_runs_on_an_empty_folder(self):
        self.parent_done(worker_plan=[fix("c1")] + [NOTHING] * 5)
        new_id = self.follow_up()
        self.plan_script(script={"checks": [{"pass_empty": ["c1"]}, {}]})
        self.play(fix("c2"))
        loop.run(new_id, self.cfg)
        steps = [r["data"] for r in self.records(new_id) if r["type"] == "plan_step" and r["data"]["step"] == "critique"]
        self.assertEqual(steps[0]["empty_folder"]["c1"], planner.EMPTY_FOLDER_PROBLEM)
        self.assertEqual(len(self.planner_calls("checks")), 2, "c1 was re-written after the veto")

    def test_resume_after_a_crash_keeps_the_follow_up_design(self):
        self.parent_done(worker_plan=[fix("c1")] + [NOTHING] * 5)
        new_id = self.follow_up()
        self.play(fix("c2"))
        real_append, count = Ledger.append, [0]

        def append(ledger, record_type, data):
            record = real_append(ledger, record_type, data)
            count[0] += 1
            if count[0] == 3:  # plan_started, brief, design are durable
                raise Crash
            return record

        with mock.patch.object(Ledger, "append", append):
            with self.assertRaises(Crash):
                loop.run(new_id, self.cfg)
        self.assertEqual(loop.run(new_id, self.cfg), "complete")
        self.assertEqual(len(self.planner_calls("design")), 1)
        self.assertEqual(json.loads(self.cfg.mission_file(new_id).read_text(encoding="utf-8"))["parent"], "p")

    def test_an_invalid_parent_file_is_a_mission_error(self):
        admin.create_request(self.cfg, "q", "요청".encode())
        self.cfg.parent_file("q").write_text("Not A Valid Id\n")
        with self.assertRaises(MissionError):
            loop.run("q", self.cfg)
        self.assertEqual(self.planner_calls(), [])


class BaselineTests(PlannedFollowUpCase):
    def test_a_check_that_passes_on_the_inherited_folder_is_achieved_not_invalid(self):
        self.parent_done(worker_plan=[fix("c1")] + [NOTHING] * 5)
        new_id = self.follow_up()
        self.play(fix("c2"))
        self.assertEqual(loop.run(new_id, self.cfg), "complete")
        baseline = self.baseline(new_id)
        self.assertEqual((baseline["invalid"], baseline["inherited_pass"]), ([], ["c1"]))
        self.assertTrue(baseline["results"]["c1"]["passed"])
        self.assertFalse(baseline["results"]["c2"]["passed"])
        (call,) = self.calls()
        self.assertIn("[c1] 요구 사항 c1 — currently: PASSING", call["prompt"])
        self.assertIn("[c2] 요구 사항 c2 — currently: FAILING", call["prompt"])
        passed = {r["data"]["criterion"]: r["data"]["passed"] for r in self.records(new_id)
                  if r["type"] == "check_result"}
        self.assertEqual(passed, {"c1": True, "c2": True})
        finished = self.records(new_id)[-1]["data"]
        self.assertEqual((finished["status"], finished["iterations"]), ("complete", 1))
        self.assertEqual([p["criterion"] for p in finished["proofs"]], ["c1", "c2"])

    def test_all_passing_at_baseline_runs_the_review_at_iteration_0_and_completes(self):
        self.parent_done(worker_plan=[fix("c1", "c2")])
        new_id = self.follow_up()
        self.assertEqual(loop.run(new_id, self.cfg), "complete")
        types = self.kinds(new_id)
        # SPEC-F section 2: inherited passes count only after the canary held for them. A pass also needs a proof
        # from the final checks, so the inherited passes are checked once more (iteration 0) before finishing.
        self.assertEqual(types[types.index("baseline"):],
                         ["baseline", "canary", "review", "check_result", "check_result", "mission_finished"])
        confirms = [r for r in self.records(new_id) if r["type"] == "check_result"]
        self.assertEqual([(r["data"]["iteration"], r["data"]["criterion"], r["data"]["passed"], r["data"]["confirm"])
                          for r in confirms], [(0, "c1", True, True), (0, "c2", True, True)])
        canary_data = next(r["data"] for r in self.records(new_id) if r["type"] == "canary")
        self.assertEqual(canary_data["iteration"], 0)
        self.assertTrue(all(result["held"] for result in canary_data["results"].values()))
        self.assertEqual(self.baseline(new_id)["inherited_pass"], ["c1", "c2"])
        self.assertEqual(self.baseline(new_id)["invalid"], [])
        (call,) = self.planner_calls("review")
        self.assertIn("Your step: review.", call["prompt"])
        review = next(r["data"] for r in self.records(new_id) if r["type"] == "review")
        self.assertEqual(review["iteration"], 0)
        self.assertEqual(self.calls(), [], "no worker call")
        finished = self.records(new_id)[-1]["data"]
        self.assertEqual((finished["status"], finished["iterations"]), ("complete", 0))
        self.assertEqual(finished["proofs"], [{"criterion": r["data"]["criterion"], "seq": r["seq"], "hash": r["hash"]}
                                              for r in confirms])

    def test_a_veto_at_iteration_0_sends_the_worker_back_as_usual(self):
        self.parent_done(worker_plan=[cheating_fix("c1", "c2")])
        new_id = self.follow_up()
        self.plan_script(script={"review": [{"findings": [FINDING]}, {}]})
        self.play(fix("c1", "c2"))
        self.assertEqual(loop.run(new_id, self.cfg), "complete")
        types = self.kinds(new_id)
        # SPEC-F: the canary confirms the inherited passes first; the inherited solution.py gets a mutation score.
        self.assertEqual(types[types.index("baseline"):], [
            "baseline", "canary", "mutation", "review", "review_veto", "iteration_worker", "check_result",
            "check_result", "canary", "review", "mission_finished"])
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("Reviewer finding", self.calls()[0]["prompt"])
        self.assertIn(CHEAT_FILE["solution.py"].splitlines()[0][:20], self.calls()[0]["prompt"])

    def test_a_mission_without_a_parent_keeps_the_old_rule(self):
        mission = make_mission(("c1", "c2"), checks={"c1": ALWAYS_PASSES})
        self.play(fix("c2"))
        self.assertEqual(self.run_mission(mission), "partial")
        baseline = self.baseline("demo")
        self.assertEqual(baseline["invalid"], ["c1"])
        self.assertNotIn("inherited_pass", baseline)

    def test_a_planned_mission_without_a_parent_is_not_reviewed_at_iteration_0(self):
        self.plan_script(criteria=["c1"])
        self.play(NOTHING, fix("c1"))
        self.assertEqual(self.new(), 0, self.err)
        types = self.types()
        self.assertNotIn("review", types[:types.index("iteration_worker")])
        self.assertEqual([r["data"]["iteration"] for r in self.records() if r["type"] == "review"], [2])

    def test_old_baseline_records_without_inherited_pass_replay(self):
        state = loop.LoopState()
        state.criteria = [("c1", "x"), ("c2", "y")]
        state.apply({"type": "baseline", "seq": 1, "ts": "2026-01-01T00:00:00.000000Z",
                     "data": {"results": {"c1": {"passed": True}, "c2": {"passed": False}}, "invalid": ["c1"]}})
        self.assertEqual(state.status, {"c1": loop.INVALID, "c2": loop.FAILING})
        self.assertFalse(state.inherited_pass)


class MissionFileTests(TimedTestCase):
    def raw(self, **extra) -> dict:
        return {**make_mission(), **extra}

    def test_parent_is_an_optional_valid_id_shown_in_the_public_view(self):
        mission = parse_mission(self.raw(parent="p-c1"))
        self.assertEqual((mission.parent, mission.public_view()["parent"]), ("p-c1", "p-c1"))
        plain = parse_mission(self.raw())
        self.assertIsNone(plain.parent)
        self.assertNotIn("parent", plain.public_view())
        self.assertIsInstance(plain, Mission)

    def test_an_invalid_parent_is_refused(self):
        for bad in ("Bad Id", "", 5, None, "../x"):
            with self.subTest(parent=bad):
                with self.assertRaises(MissionError):
                    parse_mission(self.raw(parent=bad))


class PastEvidenceFollowUpTests(PlannedFollowUpCase):
    def test_parent_report_and_status_mark_results_as_past_only_when_followup_exists(self):
        self.parent_done(worker_plan=[fix("c1", "c2")])
        self.assertIn("감독자 검사 기준 ", self.report_text("p"))
        self.assertNotIn("이후 이어서 한 미션:", self.report_text("p"))
        self.assertNotIn("이후 이어서 한 미션:", admin.mission_status(self.cfg, "p"))
        self.assertNotIn("이후 이어서 한 미션:", card_text(cards.status_card(report.load_briefing(self.cfg, "p"), [])))

        c1 = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertEqual(c1, "p-c1")
        self.assertIn("이후 이어서 한 미션: p-c1", self.report_text("p"))
        self.assertIn("이후 이어서 한 미션: p-c1", admin.report_text(self.cfg, "p"))
        self.assertIn("이후 이어서 한 미션: p-c1", admin.mission_status(self.cfg, "p"))
        self.assertIn("이후 이어서 한 미션: p-c1", card_text(cards.status_card(report.load_briefing(self.cfg, "p"), [])))
        self.assertNotIn("이후 이어서 한 미션:", admin.mission_status(self.cfg, "p-c1"))

        c2 = admin.continue_mission(self.cfg, "p", NEW_TEXT)
        self.assertEqual(c2, "p-c2")
        self.assertIn("이후 이어서 한 미션: p-c1, p-c2", self.report_text("p"))
        self.assertIn("이후 이어서 한 미션: p-c1, p-c2", admin.mission_status(self.cfg, "p"))
        self.assertIn("이후 이어서 한 미션: p-c1, p-c2", card_text(cards.status_card(report.load_briefing(self.cfg, "p"), [])))

        with mock.patch.object(config, "LANGUAGE", "en"):
            self.assertIn("Continued in follow-up mission: p-c1, p-c2",
                          report.build(self.cfg, "p", self.records("p")).text)
            self.assertIn("Continued in follow-up mission: p-c1, p-c2",
                          admin.mission_status(self.cfg, "p"))
            self.assertIn("Continued in follow-up mission: p-c1, p-c2",
                          card_text(cards.status_card(report.load_briefing(self.cfg, "p"), [])))


if __name__ == "__main__":
    import unittest
    unittest.main()
