"""CLI entry point for Voice Claude Agent."""

import sys
import time

import click

from voice_claude_agent.config import (
    find_claude_executable,
    get_agent_state_dir,
)
from voice_claude_agent.claude_runner import run_claude
from voice_claude_agent.logging_store import write_session, write_last_result
from voice_claude_agent.recorder import SoundDeviceRecorder, FakeRecorder
from voice_claude_agent.risk import classify_risk, requires_confirmation
from voice_claude_agent.stt import FakeTranscriber, RecordingTranscriber, TextInputTranscriber
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


@main.command()
@click.option("--duration", "-d", default=5, help="Max recording duration in seconds.")
def record(duration: int):
    """Record audio from the microphone (push-to-talk) and print a summary.

    Press Enter to start recording. Press Enter again to stop and transcribe.
    """
    recorder = SoundDeviceRecorder()
    transcriber = RecordingTranscriber()

    input("Press Enter to start recording...")
    recorder.start()
    click.echo(f"Recording... (max {duration}s, press Enter to stop)")

    # Wait for stop signal or duration limit
    start = time.monotonic()
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    elapsed = time.monotonic() - start
    if elapsed > duration:
        click.echo(f"Duration limit ({duration}s) reached.")

    recorder.stop()
    audio = recorder.get_audio()
    click.echo(f"Recorded {len(audio)} bytes ({len(audio) / 2 / 16000:.1f}s)")

    text = transcriber.transcribe(audio)
    click.echo(f"Transcription: {text}")


@main.command()
@click.option("--duration", "-d", default=10, help="Max recording duration in seconds.")
@click.option("--fake", is_flag=True, help="Use fake recorder for testing.")
def voice(duration: int, fake: bool):
    """Record voice, transcribe, run Claude CLI, and speak the result."""
    if fake:
        recorder = FakeRecorder(b"test audio data")
        transcriber = TextInputTranscriber()
    else:
        recorder = SoundDeviceRecorder()
        transcriber = RecordingTranscriber()

    if not fake:
        input("Press Enter to start recording...")
        recorder.start()
        click.echo(f"Recording... (max {duration}s, press Enter to stop)")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        recorder.stop()
        audio = recorder.get_audio()
        click.echo(f"Recorded {len(audio)} bytes ({len(audio) / 2 / 16000:.1f}s)")
    else:
        audio = recorder.get_audio()

    text = transcriber.transcribe(audio)
    click.echo(f"Transcription: {text}")

    if not text.strip():
        click.echo("No speech detected. Aborting.")
        return

    _run_pipeline(text, input_mode="voice", tts_fake=False)


@main.command()
@click.argument("stub_text", default="请回复 OK")
def demo_voice(stub_text: str):
    """Run the voice pipeline with fake audio: recorder -> STT -> Claude CLI -> TTS."""
    recorder = FakeRecorder(b"stub audio")
    transcriber = FakeTranscriber(stub_text)

    transcript = transcriber.transcribe(recorder.get_audio())
    click.echo(f"Transcription: {transcript}")

    _run_pipeline(transcript, input_mode="voice", tts_fake=False)


if __name__ == "__main__":
    main()
