"""The checks ratchet (SPEC-H section 2): a mission's checks may never get weaker.

A change to the checks is accepted only when, checked mechanically:
1. every criterion still has a check;
2. every file of an old check is still in the new check, unchanged, unless the planner named the bug in the old
   check (`reasons`) and the critique, which was shown that reason, accepted the new check;
3. the new check still fails on an empty folder;
4. the new check fails on every mutant that the old check killed.
Violations name the criterion and the rule, never check content.
"""

import hashlib
import json
from pathlib import Path

from agentic import mutation, planner
from agentic.config import Config
from agentic.mission import Check, Mission, check_dict
from agentic.mutation import Mutant

MISSING = "criterion_without_check"
FILES = "old_files_changed"
EMPTY = "passes_empty_folder"
MUTANT = "mutant_survives"
DEFAULT_REASON = "stronger check against the surviving mutants; every file of the old check kept"


def check_sha256(check: Check) -> str:
    """sha256 of the canonical JSON of a check (argv, timeout, network, files)."""
    text = json.dumps(check_dict(check), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def changed(old: Mission, new: Mission) -> list[str]:
    """Criteria of `old` whose check differs in `new` (mission order)."""
    present = {c.id for c in new.criteria}
    return [c.id for c in old.criteria
            if c.id in present and check_sha256(c.check) != check_sha256(new.criterion(c.id).check)]


def violations(cfg: Config, old: Mission, new: Mission, reasons: dict[str, str], workdir: str | Path,
               killed_mutants: list[Mutant]) -> list[dict]:
    """Every way in which `new` is weaker than `old`; empty when the change may be accepted. `killed_mutants`: the
    mutants of the mission's mutation score that the old checks killed."""
    present = {c.id for c in new.criteria}
    found = [{"criterion": c.id, "rule": MISSING} for c in old.criteria if c.id not in present]
    ids = changed(old, new)
    for cid in ids:
        before, after = old.criterion(cid).check.files, new.criterion(cid).check.files
        if any(after.get(rel) != content for rel, content in before.items()) and not reasons.get(cid):
            found.append({"criterion": cid, "rule": FILES})
    empty = planner.empty_folder_problems(cfg, new.id, {cid: check_dict(new.criterion(cid).check) for cid in ids},
                                          ids)
    found += [{"criterion": cid, "rule": EMPTY} for cid in ids if empty[cid]]
    for mutant in killed_mutants:
        where = {"file": mutant.file, "line": mutant.line, "operator": mutant.operator}
        for cid in ids:
            if (mutation.killed(cfg, old, workdir, [cid], mutant)
                    and not mutation.killed(cfg, new, workdir, [cid], mutant)):
                found.append({"criterion": cid, "rule": MUTANT, **where})
    return found
