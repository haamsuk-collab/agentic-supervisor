"""Test helpers for Telegram HTML cards and outbox items (SPEC-E.md section 4)."""

import json
import os
from html.parser import HTMLParser
from pathlib import Path

from agentic import cards
from agentic.config import DOC_SUFFIX

ALLOWED = {"b": [], "i": [], "code": [], "pre": [], "blockquote": [[("expandable", None)]]}


class _TagChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.problems.append(f"tag <{tag}> is not allowed")
        elif attrs and attrs not in ALLOWED[tag]:
            self.problems.append(f"<{tag}> has attributes {attrs}")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.problems.append(f"self-closing <{tag}/>")

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"unbalanced </{tag}> (open: {self.stack})")
        else:
            self.stack.pop()

    def handle_decl(self, decl):
        self.problems.append(f"declaration {decl!r}")

    def handle_comment(self, data):
        self.problems.append("comment")

    def unknown_decl(self, data):
        self.problems.append(f"unknown declaration {data!r}")


def html_problems(message: str) -> list[str]:
    """Why `message` is not a safe Telegram HTML message: tags outside the allowed set, attributes, or unbalanced
    tags. Raw `<` or `&` that start no tag are reported too, because the parser sees them as text only when
    escaped."""
    checker = _TagChecker()
    checker.feed(message)
    checker.close()
    problems = checker.problems + [f"unclosed <{tag}>" for tag in checker.stack]
    for i, char in enumerate(message):
        if char == "&" and not any(message.startswith(e, i) for e in ("&amp;", "&lt;", "&gt;")):
            problems.append(f"raw & at {i}")
    return problems


def card_text(messages: list[str]) -> str:
    """What the owner reads: the messages' plain text joined by newlines."""
    return "\n".join(cards.plain_text(m) for m in messages)


def outbox_items(outbox: Path) -> list[tuple[str, dict]]:
    """(name, payload) of every item in the outbox (documents are read through their item), sorted by name."""
    items = []
    for name in sorted(os.listdir(outbox)):
        if name.endswith(DOC_SUFFIX):
            continue
        items.append((name, json.loads((outbox / name).read_text(encoding="utf-8"))))
    return items
