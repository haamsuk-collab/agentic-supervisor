"""The one place that runs system commands (systemctl, runuser, chattr, tar). Tests replace `run_command` with a
fake; callers must look it up as `commands.run_command` at call time so the replacement takes effect."""

import subprocess
from dataclasses import dataclass

TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(argv: list[str], stdin_path: str | None = None) -> CommandResult:
    """Run argv (no shell) and return its exit code and output. stdin is /dev/null, or the file `stdin_path`."""
    try:
        if stdin_path is None:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=TIMEOUT_SECONDS)
        else:
            with open(stdin_path, "rb") as source:
                proc = subprocess.run(argv, stdin=source, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]} timed out after {TIMEOUT_SECONDS}s")
    except OSError as exc:
        return CommandResult(127, "", f"cannot start {argv[0]!r}: {exc}")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
