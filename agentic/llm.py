"""One planner/reviewer model call as the `agplan` account, and reading a JSON object from its answer."""

import json
import time
from typing import Any, Callable

from agentic import config
from agentic.checks import run_process
from agentic.config import Config
from agentic.outage import matching_line
from agentic.worker import systemd_escape, unit_prefix, user_uid

# The prompt goes to omp on stdin (`omp -p` without a positional prompt reads it). The cap is a prompt budget,
# not an OS limit; the review fits its file text into it.
PROMPT_MAX_BYTES = 120_000
FENCE = "```json"
OUTPUT_SNIPPET = 2000  # characters of a rejected answer kept in the ledger


class AnswerError(ValueError):
    """The model's answer was not usable. `kind`: no_json | bad_json | schema."""

    def __init__(self, message: str, kind: str = "schema"):
        super().__init__(message)
        self.kind = kind


INFRA_KINDS = frozenset({"exit", "timeout", "start"})  # failures that say nothing about the answer itself


class StepFailed(Exception):
    """Every model failed. `kind` classifies the last failure, `error` is its full text (may be check-related).

    `outage`: when every attempt failed with an INFRA_KINDS kind and an attempt's output matches an outage
    pattern (outage.PATTERNS), the matching line; else None.
    """

    def __init__(self, step: str, kind: str, error: str, outage: str | None = None):
        super().__init__(f"{step}: {error}")
        self.step, self.kind, self.error, self.outage = step, kind, error, outage


def split_model(spec: str) -> tuple[str, str]:
    """'provider/name:thinking' -> (model, thinking)."""
    model, _, thinking = spec.rpartition(":")
    return model, thinking


def build_command(cfg: Config, model: str, thinking: str) -> list[str]:
    """The argv of one call. It never holds the prompt: `ask` sends that on stdin."""
    tail = ["--model", model, "--thinking", thinking, "--max-time", f"{config.PLANNER_MAX_MINUTES}m"]
    if cfg.planner_cmd is not None:  # override (tests): used verbatim, no systemd, no escaping
        return [*cfg.planner_cmd, *tail]
    uid = user_uid(config.PLANNER_USER, "planner")
    prefix = unit_prefix(config.PLANNER_USER, config.PLANNER_HOME, config.PLANNER_RUNTIME_SECONDS, uid,
                         config.PLANNER_OMP_ARGV)
    return [*prefix, *map(systemd_escape, tail)]


def extract_json(text: str) -> dict:
    """The JSON object in the last ```json fenced block; else the whole output as JSON."""
    decoder = json.JSONDecoder()
    blocks: list[Any] = []  # decoded value, or an AnswerError for a block that does not parse
    pos = 0
    while (found := text.find(FENCE, pos)) >= 0:
        start = found + len(FENCE)
        pos = start
        if start < len(text) and not text[start].isspace():
            continue  # ```json5, ```jsonc, ...: not our fence
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            value, pos = decoder.raw_decode(text, start)  # decodes embedded ``` inside strings correctly
        except json.JSONDecodeError as exc:
            blocks.append(AnswerError(f"the last ```json block is not valid JSON ({exc.msg} at char {exc.pos})",
                                      "bad_json"))
            close = text.find("```", start)
            pos = len(text) if close < 0 else close + 3
        else:
            blocks.append(value)
    if blocks:
        last = blocks[-1]
    else:
        try:
            last = json.loads(text)
        except json.JSONDecodeError:
            raise AnswerError("there is no ```json fenced block and the output is not JSON", "no_json") from None
    if isinstance(last, AnswerError):
        raise last
    if not isinstance(last, dict):
        raise AnswerError("the answer must be one JSON object", "bad_json")
    return last


def _prompt_problem(prompt: str) -> str | None:
    if "\0" in prompt:
        return "the prompt contains a NUL character"
    if len(prompt.encode("utf-8")) > PROMPT_MAX_BYTES:
        return f"the prompt is larger than {PROMPT_MAX_BYTES} bytes"
    return None


def ask(cfg: Config, step: str, models: list[str], prompt: str, validate: Callable[[dict], Any],
        on_attempt: Callable[[dict], None]) -> Any:
    """Ask each model in order (each at most twice) until one answer passes `validate`.

    `validate` returns the normalised answer or raises AnswerError. Every attempt is reported to
    `on_attempt` as a plain dict. Raises StepFailed when every model failed.
    """
    attempt = 0
    kind, error = "exit", "no model was asked"
    infra_only, outage_line = True, None
    for spec in models:
        model, thinking = split_model(spec)
        note = ""
        for _ in range(2):
            attempt += 1
            text = prompt + note
            record = {"attempt": attempt, "model": model, "thinking": thinking, "exit_code": None,
                      "timed_out": False, "duration_seconds": 0.0, "ok": False, "error": None,
                      "answer": None, "stderr": "", "output": ""}
            problem = _prompt_problem(text)
            if problem:  # the same prompt goes to every model: no point in trying again
                record["error"] = problem
                on_attempt(record)
                raise StepFailed(step, "prompt", problem)
            started = time.monotonic()
            # The unit enforces RuntimeMaxSec itself; the client-side limit is only a backstop.
            outer = config.PLANNER_RUNTIME_SECONDS + (10 if cfg.planner_cmd is None else 0)
            proc = run_process(build_command(cfg, model, thinking), "/", outer, config.PLANNER_OUTPUT_BYTES,
                               stdin=text.encode("utf-8"))
            record.update(exit_code=proc.exit_code, timed_out=proc.timed_out, stderr=proc.stderr,
                          duration_seconds=round(time.monotonic() - started, 3))
            if proc.timed_out:
                kind, error = "timeout", "the call timed out"
            elif proc.exit_code is None:
                kind, error = "start", proc.stderr.strip()[:300] or "the call could not be started"
            elif proc.exit_code != 0:
                kind, error = "exit", f"the call exited with code {proc.exit_code}"
            else:
                try:
                    record["answer"] = validate(extract_json(proc.stdout))
                    record["ok"] = True
                except AnswerError as exc:
                    kind, error = exc.kind, str(exc)
            if record["ok"]:
                on_attempt(record)
                return record["answer"]
            infra_only = infra_only and kind in INFRA_KINDS
            outage_line = outage_line or matching_line(proc.stderr)
            record["error"] = error
            record["output"] = proc.stdout[-OUTPUT_SNIPPET:]
            on_attempt(record)
            note = (f"\n\nYour previous answer was rejected: {error.rstrip('.')}. "
                    "Answer again with one JSON object in a ```json fence.")
    raise StepFailed(step, kind, error, outage_line if infra_only else None)
