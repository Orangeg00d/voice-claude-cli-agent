"""Claude CLI subprocess executor."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from voice_claude_agent.config import DEFAULT_TIMEOUT_SECONDS, get_claude_workdir


@dataclass
class ClaudeRunResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    cwd: str = ""


def run_claude(
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    extra_args: list[str] | None = None,
    workdir: str | Path | None = None,
) -> ClaudeRunResult:
    command = ["claude", "-p", prompt]
    if extra_args:
        command = [command[0], *extra_args, *command[1:]]

    cwd_path = Path(workdir).expanduser() if workdir is not None else get_claude_workdir()
    cwd = str(cwd_path)

    start = time.monotonic()
    if not cwd_path.exists() or not cwd_path.is_dir():
        duration = time.monotonic() - start
        return ClaudeRunResult(
            command=command,
            exit_code=-3,
            stdout="",
            stderr=f"Claude workdir is invalid or not a directory: {cwd}",
            duration_seconds=round(duration, 3),
            timed_out=False,
            cwd=cwd,
        )

    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            cwd=cwd,
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
            cwd=cwd,
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
            cwd=cwd,
        )
    except OSError as e:
        duration = time.monotonic() - start
        return ClaudeRunResult(
            command=command,
            exit_code=-4,
            stdout="",
            stderr=f"Claude CLI failed to start in workdir {cwd}: {e}",
            duration_seconds=round(duration, 3),
            timed_out=False,
            cwd=cwd,
        )
