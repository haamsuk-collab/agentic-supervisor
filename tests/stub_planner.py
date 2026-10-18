"""Stub planner/reviewer for tests. Invoked like omp: <opts> --model M --thinking T --max-time Nm, prompt on stdin.

The step is read from the prompt's "Your step: <name>." line. Environment:
  AGENTIC_STUB_DIR      directory outside the worker folder holding planner_calls.jsonl (one line per call)
  AGENTIC_STUB_PLANNER  JSON object:
    "criteria":     criterion ids the design step invents (default ["c1", "c2"])
    "script":       {step: [directive, ...]}; the n-th call of a step plays directive[min(n-1, len-1)]
    "kill_at_step": the first call of this step SIGKILLs its parent (the supervisor), once
A directive may hold: "raw" (print this text only, then exit with "exit" or 0), "exit" (exit code), "stderr" (text
written to stderr first), "sleep" (seconds), "brief" (fields that replace the default brief), "texts" (design step:
{criterion id: text} replacing the default criterion text), "pass_empty" (checks step: criterion ids whose check
passes on an empty folder), "checks" (checks step: {criterion id: check} used instead of the default check), "veto"
(critique step: {criterion id: problem}), "findings" (review step).
No directive = a good answer.

The default checks run as `/usr/bin/python3.12 -I` (SPEC-H section 1) and make criterion cN pass when the work
folder holds cN.txt containing "ok" and ignore the stdout text; each check file carries a SECRET_CONTENT_MARKER and
the number of the call that wrote it.
"""

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

CHECK_PYTHON = "/usr/bin/python3.12"

CHECK_SCRIPT = """# SECRET_CONTENT_MARKER_{cid}
# written by checks call {call}
import sys
try:
    text = open("{cid}.txt").read().strip()
except OSError:
    sys.exit(1)
sys.exit(0 if text == "ok" else 1)
"""


def ids_after(prompt: str, label: str) -> list[str]:
    found = re.search(rf"^{re.escape(label)}: (.+)$", prompt, re.MULTILINE)
    return [cid.strip() for cid in found.group(1).split(",")] if found else []


def answer(step: str, cfg: dict, directive: dict, prompt: str, call: int) -> dict:
    if step == "brief":
        return {"goal": "날씨 CLI 만들기", "deliverables": ["weather 명령"], "assumptions": ["온도는 섭씨로 표시한다"],
                "out_of_scope": [], "language": "ko", **directive.get("brief", {})}
    if step == "design":
        texts = directive.get("texts", {})
        return {"approach": "Write one text file per requirement.",
                "criteria": [{"id": cid, "text": texts.get(cid, f"요구 사항 {cid}")}
                             for cid in cfg.get("criteria", ["c1", "c2"])]}
    if step == "checks":
        checks = {}
        for cid in ids_after(prompt, "Write checks for exactly these criterion ids"):
            name = f"_agentic_check_{cid}.py"
            if cid in directive.get("checks", {}):
                checks[cid] = directive["checks"][cid]
            elif cid in directive.get("pass_empty", []):
                checks[cid] = {"argv": [CHECK_PYTHON, "-I", "-c", "assert True"], "timeout": 30}
            else:
                checks[cid] = {"argv": [CHECK_PYTHON, "-I", name], "timeout": 30,
                               "files": {name: CHECK_SCRIPT.format(cid=cid, call=call)}}
        return {"checks": checks}
    if step == "critique":
        veto = directive.get("veto", {})
        verdicts = []
        for cid in ids_after(prompt, "Review exactly these criterion ids"):
            verdicts.append({"criterion": cid, "ok": False, "problem": veto[cid]} if cid in veto
                            else {"criterion": cid, "ok": True})
        return {"verdicts": verdicts}
    if step == "review":
        return {"findings": directive.get("findings", [])}
    raise SystemExit(f"stub planner: unknown step {step!r}")


def main() -> None:
    argv = sys.argv[1:]
    opts, i = {}, 0
    while i < len(argv) and argv[i].startswith("--"):
        opts[argv[i][2:]] = argv[i + 1]
        i += 2
    if i != len(argv):
        raise SystemExit(f"stub planner: unexpected positional arguments {argv[i:]!r} (the prompt comes on stdin)")
    prompt = sys.stdin.read()
    cfg = json.loads(os.environ.get("AGENTIC_STUB_PLANNER", "{}"))
    stub_dir = Path(os.environ["AGENTIC_STUB_DIR"])
    calls = stub_dir / "planner_calls.jsonl"
    step = re.search(r"^Your step: (\w+)\.$", prompt, re.MULTILINE).group(1)
    previous = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    call = sum(1 for c in previous if c["step"] == step) + 1

    marker = stub_dir / "planner_killed"
    if cfg.get("kill_at_step") == step and not marker.exists():
        marker.touch()
        os.kill(os.getppid(), signal.SIGKILL)
        sys.exit(1)

    with calls.open("a") as handle:
        handle.write(json.dumps({
            "step": step, "call": call, "model": opts["model"], "thinking": opts["thinking"],
            "max_time": opts["max-time"], "prompt": prompt, "argv": sys.argv,
            "stdin_devnull": os.path.samestat(os.fstat(0), os.stat("/dev/null")),
        }) + "\n")

    script = cfg.get("script", {}).get(step, [{}])
    directive = script[min(call - 1, len(script) - 1)]
    time.sleep(directive.get("sleep", 0))
    sys.stderr.write(directive.get("stderr", ""))
    if "raw" in directive:
        print(directive["raw"])
        sys.exit(directive.get("exit", 0))
    if "exit" in directive:
        sys.exit(directive["exit"])
    body = json.dumps(answer(step, cfg, directive, prompt, call), ensure_ascii=False, indent=2)
    # An earlier fence the supervisor must ignore: only the last ```json block counts.
    print('Thinking about it.\n```json\n{"decoy": true}\n```\nFinal answer:\n```json\n' + body + "\n```")


main()
