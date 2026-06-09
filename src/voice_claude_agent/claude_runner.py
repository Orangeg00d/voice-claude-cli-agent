"""Claude CLI subprocess executor."""

import subprocess
import threading
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
    cancelled: bool = False
    cwd: str = ""


# F079: Global cancel event for the menu bar app.
_cancel_event = threading.Event()


def _get_cancel_event() -> threading.Event:
    """Return the global cancellation event. Cleared on first access per run."""
    return _cancel_event


def reset_cancel_event() -> None:
    """Clear the cancellation flag. Must be called before each Claude invocation."""
    _cancel_event.clear()


def request_cancel() -> None:
    """Signal the running Claude subprocess to be cancelled."""
    _cancel_event.set()


def is_cancelled() -> bool:
    """Check whether cancellation has been requested."""
    return _cancel_event.is_set()


def run_claude(
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    extra_args: list[str] | None = None,
    workdir: str | Path | None = None,
    cancel_event: threading.Event | None = None,
) -> ClaudeRunResult:
    command = ["claude", "-p", prompt]
    if extra_args:
        command = [command[0], *extra_args, *command[1:]]

    cwd_path = Path(workdir).expanduser() if workdir is not None else get_claude_workdir()
    cwd = str(cwd_path)

    if cancel_event is None:
        cancel_event = _cancel_event

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
        # F079: poll cancel event periodically; use Popen for cancellability
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            # Wait up to timeout in small chunks so we can check cancel signal
            poll_interval = 0.5
            elapsed = 0.0
            while elapsed < timeout:
                if cancel_event.is_set():
                    # Kill the subprocess and return
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    duration = time.monotonic() - start
                    return ClaudeRunResult(
                        command=command,
                        exit_code=-5,
                        stdout="",
                        stderr="Cancelled by user",
                        duration_seconds=round(duration, 3),
                        timed_out=False,
                        cancelled=True,
                        cwd=cwd,
                    )
                try:
                    proc.wait(timeout=poll_interval)
                    break
                except subprocess.TimeoutExpired:
                    elapsed += poll_interval
            else:
                # Full timeout hit
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
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
        finally:
            # Ensure process is cleaned up in all paths
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

        try:
            out, err = proc.communicate(timeout=5)
            duration = time.monotonic() - start
            return ClaudeRunResult(
                command=command,
                exit_code=proc.returncode,
                stdout=(out or ""),
                stderr=(err or ""),
                duration_seconds=round(duration, 3),
                timed_out=False,
                cwd=cwd,
            )
        except Exception:
            # Already cleaned up above, return already captured data
            duration = time.monotonic() - start
            return ClaudeRunResult(
                command=command,
                exit_code=proc.returncode if proc.poll() is not None else -1,
                stdout="",
                stderr="Process terminated before reading output",
                duration_seconds=round(duration, 3),
                timed_out=False,
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
