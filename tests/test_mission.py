import copy
import json
import unittest

from agentic import config
from agentic.mission import DEFAULT_LIMITS, MissionError, parse_mission
from helpers import TimedTestCase

GOOD = {
    "id": "weather-cli",
    "goal": "g",
    "criteria": [{"id": "c1", "text": "t", "check": {"argv": ["true"], "files": {"checks/a.py": "x"}}}],
}


def variant(**changes):
    mission = copy.deepcopy(GOOD)
    for dotted, value in changes.items():
        target = mission
        *path, last = dotted.split("__")
        for key in path:
            target = target[int(key)] if key.isdigit() else target[key]
        target[int(last) if last.isdigit() else last] = value
    return mission


class MissionTests(TimedTestCase):
    def test_defaults_apply_when_limits_and_ladder_are_omitted(self):
        mission = parse_mission(GOOD)
        self.assertEqual(mission.limits, DEFAULT_LIMITS)
        self.assertEqual([dict(s) for s in mission.ladder], config.WORKER_LADDER)
        self.assertEqual(mission.criteria[0].check.timeout, 120)

    def test_partial_limits_keep_the_other_defaults(self):
        mission = parse_mission(variant(limits={"escalate_after": 5}))
        self.assertEqual(mission.limits["escalate_after"], 5)
        self.assertEqual(mission.limits["give_up_after"], 2)

    def test_unknown_keys_are_rejected_everywhere(self):
        for bad in (variant(extra=1), variant(criteria__0__extra=1), variant(criteria__0__check__extra=1),
                    variant(limits={"nope": 1}), variant(ladder=[{"model": "m", "nope": 1}])):
            with self.subTest(bad=json.dumps(bad)[:60]), self.assertRaises(MissionError):
                parse_mission(bad)

    def test_invalid_ids_are_rejected(self):
        for bad_id in ("", "A", "-x", "a_b", "a" * 42, "../x", "a b"):
            with self.subTest(id=bad_id), self.assertRaises(MissionError):
                parse_mission(variant(id=bad_id))
        with self.assertRaises(MissionError):
            parse_mission(variant(criteria__0__id="Bad"))
        self.assertEqual(parse_mission(variant(id="a" * 41)).id, "a" * 41)

    def test_duplicate_criterion_ids_are_rejected(self):
        crit = GOOD["criteria"][0]
        with self.assertRaises(MissionError):
            parse_mission(variant(criteria=[crit, crit]))

    def test_argv_must_be_a_non_empty_list_of_strings(self):
        for bad in ([], "pytest", ["ok", 1], None):
            with self.subTest(argv=bad), self.assertRaises(MissionError):
                parse_mission(variant(criteria__0__check__argv=bad))

    def test_timeout_range(self):
        for bad in (0, 601, -1, 1.5, True, "5"):
            with self.subTest(timeout=bad), self.assertRaises(MissionError):
                parse_mission(variant(criteria__0__check__timeout=bad))
        for ok in (1, 600):
            self.assertEqual(parse_mission(variant(criteria__0__check__timeout=ok)).criteria[0].check.timeout, ok)

    def test_check_file_paths_must_stay_inside_the_folder(self):
        for bad in ("/etc/passwd", "../x", "a/../../x", "", "."):
            with self.subTest(path=bad), self.assertRaises(MissionError):
                parse_mission(variant(criteria__0__check__files={bad: "x"}))

    def test_public_view_has_no_check_data(self):
        view = json.dumps(parse_mission(GOOD).public_view())
        for secret in ("true", "checks/a.py", "argv", "files"):
            self.assertNotIn(f'"{secret}"', view)
        self.assertNotIn("checks/a.py", view)


class PlannedMissionFieldTests(TimedTestCase):
    def test_request_approach_and_assumptions_are_optional_public_fields(self):
        mission = parse_mission(variant(request="요청", approach="a", assumptions=["x", "y"]))
        self.assertEqual((mission.request, mission.approach, mission.assumptions), ("요청", "a", ("x", "y")))
        view = mission.public_view()
        self.assertEqual((view["request"], view["approach"], view["assumptions"]), ("요청", "a", ["x", "y"]))
        plain = parse_mission(GOOD)
        self.assertEqual((plain.request, plain.approach, plain.assumptions), (None, None, None))
        for key in ("request", "approach", "assumptions"):
            self.assertNotIn(key, plain.public_view())

    def test_size_and_type_limits(self):
        for bad in (variant(request=""), variant(request="a" * (20 * 1024 + 1)), variant(request=5),
                    variant(approach="a" * 3001), variant(approach=["a"]),
                    variant(assumptions="one"), variant(assumptions=["a"] * 31),
                    variant(assumptions=["a" * 501]), variant(assumptions=[1])):
            with self.subTest(bad=str(bad)[-40:]), self.assertRaises(MissionError):
                parse_mission(bad)
        parse_mission(variant(request="a" * (20 * 1024), approach="a" * 3000, assumptions=["a" * 500] * 30))
        parse_mission(variant(assumptions=[]))

    def test_a_nul_character_in_argv_is_rejected(self):
        with self.assertRaises(MissionError):
            parse_mission(variant(criteria__0__check__argv=["a\0b"]))


if __name__ == "__main__":
    unittest.main()
