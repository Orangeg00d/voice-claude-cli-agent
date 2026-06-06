"""CLI entry point for Voice Claude Agent."""

import sys
import time

import click

from voice_claude_agent.config import (
    check_mic_permission,
    find_claude_executable,
    get_agent_state_dir,
)
from voice_claude_agent.claude_runner import run_claude
from voice_claude_agent.logging_store import write_session, write_last_result
from voice_claude_agent.recorder import SoundDeviceRecorder, FakeRecorder
from voice_claude_agent.risk import classify_risk, requires_confirmation
from voice_claude_agent.stt import (
    FakeTranscriber,
    RecordingTranscriber,
    TextInputTranscriber,
    list_available_backends,
)
from voice_claude_agent.summarizer import summarize
from voice_claude_agent.tts import MacOSSaySpeaker, FakeSpeaker
from voice_claude_agent.wake import ManualWakeTrigger

_STT_BACKEND_HELP = "STT backend: text-input (dev), whisper-cli, or apple-speech (macOS)"


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
        "mic_permission": False,
        "mic_detail": "",
        "stt_backends": [],
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

    # Check mic permission
    status["mic_permission"], status["mic_detail"] = check_mic_permission()

    # List STT backends
    status["stt_backends"] = list_available_backends()

    return status


def _warn_mic(permission: bool, detail: str) -> None:
    """Print a user-friendly warning if mic permission is missing."""
    if permission:
        return
    click.echo(click.style("WARNING: Microphone not available", fg="yellow", bold=True))
    click.echo(f"  {detail}")
    click.echo("  Grant permission in: System Settings > Privacy & Security > Microphone")
    click.echo("  Then restart your terminal and try again.")
    click.echo()


def _safe_real_recorder() -> SoundDeviceRecorder | None:
    """Create a SoundDeviceRecorder, returning None on failure with a message."""
    try:
        return SoundDeviceRecorder()
    except Exception as e:
        click.echo(click.style(f"Microphone error: {e}", fg="red"))
        click.echo(
            "Check mic permission in System Settings > Privacy & Security > Microphone."
        )
        click.echo("Use --fake or --stt-backend text-input for testing without a mic.")
        return None


@click.group()
def main():
    """Voice Claude Agent — macOS voice-activated Claude CLI assistant."""


@main.command()
def check():
    """Check dependencies, Claude CLI, microphone, and STT backends."""
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
    click.echo(
        f"  Microphone:       {'ACCESSIBLE' if status['mic_permission'] else 'DENIED / UNAVAILABLE'}"
    )
    if status["mic_detail"]:
        click.echo(f"    ({status['mic_detail']})")
    click.echo(
        f"  STT backends:     {', '.join(status['stt_backends']) if status['stt_backends'] else 'none'}"
    )
    click.echo(f"  Agent state dir:  {status['agent_state_dir']}")

    all_ok = (
        status["claude_available"]
        and status["say_available"]
        and status["platform"] == "Darwin"
    )
    if all_ok:
        click.echo("\nAll critical checks passed.")
        if not status["mic_permission"]:
            click.echo("Microphone is not available — real voice recording will not work.")
            click.echo("Use --fake for testing, or grant mic permission in System Settings.")
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
    click.echo(
        f"Running: claude -p \"{prompt[:80]}{'...' if len(prompt) > 80 else ''}\""
    )
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
@click.option(
    "--stt-backend", default="text-input", help=_STT_BACKEND_HELP,
)
def record(duration: int, stt_backend: str):
    """Record audio from the microphone (push-to-talk) and print transcription.

    Press Enter to start recording. Press Enter again to stop.
    """
    permission, detail = check_mic_permission()
    _warn_mic(permission, detail)

    recorder = _safe_real_recorder()
    if recorder is None:
        return

    transcriber = RecordingTranscriber(backend=stt_backend)
    click.echo(f"STT backend: {stt_backend}")

    input("Press Enter to start recording...")
    recorder.start()
    click.echo(f"Recording... (max {duration}s, press Enter to stop)")

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
    if not audio:
        click.echo(click.style("No audio captured. Check microphone connection.", fg="red"))
        return

    click.echo(f"Recorded {len(audio)} bytes ({len(audio) / 2 / 16000:.1f}s)")

    text = transcriber.transcribe(audio)
    click.echo(f"Transcription: {text}")


