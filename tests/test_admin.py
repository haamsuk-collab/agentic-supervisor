import json
import os
import stat
import sys
from unittest import mock

from agentic import admin, commands
from agentic.mission import MissionError
from helpers import FakeCommands, SupervisorTestCase, fix, make_mission


def respond(argv):
    if argv[:2] == ["systemctl", "is-active"]:
        return commands.CommandResult(3, "inactive\n", "")
    return commands.CommandResult(0, "", "")


class AdminCase(SupervisorTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeCommands(respond)
        for patcher in (mock.patch("agentic.commands.run_command", self.fake),
                        mock.patch("os.geteuid", return_value=0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def mission_file(self, mission: dict):
        path = self.stub_dir / f"{mission['id']}.json"
        path.write_text(json.dumps(mission), encoding="utf-8")
        return path


class MissionCliTests(AdminCase):
    def test_refuses_to_run_unless_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            for args in (["list"], ["status", "demo"], ["stop", "demo"], ["report", "demo"],
                         ["new", str(self.mission_file(make_mission()))], ["start", "x"]):
                with self.subTest(args=args):
                    self.assertEqual(self.cli("mission", *args), 2)
                    self.assertIn("must be run as root", self.err)
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.cfg.state / "missions").exists())

    def test_new_creates_the_mission_and_starts_the_unit(self):
        request = self.stub_dir / "request.txt"
        request.write_text("날씨를 알려주는 프로그램을 만들어 주세요.", encoding="utf-8")
        self.assertEqual(self.cli("mission", "new", str(request), "weather"), 0, self.err)
        self.assertEqual(self.out.strip(), "mission weather started")
        self.assertEqual(self.fake.calls, [
            ["runuser", "-u", "agwork", "--", "mkdir", "-p", str(self.cfg.workdir("weather"))],
            ["systemctl", "enable", "--now", "agentic@weather.service"]])
        self.assertEqual(stat.S_IMODE(self.cfg.mission_dir("weather").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.cfg.request_file("weather").stat().st_mode), 0o600)

    def test_new_refuses_a_bad_request_an_bad_id_and_an_existing_mission(self):
        request = self.stub_dir / "request.txt"
        request.write_text("a request", encoding="utf-8")
        empty = self.stub_dir / "empty.txt"
        empty.write_text("  \n", encoding="utf-8")
        self.assertEqual(self.cli("mission", "new", str(empty)), 2)
        self.assertEqual(self.cli("mission", "new", str(request), "Bad Id"), 2)
        self.assertEqual(self.cli("mission", "new", str(request), "dup"), 0)
        self.assertEqual(self.cli("mission", "new", str(request), "dup"), 2)
        self.assertIn("already exists", self.err)
        launches = [c for c in self.fake.calls if c[:2] != ["systemctl", "is-active"]]
        self.assertEqual(len(launches), 2, "only the first dup was launched")

    def test_a_failing_command_is_an_error_exit(self):
        request = self.stub_dir / "request.txt"
        request.write_text("a request", encoding="utf-8")
        self.fake.respond = lambda argv: commands.CommandResult(1, "", "no such unit")
        self.assertEqual(self.cli("mission", "new", str(request), "m1"), 2)
        self.assertIn("no such unit", self.err)
        self.assertEqual(len(self.fake.calls), 1)  # the unit is not started when mkdir failed

    def test_start_installs_the_mission_file_and_starts_the_unit(self):
        path = self.mission_file(make_mission())
        self.assertEqual(self.cli("mission", "start", str(path)), 0, self.err)
        target = self.cfg.mission_file("demo")
        self.assertEqual(target.read_bytes(), path.read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self.fake.calls, [
            ["runuser", "-u", "agwork", "--", "mkdir", "-p", str(self.cfg.workdir("demo"))],
            ["systemctl", "enable", "--now", "agentic@demo.service"]])

    def test_start_refuses_an_existing_mission_with_different_content(self):
        self.assertEqual(self.cli("mission", "start", str(self.mission_file(make_mission()))), 0)
        self.fake.calls.clear()
        changed = self.mission_file(make_mission(ids=("c1",)))
        self.assertEqual(self.cli("mission", "start", str(changed)), 2)
        self.assertIn("differs", self.err)
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(MissionError):
            admin.start_mission(self.cfg, changed)
        # the same file again is fine (the unit is just enabled again)
        self.assertEqual(self.cli("mission", "start", str(self.mission_file(make_mission()))), 0)

    def test_start_refuses_an_invalid_mission_file(self):
        path = self.stub_dir / "bad.json"
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(self.cli("mission", "start", str(path)), 2)
        self.assertEqual(self.fake.calls, [])

    def test_list_status_report_and_stop(self):
        self.play(fix("c1", "c2"))
        self.run_mission()
        self.assertEqual(self.cli("mission", "list"), 0)
        (line,) = self.out.splitlines()
        self.assertTrue(line.startswith("demo · inactive · 마지막 기록: mission_finished ("), line)
        self.assertTrue(line.endswith(" · 반복 1회"))

        self.assertEqual(self.cli("mission", "status", "demo"), 0)
        self.assertEqual(self.out.splitlines()[0], line)
        status_part = self.out.split("\n\n")[0]
        self.assertEqual(len([l for l in status_part.splitlines() if l.startswith("- ")]), 5)
        self.assertTrue(self.out.endswith(self.report_text()))

        written = self.report_text()
        self.cfg.report_file("demo").unlink()
        self.assertEqual(self.cli("mission", "report", "demo"), 0, self.err)
        self.assertEqual(self.report_text(), written)

        self.fake.calls.clear()
        self.assertEqual(self.cli("mission", "stop", "demo"), 0)
        self.assertEqual(self.fake.calls, [["systemctl", "disable", "--now", "agentic@demo.service"]])
        self.assertEqual(self.cli("mission", "stop", "nope"), 2)
        self.assertIn("unknown mission", self.err)
        self.assertEqual(self.cli("mission", "status", "nope"), 2)
        self.assertEqual(self.cli("mission", "stop", "Bad Id"), 2)


class AdminFunctionTests(AdminCase):
    def test_list_is_empty_without_a_state_folder(self):
        self.assertEqual(admin.list_missions(self.cfg), [])

    def test_only_valid_mission_folders_are_listed(self):
        self.install(make_mission(mission_id="b-mission"))
        self.install(make_mission(mission_id="a-mission"))
        (self.cfg.state / "missions" / "Not Valid").mkdir()
        (self.cfg.state / "missions" / "a-file").write_text("x")
        self.assertEqual([line.split(" · ")[0] for line in admin.list_missions(self.cfg)], ["a-mission", "b-mission"])

    def test_state_is_the_stripped_output_even_when_systemctl_fails(self):
        self.install(make_mission())
        self.fake.respond = lambda argv: commands.CommandResult(3, "failed\n", "")
        self.assertEqual(admin.list_missions(self.cfg), ["demo · failed · 기록 없음"])
        self.fake.respond = lambda argv: commands.CommandResult(127, "", "no systemctl")
        self.assertEqual(admin.list_missions(self.cfg), ["demo · unknown · 기록 없음"])

    def test_a_broken_ledger_is_reported_not_raised(self):
        self.play(fix("c1", "c2"))
        self.run_mission()
        path = self.cfg.ledger_file("demo")
        path.write_text(path.read_text().replace("mission_started", "mission_startex"), encoding="utf-8")
        (line,) = admin.list_missions(self.cfg)
        self.assertEqual(line, "demo · inactive · 원장이 손상됨")
        self.assertEqual(admin.mission_status(self.cfg, "demo"), line)

    def test_report_text(self):
        self.install(make_mission())
        self.assertEqual(admin.report_text(self.cfg, "demo"), "아직 보고서가 없습니다")
        self.cfg.report_file("demo").write_text("# 보고서\n", encoding="utf-8")
        self.assertEqual(admin.report_text(self.cfg, "demo"), "# 보고서\n")
        with self.assertRaises(admin.UnknownMission):
            admin.report_text(self.cfg, "nope")

    def test_new_mission_validates_like_the_cli(self):
        for text in ("", "   ", "a\0b", "x" * (20 * 1024 + 1)):
            with self.subTest(text=text[:5]):
                with self.assertRaises(MissionError):
                    admin.new_mission(self.cfg, text)
        self.assertEqual(admin.new_mission(self.cfg, "x" * (20 * 1024), "big"), "big")
        self.assertEqual(self.fake.calls[-1], ["systemctl", "enable", "--now", "agentic@big.service"])

    def test_default_id_gets_a_suffix_when_the_folder_exists(self):
        with mock.patch("agentic.admin.datetime") as fake_datetime:
            import datetime
            fake_datetime.now.return_value = datetime.datetime(2026, 1, 2, 3, 4, 5)
            ids = [admin.new_mission(self.cfg, "request") for _ in range(3)]
        self.assertEqual(ids, ["m-20260102-030405", "m-20260102-030405-2", "m-20260102-030405-3"])

    def test_new_mission_is_refused_while_three_missions_are_running(self):
        states = {"a": "active", "b": "activating", "c": "active", "d": "inactive", "e": "failed"}
        for mission_id in states:
            self.cfg.mission_dir(mission_id).mkdir(parents=True)

        def answer(argv):
            if argv[:2] != ["systemctl", "is-active"]:
                return respond(argv)
            state = states[argv[2].removeprefix("agentic@").removesuffix(".service")]
            return commands.CommandResult(0 if state == "active" else 3, state + "\n", "")

        self.fake.respond = answer
        for mission_id in (None, "new-one"):
            with self.subTest(mission_id=mission_id):
                with self.assertRaises(admin.TooManyMissions) as caught:
                    admin.new_mission(self.cfg, "a request", mission_id)
                self.assertEqual(str(caught.exception), "돌고 있는 미션이 3개라 새 미션을 시작하지 않았습니다")
        self.assertEqual(sorted(os.listdir(self.cfg.state / "missions")), sorted(states), "nothing was created")
        self.assertEqual([c for c in self.fake.calls if c[:2] != ["systemctl", "is-active"]], [])
        request = self.stub_dir / "request.txt"
        request.write_text("a request", encoding="utf-8")
        self.assertEqual(self.cli("mission", "new", str(request), "new-one"), 2)
        self.assertIn("돌고 있는 미션이 3개라 새 미션을 시작하지 않았습니다", self.err)

        states["c"] = "deactivating"  # two running: the third may start
        self.assertEqual(admin.new_mission(self.cfg, "a request", "new-one"), "new-one")
        self.assertEqual(self.fake.calls[-1], ["systemctl", "enable", "--now", "agentic@new-one.service"])

    def test_event_lines_cover_every_record_type_and_hide_check_data(self):
        samples = {
            "mission_started": {"id": "demo", "criteria": [{"id": "c1"}, {"id": "c2"}]},
            "baseline": {"results": {"c1": {"stdout": "SECRET_OUT"}}, "invalid": ["c1"]},
            "iteration_worker": {"iteration": 2, "model": "m/x", "thinking": "high", "exit_code": 0,
                                 "timed_out": False, "note": "SECRET_NOTE", "stderr": "SECRET_ERR"},
            "check_result": {"iteration": 2, "criterion": "c1", "passed": False, "stdout": "SECRET_OUT",
                             "stderr": "SECRET_ERR", "exit_code": 1, "timed_out": False},
            "escalated": {"iteration": 2, "from_step": 0, "to_step": 1, "model": "m/y", "stalls": 2},
            "gave_up": {"iteration": 3, "criteria": ["c1"], "attempts": 3},
            "stopped_time": {"iteration": 3, "elapsed_seconds": 5.0},
            "mission_finished": {"status": "partial", "iterations": 3},
            "plan_started": {"request_sha256": "0" * 64, "request_bytes": 3},
            "plan_step": {"step": "checks", "round": 1, "ok": True, "answer": {"argv": ["SECRET_ARGV"]}},
            "plan_finished": {"dropped": [{"id": "c3"}]},
            "plan_failed": {"step": "design", "kind": "exit", "reason": "계획을 세우지 못함"},
            "review": {"ok": True, "findings": [{"verified": True, "quote": "SECRET_QUOTE"}, {"verified": False}]},
            "review_veto": {"iteration": 2, "findings": [{"criterion": "c1", "problem": "SECRET_PROBLEM"}]},
            "worker_unavailable": {"iteration": 2, "step": 0, "model": "m/x", "reason": "429 rate limit",
                                   "attempt": 2, "wait_seconds": 600, "stderr": "SECRET_ERR"},
            "plan_unavailable": {"step": "review", "round": 1, "attempt": 1, "reason": "quota",
                                 "wait_seconds": 300},
            # Stage F record types (SPEC-F sections 2-4).
            "canary": {"iteration": 2, "results": {"c1": {"held": True, "exit_code": 1}}, "changed_files": 1},
            "mutation": {"iteration": 2, "mutants": 4, "killed": 1, "skipped": 1, "details": [
                {"file": "SECRET_FILE.py", "line": 3, "operator": "compare_flip", "killed": False}]},
            "checks_strengthened": {"iteration": 2, "ok": True, "replaced": ["c1"], "kept": []},
            "notice": {"kind": "append_only", "text": "원장을 추가 전용(chattr +a)으로 잠그지 못함", "exit_code": 1,
                       "detail": "SECRET_ERR"},
            # Stage H record types (SPEC-H sections 1-3).
            "hygiene": {"iteration": 0, "files": ["json.py"]},
            "plan_filter": {"step": "critique", "round": 0, "results": {"c1": "SECRET_PROBLEM"}, "checked": 1,
                            "rejected": 1},
            "checks_update": {"iteration": 2, "criterion": "c1", "old_sha256": "0" * 64, "new_sha256": "1" * 64,
                              "reason": "SECRET_REASON"},
            "checks_update_rejected": {"iteration": 2, "criteria": ["c1"], "violations": [
                {"criterion": "c1", "rule": "mutant_survives", "file": "SECRET_FILE.py", "line": 3}]},
        }
        from agentic.ledger import RECORD_TYPES
        self.assertEqual(set(samples), set(RECORD_TYPES))
        for kind, data in samples.items():
            with self.subTest(kind=kind):
                text = admin.event_text({"type": kind, "data": data})
                self.assertNotIn("SECRET", text)
                self.assertNotIn("기록 ", text.split(" — ")[0])  # a real summary, not the fallback
                self.assertNotIn("\n", text)
        self.assertEqual(admin.event_text({"type": "check_result", "data": samples["check_result"]}),
                         "🧪 점검 2회차 — c1: 실패")
        self.assertEqual(admin.event_text({"type": "worker_unavailable", "data": samples["worker_unavailable"]}),
                         "⏸️ 모델을 쓸 수 없음 — m/x, 이유: 429 rate limit, 2번째 시도, 10분 대기")
        self.assertEqual(admin.event_text({"type": "plan_unavailable", "data": samples["plan_unavailable"]}),
                         "⏸️ 계획용 모델을 쓸 수 없음 — review, 이유: quota, 5분 대기")
        self.assertEqual(admin.event_text({"type": "gave_up", "data": {"weird": 1}}), "🏳️ 포기 — 조건 ")

    def test_status_with_real_checks_hides_them(self):
        self.play(fix("c1"), fix("c2"))
        self.run_mission()
        text = admin.mission_status(self.cfg, "demo") + "\n".join(admin.list_missions(self.cfg))
        for secret in ("SECRET_CONTENT_MARKER", "secret_check", sys.executable, "ALL TESTS PASSED"):
            self.assertNotIn(secret, text)
        self.assertTrue(os.path.exists(self.cfg.ledger_file("demo")))
