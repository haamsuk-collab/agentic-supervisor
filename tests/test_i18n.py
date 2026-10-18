"""SPEC-G section 2: both catalogs have the same keys, and no user-facing text is hard-coded outside agentic/i18n/."""

import ast
import json
import re
import string
import unittest
from unittest import mock

from helpers import ROOT, TimedTestCase

from agentic import config
from agentic.i18n import CATALOGS, en, ko, t

HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")
LETTERS = re.compile(r"[A-Za-z\u3130-\u318f\uac00-\ud7a3]")
PACKAGE = ROOT / "agentic"
I18N = PACKAGE / "i18n"
ENGLISH_RULES = ROOT / "guidance" / "user" / "rules" / "english-writing.md"
TEXT_KWARGS = ("help", "description", "epilog", "usage")


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def code_modules() -> list:
    return [p for p in sorted(PACKAGE.rglob("*.py")) if I18N not in p.parents]


def docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def literal_texts(node: ast.AST) -> list[str]:
    """Every str constant inside `node` (f-string parts included), except what a nested t(...) call holds."""
    out = []

    def visit(n):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t":
            return
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return out


class CatalogTests(TimedTestCase):
    def test_both_catalogs_have_the_same_keys_and_placeholders(self):
        self.assertEqual(set(CATALOGS), {"en", "ko"})
        self.assertEqual(sorted(en.MESSAGES), sorted(ko.MESSAGES))
        self.assertGreater(len(en.MESSAGES), 100)
        for key in en.MESSAGES:
            with self.subTest(key=key):
                self.assertIsInstance(en.MESSAGES[key], str)
                self.assertIsInstance(ko.MESSAGES[key], str)
                self.assertEqual(fields(en.MESSAGES[key]), fields(ko.MESSAGES[key]))

    def test_the_english_catalog_holds_no_korean(self):
        for key, value in en.MESSAGES.items():
            self.assertIsNone(HANGUL.search(value), key)

    def test_the_language_is_read_at_call_time(self):
        key = next(k for k, v in ko.MESSAGES.items() if not fields(v) and HANGUL.search(v))
        with mock.patch.object(config, "LANGUAGE", "en"):
            self.assertEqual(t(key), en.MESSAGES[key].format_map({}))
        with mock.patch.object(config, "LANGUAGE", "ko"):
            self.assertEqual(t(key), ko.MESSAGES[key].format_map({}))

    def test_every_key_the_code_asks_for_exists(self):
        asked = set()
        for path in code_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t"
                        and node.args and isinstance(node.args[0], ast.Constant)):
                    asked.add(node.args[0].value)
        self.assertGreater(len(asked), 50)
        self.assertEqual(sorted(asked - set(en.MESSAGES)), [])


class NoHardCodedTextTests(TimedTestCase):
    def test_no_korean_string_outside_the_catalogs(self):
        for path in code_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = docstring_ids(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    self.assertIsNone(HANGUL.search(node.value), f"{path.relative_to(ROOT)}:{node.lineno}")

    def test_printed_and_argparse_texts_come_from_the_catalogs(self):
        checked = 0
        for path in code_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                parts = []
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    parts = [a for a in node.args]
                parts += [k.value for k in node.keywords if k.arg in TEXT_KWARGS]
                for part in parts:
                    checked += 1
                    for text in literal_texts(part):
                        self.assertIsNone(LETTERS.search(text), f"{path.relative_to(ROOT)}:{node.lineno}: {text!r}")
        self.assertGreater(checked, 20)


class BannedPhraseTests(TimedTestCase):
    def test_no_catalog_text_holds_a_banned_phrase_in_either_language(self):
        self.assertGreater(len(ko.BANNED_PHRASES), 10)
        self.assertGreater(len(en.BANNED_PATTERNS), 3)
        patterns = [re.compile(p) for p in en.BANNED_PATTERNS]
        for key in en.MESSAGES:
            for phrase in ko.BANNED_PHRASES:
                self.assertNotIn(phrase, ko.MESSAGES[key], key)
            for pattern in patterns:
                self.assertIsNone(pattern.search(en.MESSAGES[key]), f"{key}: {pattern.pattern}")

    def test_the_english_patterns_catch_the_tells_they_name(self):
        patterns = [re.compile(p) for p in en.BANNED_PATTERNS]
        for tell in ("Great question! The run ended.", "I hope this helps", "Let's dive in."):
            self.assertTrue(any(p.search(tell) for p in patterns), tell)

    @unittest.skipUnless(ENGLISH_RULES.exists(), "guidance/ is copied in by the operator (SPEC-G section 7)")
    def test_the_english_patterns_are_the_guidance_list(self):
        header = ENGLISH_RULES.read_text(encoding="utf-8").split("---")[1]
        line = next(l for l in header.splitlines() if l.startswith("condition:"))
        self.assertEqual(list(en.BANNED_PATTERNS), json.loads(line.split(":", 1)[1]))


if __name__ == "__main__":
    unittest.main()
