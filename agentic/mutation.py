"""Mutation score (SPEC-F section 3): how strong the checks are.

Up to MAX_MUTANTS single-point mutants of the worker's changed Python files are made with `ast`; positions are
chosen with a random generator seeded from the mission id, so the same mission always gets the same mutants. A
mutant is killed when any passing criterion's check fails on a copy of the worker folder that holds that one
mutation. The record holds file, line, operator and the outcome only, never check content.
"""

import ast
import hashlib
import os
import random
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from agentic import checks, config
from agentic.config import Config
from agentic.mission import Mission
MIN_MUTANTS_FOR_STRENGTHENING = 3
WEAK_SCORE = 0.5
SOURCE_MAX_BYTES = 1024 * 1024  # larger files are not mutated
COMPARE_FLIP = "compare_flip"
AND_OR = "and_or"
TRUE_FALSE = "true_false"
RETURN_NONE = "return_none"
INT_PLUS_ONE = "int_plus_one"
_FLIPS = {ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}


@dataclass(frozen=True)
class Mutant:
    file: str  # relative posix path in the worker folder
    line: int
    operator: str
    index: int  # position among the file's mutation points (ast.walk order)


def _points(tree: ast.AST) -> list[tuple[ast.AST, str, int]]:
    """(node, operator, operand index) for every mutation point, in ast.walk order."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            found += [(node, COMPARE_FLIP, i) for i, op in enumerate(node.ops) if type(op) in _FLIPS]
        elif isinstance(node, ast.BoolOp):
            found.append((node, AND_OR, 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            found.append((node, TRUE_FALSE, 0))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            found.append((node, INT_PLUS_ONE, 0))
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            found.append((node, RETURN_NONE, 0))
    return found


def _mutate(node: ast.AST, operator: str, operand: int) -> None:
    if operator == COMPARE_FLIP:
        node.ops[operand] = _FLIPS[type(node.ops[operand])]()
    elif operator == AND_OR:
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif operator == TRUE_FALSE:
        node.value = not node.value
    elif operator == INT_PLUS_ONE:
        node.value += 1
    else:
        node.value = ast.Constant(None)


def mutated_source(source: str, index: int) -> str:
    """`source` with mutation point `index` changed (the code is re-printed by ast.unparse)."""
    tree = ast.parse(source)
    node, operator, operand = _points(tree)[index]
    _mutate(node, operator, operand)
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def _read(path: Path) -> str | None:
    """A regular file's text (never through a symlink), or None when it cannot be mutated."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = os.read(fd, SOURCE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > SOURCE_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def seed(mission_id: str) -> int:
    return int.from_bytes(hashlib.sha256(mission_id.encode("utf-8")).digest()[:8], "big")


def generate(workdir: str | Path, files: list[str], mission_id: str) -> list[Mutant]:
    """Up to MAX_MUTANTS mutants across `files`, chosen with a generator seeded from the mission id."""
    candidates = []
    for rel in sorted(files):
        source = _read(Path(workdir) / rel)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        candidates += [Mutant(rel, getattr(node, "lineno", 0), operator, index)
                       for index, (node, operator, _) in enumerate(_points(tree))]
    chosen = random.Random(seed(mission_id)).sample(candidates, min(config.MUTATION_MAX_MUTANTS, len(candidates)))
    return sorted(chosen, key=lambda m: (m.file, m.line, m.index))


def killed(cfg: Config, mission: Mission, workdir: str | Path, criteria: list[str], mutant: Mutant) -> bool:
    """True when a check of `criteria` fails on a copy of the worker folder holding this one mutation."""
    target = cfg.scratch_root(mission.id) / f"mutant-{secrets.token_hex(16)}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        checks.snapshot_tree(workdir, target)
        source = _read(target / mutant.file)
        fd = os.open(target / mutant.file, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(mutated_source(source, mutant.index))
        for cid in criteria:
            if not checks.run_check(cfg, mission.id, "mutant", cid, mission.criterion(cid).check, target).passed:
                return True
        return False
    finally:
        shutil.rmtree(target, ignore_errors=True)


def run(cfg: Config, mission: Mission, workdir: str | Path, criteria: list[str], files: list[str],
        clock: Callable[[], datetime], budget_seconds: float | None = None) -> dict:
    """The data of a `mutation` record (without the iteration). Mutants not started before the time budget ran
    out are recorded with `"skipped": true` and `"killed": null`."""
    if budget_seconds is None:
        budget_seconds = config.MUTATION_BUDGET_SECONDS
    mutants = generate(workdir, files, mission.id)
    deadline = clock() + timedelta(seconds=budget_seconds)
    details, killed_count, skipped = [], 0, 0
    for mutant in mutants:
        entry = {"file": mutant.file, "line": mutant.line, "operator": mutant.operator}
        if clock() >= deadline:
            details.append({**entry, "killed": None, "skipped": True})
            skipped += 1
            continue
        outcome = killed(cfg, mission, workdir, criteria, mutant)
        killed_count += outcome
        details.append({**entry, "killed": outcome, "skipped": False})
    return {"mutants": len(mutants), "killed": killed_count, "skipped": skipped, "details": details}


def ran(data: dict) -> int:
    """Mutants that really ran (the score is killed / ran)."""
    return data["mutants"] - data.get("skipped", 0)


def weak(data: dict) -> bool:
    """At least MIN_MUTANTS_FOR_STRENGTHENING mutants ran and fewer than half of them were killed."""
    n = ran(data)
    return n >= MIN_MUTANTS_FOR_STRENGTHENING and data["killed"] / n < WEAK_SCORE
