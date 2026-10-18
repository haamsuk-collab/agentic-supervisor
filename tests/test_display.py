"""Times shown to people are in config.DISPLAY_TIMEZONE (KST); the ledger keeps UTC."""

from datetime import datetime, timedelta, timezone
from unittest import mock

from agentic import admin, commands, config
from agentic.display import display_time
from agentic.ledger import parse_ts
from helpers import FakeCommands, SupervisorTestCase, TimedTestCase, fix, results

UTC = timezone.utc
LATE_EVENING_UTC = datetime(2026, 10, 18, 15, 0, 0, 250000, tzinfo=UTC)  # 2026-10-19 00:00:00 KST


class DisplayTimeTests(TimedTestCase):
    def test_utc_is_shown_as_kst(self):
        self.assertEqual(display_time(datetime(2026, 10, 18, 5, 53, 21, tzinfo=UTC)), "2026-10-18 14:53:21 KST")

    def test_the_date_moves_on_when_kst_has_passed_midnight(self):
        self.assertEqual(display_time(datetime(2026, 10, 18, 15, 0, 0, tzinfo=UTC)), "2026-10-19 00:00:00 KST")
        self.assertEqual(display_time(datetime(2026, 10, 18, 14, 59, 59, tzinfo=UTC)), "2026-10-18 23:59:59 KST")
        self.assertEqual(display_time(datetime(2026, 12, 31, 20, 30, 0, tzinfo=UTC)), "2027-01-01 05:30:00 KST")
        self.assertEqual(display_time(datetime(2028, 2, 28, 16, 0, 0, tzinfo=UTC)), "2028-02-29 01:00:00 KST")

    def test_a_ledger_timestamp_and_other_offsets_convert_to_the_same_moment(self):
        self.assertEqual(display_time(parse_ts("2026-10-18T05:53:21.987Z")), "2026-10-18 14:53:21 KST")
        new_york = timezone(timedelta(hours=-4))
        self.assertEqual(display_time(datetime(2026, 10, 18, 1, 53, 21, tzinfo=new_york)), "2026-10-18 14:53:21 KST")

    def test_a_naive_datetime_is_refused(self):
        with self.assertRaises(ValueError):
            display_time(datetime(2026, 10, 18, 5, 53, 21))

    def test_the_zone_comes_from_the_config(self):
        with mock.patch.object(config, "DISPLAY_TIMEZONE", "UTC"):
            self.assertEqual(display_time(datetime(2026, 10, 18, 5, 53, 21, tzinfo=UTC)), "2026-10-18 05:53:21 UTC")


class ShownTimesTests(SupervisorTestCase):
    """A mission run at 15:00 UTC on 2026-10-18: everything a person reads says 2026-10-19 00:00:00 KST."""

    SHOWN = "2026-10-19 00:00:00 KST"

    def setUp(self):
        super().setUp()
        self.fake = FakeCommands(lambda argv: commands.CommandResult(3, "inactive\n", ""))
        for patcher in (mock.patch("agentic.commands.run_command", self.fake),
                        mock.patch("os.geteuid", return_value=0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.play(fix("c1", "c2"))
        self.assertEqual(self.run_mission(clock=lambda: LATE_EVENING_UTC), "complete")

    def test_the_ledger_still_stores_utc(self):
        self.assertEqual({r["ts"] for r in self.records()}, {"2026-10-18T15:00:00.250Z"})

    def test_report_done_lines_show_the_last_check_in_kst(self):
        done = results(self.report_text(), "✅")
        self.assertEqual(done, [f"- [c1] Requirement c1 — 마지막 검사 {self.SHOWN}, 종료 코드 0",
                                f"- [c2] Requirement c2 — 마지막 검사 {self.SHOWN}, 종료 코드 0"])

    def test_list_and_status_lines_show_kst(self):
        line = f"demo · inactive · 마지막 기록: mission_finished ({self.SHOWN}) · 반복 1회"
        self.assertEqual(admin.list_missions(self.cfg), [line])
        status = admin.mission_status(self.cfg, "demo").splitlines()
        self.assertEqual(status[0], line)
        self.assertEqual(len(status), 1 + admin.EVENTS_SHOWN)
        self.assertTrue(all(row.startswith(f"- {self.SHOWN} ") for row in status[1:]), status)

    def test_the_mission_command_prints_kst(self):
        self.assertEqual(self.cli("mission", "list"), 0, self.err)
        self.assertIn(f"({self.SHOWN})", self.out)
        self.assertNotIn("Z)", self.out)
