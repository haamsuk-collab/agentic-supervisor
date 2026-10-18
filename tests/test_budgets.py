"""SPEC-H section 5: context byte and line budgets on guidance/user files."""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, TimedTestCase

USER_GUIDANCE = ROOT / "guidance" / "user"

RULES_MD_MAX_BYTES = 4_500
WORKSPACE_STATE_MAX_BYTES = 200
RULE_BODY_MAX_BYTES = 1_500
SKILL_DESCRIPTION_MAX_BYTES = 600
AGENTS_MD_MAX_BYTES = 3_000
SKILL_MD_MAX_LINES = 280

# Representative worst-case inputs for guidance/user/extensions/workspace-state.ts:
# repo path under /home/agent/work, a 36-char feature branch name, and 3-digit counts.
WORST_CASE_CWD = "/home/agent/work/agentic-supervisor"
WORST_CASE_BRANCH = "feature/stage-h-context-byte-budgets"
WORST_CASE_CHANGED = 999
WORST_CASE_MISSIONS = 999

FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
GIT_TEMPLATE_RE = re.compile(r"git\s*=\s*`([^`]+)`")
CONTENT_TEMPLATE_RE = re.compile(r"const\s+content\s*=\s*`([^`]+)`")


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def split_front_matter(text: str, source: Path) -> tuple[str, str]:
    match = FRONT_MATTER_RE.match(text)
    if match is None:
        raise ValueError(f"{source}: missing YAML front matter")
    return match.group(1), text[match.end():]


def extract_rule_body(path: Path) -> str:
    _, body = split_front_matter(path.read_text(encoding="utf-8"), path)
    return body


def extract_skill_description(path: Path) -> str:
    front_matter, _ = split_front_matter(path.read_text(encoding="utf-8"), path)
    parts: list[str] = []
    in_desc = False
    for line in front_matter.splitlines():
        if not in_desc:
            if line.startswith("description:"):
                head = line.split(":", 1)[1].strip()
                if head not in ("", ">", "|", ">-", "|-"):
                    parts.append(head.strip("\"'"))
                in_desc = True
        else:
            if line.startswith((" ", "\t")):
                parts.append(line.strip())
            elif not line.strip():
                continue
            else:
                break
    if not in_desc or not parts:
        raise ValueError(f"{path}: missing description in front matter")
    return " ".join(" ".join(parts).split())


def render_workspace_state_line(
    ts_path: Path,
    cwd: str = WORST_CASE_CWD,
    branch: str = WORST_CASE_BRANCH,
    changed: int = WORST_CASE_CHANGED,
    missions: int = WORST_CASE_MISSIONS,
) -> str:
    source = ts_path.read_text(encoding="utf-8")
    git_match = GIT_TEMPLATE_RE.search(source)
    content_match = CONTENT_TEMPLATE_RE.search(source)
    if git_match is None or content_match is None:
        raise ValueError(f"{ts_path}: missing workspace-state template strings")
    git_part = (
        git_match.group(1)
        .replace("${branch}", str(branch))
        .replace("${changed}", str(changed))
    )
    return (
        content_match.group(1)
        .replace("${cwd}", str(cwd))
        .replace("${git}", git_part)
        .replace("${missions}", str(missions))
    )


def _rel(path: Path, user_dir: Path) -> str:
    try:
        return str(Path("guidance/user") / path.relative_to(user_dir))
    except ValueError:
        return str(path)


def _byte_violation(label: str, actual: int, limit: int) -> str | None:
    if actual > limit:
        return f"{label}: {actual} bytes > {limit} bytes (+{actual - limit} bytes over)"
    return None


def _line_violation(label: str, actual: int, limit: int) -> str | None:
    if actual > limit:
        return f"{label}: {actual} lines > {limit} lines (+{actual - limit} lines over)"
    return None


def check_budgets(user_dir: Path = USER_GUIDANCE) -> list[str]:
    violations: list[str] = []

    rules_md = user_dir / "RULES.md"
    v = _byte_violation(_rel(rules_md, user_dir), len(rules_md.read_bytes()), RULES_MD_MAX_BYTES)
    if v:
        violations.append(v)

    agents_md = user_dir / "AGENTS.md"
    v = _byte_violation(_rel(agents_md, user_dir), len(agents_md.read_bytes()), AGENTS_MD_MAX_BYTES)
    if v:
        violations.append(v)

    ws_ts = user_dir / "extensions" / "workspace-state.ts"
    ws_line = render_workspace_state_line(ws_ts)
    v = _byte_violation(_rel(ws_ts, user_dir), utf8_len(ws_line), WORKSPACE_STATE_MAX_BYTES)
    if v:
        violations.append(v)

    rule_files = sorted((user_dir / "rules").glob("*.md"))
    if not rule_files:
        violations.append(f"{_rel(user_dir / 'rules', user_dir)}: no rule files found")
    for rule_path in rule_files:
        body_bytes = utf8_len(extract_rule_body(rule_path))
        v = _byte_violation(f"{_rel(rule_path, user_dir)} body", body_bytes, RULE_BODY_MAX_BYTES)
        if v:
            violations.append(v)

    skill_files = sorted((user_dir / "skills").glob("*/SKILL.md"))
    if not skill_files:
        violations.append(f"{_rel(user_dir / 'skills', user_dir)}: no SKILL.md files found")
    for skill_path in skill_files:
        desc_bytes = utf8_len(extract_skill_description(skill_path))
        v = _byte_violation(
            f"{_rel(skill_path, user_dir)} description",
            desc_bytes,
            SKILL_DESCRIPTION_MAX_BYTES,
        )
        if v:
            violations.append(v)
        line_count = len(skill_path.read_text(encoding="utf-8").splitlines())
        v = _line_violation(_rel(skill_path, user_dir), line_count, SKILL_MD_MAX_LINES)
        if v:
            violations.append(v)

    return violations