@main.command()
@click.option("--duration", "-d", default=10, help="Max recording duration in seconds.")
@click.option("--fake", is_flag=True, help="Use fake recorder for testing.")
@click.option(
    "--stt-backend", default="text-input", help=_STT_BACKEND_HELP,
)
def voice(duration: int, fake: bool, stt_backend: str):
    """Record voice, transcribe, run Claude CLI, and speak the result."""
    if fake:
        recorder = FakeRecorder(b"test audio data")
        transcriber = TextInputTranscriber()
    else:
        permission, detail = check_mic_permission()
        _warn_mic(permission, detail)

        real = _safe_real_recorder()
        if real is None:
            return
        recorder = real
        transcriber = RecordingTranscriber(backend=stt_backend)
        click.echo(f"STT backend: {stt_backend}")

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
        if not audio:
            click.echo(
                click.style("No audio captured. Check microphone connection.", fg="red")
            )
            return
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


@main.command()
@click.option("--fake", is_flag=True, help="Use fake recorder/STT for testing.")
@click.option("--once", is_flag=True, help="Run one iteration and exit (no loop).")
@click.option(
    "--stt-backend", default="text-input", help=_STT_BACKEND_HELP,
)
def wake(fake: bool, once: bool, stt_backend: str):
    """Wake loop: wait for trigger → record → STT → Claude CLI → TTS.

    Runs in a loop until Ctrl+C. In --fake mode, uses a FakeRecorder
    and FakeTranscriber so no real mic is required.
    """
    trigger = ManualWakeTrigger(auto_trigger=fake)

    if fake:
        recorder = FakeRecorder(b"stub wake audio")
        transcriber = FakeTranscriber("请回复 OK")
    else:
        permission, detail = check_mic_permission()
        _warn_mic(permission, detail)

        real = _safe_real_recorder()
        if real is None:
            return
        recorder = real
        transcriber = RecordingTranscriber(backend=stt_backend)
        click.echo(f"STT backend: {stt_backend}")

    click.echo("Voice Claude Agent — Wake Mode")
    click.echo("Press Ctrl+C to exit.")
    click.echo()

    iteration = 0
    try:
        while True:
            iteration += 1

            # 1. Wait for wake
            click.echo(f"[{iteration}] Waiting for wake trigger...")
            if not trigger.wait_for_wake():
                click.echo("Wake trigger cancelled. Exiting.")
                break

            click.echo(f"[{iteration}] Woke! Recording...")

            # 2. Record
            recorder.start()
            if fake:
                import time as _time

                _time.sleep(0.5)
            else:
                click.echo("Press Enter to stop recording...")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
            recorder.stop()
            audio = recorder.get_audio()

            if not audio and not fake:
                click.echo(
                    click.style(
                        f"[{iteration}] No audio captured. Check microphone.",
                        fg="red",
                    )
                )
                continue

            # 3. STT
            transcript = transcriber.transcribe(audio)
            click.echo(f"[{iteration}] Transcript: {transcript}")

            if not transcript.strip():
                click.echo(
                    f"[{iteration}] No speech detected. Waiting for next wake."
                )
                continue

            # 4. Run pipeline
            _run_pipeline(transcript, input_mode="voice", tts_fake=False)

            if once:
                click.echo(f"Wake loop stopped after {iteration} iteration(s).")
                break

    except KeyboardInterrupt:
        click.echo(f"\nWake loop stopped after {iteration} iteration(s).")


if __name__ == "__main__":
    main()
