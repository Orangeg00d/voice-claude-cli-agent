"""CLI entry point for Voice Claude Agent."""

import sys
from pathlib import Path

import click

from voice_claude_agent.config import (
    find_claude_executable,
    get_agent_state_dir,
    get_project_root,
)
from voice_claude_agent.claude_runner import run_claude
from voice_claude_agent.confirmation import confirm_or_reject
from voice_claude_agent.logging_store import write_session, write_last_result
from voice_claude_agent.risk import classify_risk, requires_confirmation
from voice_claude_agent.summarizer import summarize
from voice_claude_agent.tts import MacOSSaySpeaker, FakeSpeaker


def _check_dependencies() -> dict:
    """Check all dependencies and return status dict."""
    import platform

    status = {
        "platform": platform.system(),
        "python_version": sys.version,
        "claude_path": find_claude_executable(),
        "claude_available": False,
        "claude_version": None,
        "say_available": False,
        "agent_state_dir": str(get_agent_state_dir()),
    }

    # Check claude
    if status["claude_path"]:
        import subprocess

        try:
            proc = subprocess.run(
                [status["claude_path"], "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            status["claude_version"] = proc.stdout.strip() or proc.stderr.strip()
            status["claude_available"] = proc.returncode == 0
        except Exception:
            status["claude_available"] = False

    # Check say
    import shutil

    status["say_available"] = shutil.which("say") is not None

    return status


@click.group()
def main():
    """Voice Claude Agent — macOS voice-activated Claude CLI assistant."""


@main.command()
def check():
    """Check dependencies and Claude CLI availability."""
    status = _check_dependencies()
    click.echo("=== Voice Claude Agent — Dependency Check ===")
    click.echo(f"  Platform:         {status['platform']}")
    click.echo(f"  Python:           {status['python_version'].split()[0]}")
    click.echo(
        f"  Claude CLI:       {'AVAILABLE' if status['claude_available'] else 'NOT FOUND'}"
    )
    if status["claude_path"]:
        click.echo(f"  Claude path:      {status['claude_path']}")
    if status["claude_version"]:
        click.echo(f"  Claude version:   {status['claude_version']}")
    click.echo(f"  macOS say:        {'AVAILABLE' if status['say_available'] else 'NOT FOUND'}")
    click.echo(f"  Agent state dir:  {status['agent_state_dir']}")

    all_ok = (
        status["claude_available"]
        and status["say_available"]
        and status["platform"] == "Darwin"
    )
    if all_ok:
        click.echo("\nAll checks passed.")
    else:
        issues = []
        if not status["claude_available"]:
            issues.append("Claude CLI is not available.")
        if not status["say_available"]:
            issues.append("macOS 'say' command not found.")
        if status["platform"] != "Darwin":
            issues.append(
                f"Platform is {status['platform']}, but this agent targets macOS."
            )
        click.echo("\nIssues found:")
        for i in issues:
            click.echo(f"  - {i}")
        sys.exit(1)


@main.command()
@click.argument("prompt")
def demo_text(prompt: str):
    """Run the full demo-text pipeline: Claude CLI -> summarize -> TTS (fake)."""
    _run_pipeline(prompt, input_mode="text", tts_fake=True)


@main.command()
@click.argument("prompt")
def run_text(prompt: str):
    """Run Claude CLI with the given text prompt and speak the result."""
    _run_pipeline(prompt, input_mode="text", tts_fake=False)


def _run_pipeline(prompt: str, input_mode: str, tts_fake: bool) -> None:
    speaker = FakeSpeaker() if tts_fake else MacOSSaySpeaker()

    # 1. Risk classification
    risk_level = classify_risk(prompt)
    click.echo(f"Risk level: {risk_level.value}")

    # 2. Confirmation if required
    confirmation_required = requires_confirmation(risk_level)
    confirmation_received = False
    if confirmation_required:
        from voice_claude_agent.confirmation import ask_confirmation

        if not ask_confirmation(prompt):
            click.echo("Action rejected. Aborting.")
            speaker.speak("高风险动作已被拒绝，未执行。")
            return
        confirmation_received = True

    # 3. Execute Claude CLI
    click.echo(f"Running: claude -p \"{prompt[:80]}{'...' if len(prompt) > 80 else ''}\"")
    result = run_claude(prompt)

    # 4. Summarize
    combined = result.stdout
    if result.stderr and result.exit_code != 0:
        combined = result.stderr + "\n" + result.stdout
    summary = summarize(combined, result.exit_code, result.duration_seconds)
    click.echo(f"Summary: {summary}")

    # 5. Log to JSONL
    write_session({
        "input_mode": input_mode,
        "transcript": prompt,
        "risk_level": risk_level.value,
        "confirmation_required": confirmation_required,
        "confirmation_received": confirmation_received,
        "claude_command": result.command,
        "exit_code": result.exit_code,
        "summary": summary,
        "spoken": True,
    })

    write_last_result({
        "prompt": prompt,
        "exit_code": result.exit_code,
        "summary": summary,
        "risk_level": risk_level.value,
    })

    # 6. TTS speak
    speaker.speak(summary)
    if tts_fake:
        assert isinstance(speaker, FakeSpeaker)
        click.echo(f"TTS (fake): {speaker.spoken[-1]}")


if __name__ == "__main__":
    main()