class GuidanceBudgetTests(TimedTestCase):
    def setUp(self):
        super().setUp()
        if not USER_GUIDANCE.exists():
            self.skipTest("guidance/user is copied in by the operator (SPEC-G section 7)")

    def test_rules_md_within_4500_bytes(self):
        path = USER_GUIDANCE / "RULES.md"
        v = _byte_violation(_rel(path, USER_GUIDANCE), len(path.read_bytes()), RULES_MD_MAX_BYTES)
        self.assertIsNone(v, v)

    def test_workspace_state_line_within_200_bytes(self):
        path = USER_GUIDANCE / "extensions" / "workspace-state.ts"
        line = render_workspace_state_line(path)
        self.assertIn(WORST_CASE_CWD, line)
        self.assertIn(WORST_CASE_BRANCH, line)
        v = _byte_violation(_rel(path, USER_GUIDANCE), utf8_len(line), WORKSPACE_STATE_MAX_BYTES)
        self.assertIsNone(v, v)

    def test_ttsr_rule_bodies_within_1500_bytes(self):
        rule_files = sorted((USER_GUIDANCE / "rules").glob("*.md"))
        self.assertGreater(len(rule_files), 0)
        failures = []
        for path in rule_files:
            v = _byte_violation(
                f"{_rel(path, USER_GUIDANCE)} body",
                utf8_len(extract_rule_body(path)),
                RULE_BODY_MAX_BYTES,
            )
            if v:
                failures.append(v)
        self.assertEqual(failures, [], "\n".join(failures))

    def test_skill_descriptions_within_600_bytes(self):
        skill_files = sorted((USER_GUIDANCE / "skills").glob("*/SKILL.md"))
        self.assertGreater(len(skill_files), 0)
        failures = []
        for path in skill_files:
            v = _byte_violation(
                f"{_rel(path, USER_GUIDANCE)} description",
                utf8_len(extract_skill_description(path)),
                SKILL_DESCRIPTION_MAX_BYTES,
            )
            if v:
                failures.append(v)
        self.assertEqual(failures, [], "\n".join(failures))

    def test_agents_md_within_3000_bytes(self):
        path = USER_GUIDANCE / "AGENTS.md"
        v = _byte_violation(_rel(path, USER_GUIDANCE), len(path.read_bytes()), AGENTS_MD_MAX_BYTES)
        self.assertIsNone(v, v)

    def test_skill_md_files_within_280_lines(self):
        skill_files = sorted((USER_GUIDANCE / "skills").glob("*/SKILL.md"))
        self.assertGreater(len(skill_files), 0)
        failures = []
        for path in skill_files:
            lines = len(path.read_text(encoding="utf-8").splitlines())
            v = _line_violation(_rel(path, USER_GUIDANCE), lines, SKILL_MD_MAX_LINES)
            if v:
                failures.append(v)
        self.assertEqual(failures, [], "\n".join(failures))


class BudgetCheckMechanismTests(TimedTestCase):
    def test_skips_when_guidance_user_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nonexistent"
            with mock.patch.object(sys.modules[__name__], "USER_GUIDANCE", missing):
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(GuidanceBudgetTests)
                result = unittest.TestResult()
                suite.run(result)
                self.assertEqual(len(result.skipped), suite.countTestCases())
                self.assertEqual(result.failures, [])
                self.assertEqual(result.errors, [])

    def test_reports_every_exceeded_budget_with_file_and_excess(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user"
            (user / "rules").mkdir(parents=True)
            (user / "skills" / "demo").mkdir(parents=True)
            (user / "extensions").mkdir(parents=True)

            # Exceed each budget by a known amount in UTF-8 bytes or lines.
            (user / "RULES.md").write_bytes("가".encode("utf-8") * 1505)  # 4515 B (+15)
            (user / "AGENTS.md").write_bytes(b"a" * 3012)  # 3012 B (+12)
            (user / "extensions" / "workspace-state.ts").write_text(
                'git = `git ${branch}, ${changed} changed file(s) ' + ("x" * 80) + '`;\n'
                'const content = `[workspace] cwd ${cwd} · ${git} · active missions ${missions}`;\n',
                encoding="utf-8",
            )
            (user / "rules" / "sample.md").write_text(
                "---\ndescription: d\ncondition: []\n---\n" + ("b" * 1520),
                encoding="utf-8",
            )
            folded_desc = "  " + ("word " * 125)  # normalized 624 B (+24)
            skill_body = "\n".join(f"line {i}" for i in range(285))
            (user / "skills" / "demo" / "SKILL.md").write_text(
                f"---\nname: demo\ndescription: >\n{folded_desc}\n---\n{skill_body}\n",
                encoding="utf-8",
            )

            violations = check_budgets(user)
            self.assertEqual(len(violations), 6, violations)
            joined = "\n".join(violations)
            self.assertIn("guidance/user/RULES.md: 4515 bytes > 4500 bytes (+15 bytes over)", joined)
            self.assertIn("guidance/user/AGENTS.md: 3012 bytes > 3000 bytes (+12 bytes over)", joined)
            self.assertIn("guidance/user/extensions/workspace-state.ts:", joined)
            self.assertIn("guidance/user/rules/sample.md body: 1520 bytes > 1500 bytes (+20 bytes over)", joined)
            self.assertIn(
                "guidance/user/skills/demo/SKILL.md description: 624 bytes > 600 bytes (+24 bytes over)",
                joined,
            )
            self.assertIn("guidance/user/skills/demo/SKILL.md: 290 lines > 280 lines (+10 lines over)", joined)


if __name__ == "__main__":
    unittest.main()
