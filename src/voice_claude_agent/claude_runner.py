"""Claude CLI subprocess executor."""

import subprocess
import time
from dataclasses import dataclass, field

from voice_claude_agent.config import DEFAULT_TIMEOUT_SECONDS


@dataclass
class ClaudeRunResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


def run_claude(
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    extra_args: list[str] | None = None,
) -> ClaudeRunResult:
    command = ["claude", "-p", prompt]
    if extra_args:
        command = [command[0], *extra_args, *command[1:]]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - start
        return ClaudeRunResult(
            command=command,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_seconds=round(duration, 3),
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return ClaudeRunResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=f"Timeout after {timeout}s",
            duration_seconds=round(duration, 3),
            timed_out=True,
        )
    except FileNotFoundError:
        duration = time.monotonic() - start
        return ClaudeRunResult(
            command=command,
            exit_code=-2,
            stdout="",
            stderr="Claude CLI not found. Is it installed?",
            duration_seconds=round(duration, 3),
            timed_out=False,
        )
