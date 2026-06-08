"""CLI entry point for Voice Claude Agent."""

import sys

import click

from voice_claude_agent.config import (
    check_mic_permission,
    find_claude_executable,
    get_agent_state_dir,
    get_claude_workdir,
    get_config_value,
)
from voice_claude_agent.claude_runner import run_claude
from voice_claude_agent.logging_store import write_session, write_last_result
from voice_claude_agent.recorder import SoundDeviceRecorder, FakeRecorder
from voice_claude_agent.risk import classify_risk, requires_confirmation
from voice_claude_agent.stt import (
    FakeTranscriber,
    RecordingTranscriber,
    TextInputTranscriber,
    _check_volcengine_credentials,
    _mask_credential,
    _t2s_convert,
    _volcengine_backend_available,
    list_available_backends,
)
from voice_claude_agent.summarizer import summarize, summarize_for_record
from voice_claude_agent.tts import MacOSSaySpeaker, FakeSpeaker
from voice_claude_agent.wake import ManualWakeTrigger

_STT_BACKEND_HELP = "STT backend: text-input (dev), whisper-cli, apple-speech (macOS), or volcengine-doubao (cloud ASR)"


def _resolve_stt_backend(option_value: str | None, default: str = "text-input") -> str:
    """Resolve STT backend with CLI option > env var > config.json > default priority."""
    return option_value or get_config_value("VOICE_STT_BACKEND", default)


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
        "claude_workdir": str(get_claude_workdir()),
        "claude_workdir_ok": get_claude_workdir().exists() and get_claude_workdir().is_dir(),
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

    # Volcengine ASR credential status
    ve_creds, _ve_err = _check_volcengine_credentials()
    status["volcengine_available"] = _volcengine_backend_available()
    status["volcengine_credentials"] = ve_creds

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


def _safe_record_attempt(recorder, max_duration: int) -> bytes:
    """Run a start→wait→stop cycle. Returns audio bytes or empty on error."""
    try:
        recorder.start()
    except Exception as e:
        click.echo(click.style(f"Recording start failed: {e}", fg="red"))
        return b""

    click.echo(f"Recording... (max {max_duration}s, press Enter to stop)")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass

    try:
        recorder.stop()
    except Exception as e:
        click.echo(click.style(f"Recording stop failed: {e}", fg="red"))
        return b""

    audio = recorder.get_audio()
    if not audio:
        click.echo(click.style("No audio captured. Check microphone connection.", fg="red"))
    return audio


def _stop_if_once(once: bool, iteration: int) -> bool:
    """Return True when a wake loop should stop after this iteration."""
    if once:
        click.echo(f"Wake loop stopped after {iteration} iteration(s).")
        return True
    return False


def _stt_is_error(transcript: str) -> bool:
    """Return True if the transcript looks like an STT backend error message."""
    return transcript.startswith("[STT error:")


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
    # Volcengine ASR credential status (masked)
    ve_creds = status["volcengine_credentials"]
    if ve_creds:
        click.echo("  Volcengine ASR:    CONFIGURED")
        for key in (
            "VOLCENGINE_ASR_API_KEY", "VOLCENGINE_ASR_APP_ID",
            "VOLCENGINE_ASR_ACCESS_TOKEN",
            "VOLCENGINE_ASR_RESOURCE_ID", "VOLCENGINE_ASR_CLUSTER",
            "VOLCENGINE_ASR_LANGUAGE", "VOLCENGINE_ASR_ENDPOINT",
        ):
            val = ve_creds.get(key, "")
            click.echo(f"    {key}: {_mask_credential(key, val)}")
    else:
        click.echo("  Volcengine ASR:    not configured")
    click.echo(f"  Agent state dir:  {status['agent_state_dir']}")
    click.echo(
        f"  Claude workdir:   {'OK' if status['claude_workdir_ok'] else 'INVALID'}"
        f" ({status['claude_workdir']})"
    )

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


def _run_pipeline(
    prompt: str,
    input_mode: str,
    tts_fake: bool,
    confirmation_override: bool | None = None,
) -> None:
    speaker = FakeSpeaker() if tts_fake else MacOSSaySpeaker()

    # 1. Risk classification
    risk_level = classify_risk(prompt)
    click.echo(f"Risk level: {risk_level.value}")

    # 2. Confirmation if required
    confirmation_required = requires_confirmation(risk_level)
    confirmation_received = False
    if confirmation_required:
        if confirmation_override is True:
            confirmation_received = True
        elif confirmation_override is False:
            click.echo("Action rejected. Aborting.")
            speaker.speak("高风险动作已被拒绝，未执行。")
            return
        else:
            from voice_claude_agent.confirmation import ask_confirmation

            if not ask_confirmation(prompt):
                click.echo("Action rejected. Aborting.")
                speaker.speak("高风险动作已被拒绝，未执行。")
                return
            confirmation_received = True

    # 3. Execute Claude CLI
    # F062: prepend zh-CN constraint
    wrapped = f"请始终使用简体中文回答。{prompt}"
    click.echo(
        f"Running: claude -p \"{wrapped[:80]}{'...' if len(wrapped) > 80 else ''}\""
    )
    result = run_claude(wrapped)
    result_cwd = result.cwd if isinstance(getattr(result, "cwd", ""), str) else ""

    if result.timed_out:
        click.echo(click.style("Claude CLI timed out.", fg="red"))
        speaker.speak("Claude CLI 执行超时，请检查任务或重试。")
        if tts_fake:
            assert isinstance(speaker, FakeSpeaker)
            click.echo(f"TTS (fake): {speaker.spoken[-1]}")
        write_session({
            "input_mode": input_mode,
            "transcript": prompt,
            "risk_level": risk_level.value,
            "confirmation_required": confirmation_required,
            "confirmation_received": confirmation_received,
            "claude_command": result.command,
            "claude_cwd": result_cwd,
            "exit_code": result.exit_code,
            "claude_stdout": result.stdout,
            "claude_stderr": result.stderr,
            "summary": "Timed out",
            "spoken_summary": "Claude CLI 执行超时，请检查任务或重试。",
            "spoken": True,
        })
        return

    # 4. Summarize
    combined = result.stdout
    if result.stderr and result.exit_code != 0:
        combined = result.stderr + "\n" + result.stdout
    combined = _t2s_convert(combined)
    summary = summarize_for_record(combined, result.exit_code, result.duration_seconds)
    spoken_summary = summarize(combined, result.exit_code, result.duration_seconds)
    click.echo(f"Summary: {spoken_summary}")

    # 5. Log to JSONL
    write_session({
        "input_mode": input_mode,
        "transcript": prompt,
        "risk_level": risk_level.value,
        "confirmation_required": confirmation_required,
        "confirmation_received": confirmation_received,
        "claude_command": result.command,
        "claude_cwd": result_cwd,
        "exit_code": result.exit_code,
        "claude_stdout": result.stdout,
        "claude_stderr": result.stderr,
        "summary": summary,
        "spoken_summary": spoken_summary,
        "spoken": True,
    })

    write_last_result({
        "prompt": prompt,
        "claude_cwd": result_cwd,
        "exit_code": result.exit_code,
        "summary": summary,
        "spoken_summary": spoken_summary,
        "risk_level": risk_level.value,
    })

    # 6. TTS speak
    speaker.speak(spoken_summary)
    if tts_fake:
        assert isinstance(speaker, FakeSpeaker)
        click.echo(f"TTS (fake): {speaker.spoken[-1]}")


@main.command()
@click.option("--duration", "-d", default=5, help="Max recording duration in seconds.")
@click.option("--stt-backend", default=None, help=_STT_BACKEND_HELP)
def record(duration: int, stt_backend: str | None):
    """Record audio from the microphone (push-to-talk) and print transcription.

    Press Enter to start recording. Press Enter again to stop.
    """
    permission, detail = check_mic_permission()
    _warn_mic(permission, detail)

    recorder = _safe_real_recorder()
    if recorder is None:
        return

    stt_backend = _resolve_stt_backend(stt_backend)
    transcriber = RecordingTranscriber(backend=stt_backend)
    click.echo(f"STT backend: {stt_backend}")

    input("Press Enter to start recording...")
    audio = _safe_record_attempt(recorder, max_duration=duration)
    if not audio:
        return

    click.echo(f"Recorded {len(audio)} bytes ({len(audio) / 2 / 16000:.1f}s)")

    text = transcriber.transcribe(audio)
    click.echo(f"Transcription: {text}")


@main.command()
@click.option("--duration", "-d", default=10, help="Max recording duration in seconds.")
@click.option("--fake", is_flag=True, help="Use fake recorder for testing.")
@click.option("--stt-backend", default=None, help=_STT_BACKEND_HELP)
def voice(duration: int, fake: bool, stt_backend: str | None):
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
        stt_backend = _resolve_stt_backend(stt_backend)
        transcriber = RecordingTranscriber(backend=stt_backend)
        click.echo(f"STT backend: {stt_backend}")

    if not fake:
        input("Press Enter to start recording...")
        audio = _safe_record_attempt(recorder, max_duration=duration)
        if not audio:
            return
        click.echo(f"Recorded {len(audio)} bytes ({len(audio) / 2 / 16000:.1f}s)")
    else:
        audio = recorder.get_audio()

    text = transcriber.transcribe(audio)
    click.echo(f"Transcription: {text}")

    if _stt_is_error(text):
        click.echo(click.style("STT error — cannot execute. Try a different --stt-backend.", fg="yellow"))
        return

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
@click.option("--stt-backend", default=None, help=_STT_BACKEND_HELP)
def wake(fake: bool, once: bool, stt_backend: str | None):
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
        stt_backend = _resolve_stt_backend(stt_backend)
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
            if not fake:
                audio = _safe_record_attempt(recorder, max_duration=10)
            else:
                recorder.start()
                import time as _time

                _time.sleep(0.5)
                recorder.stop()
                audio = recorder.get_audio()

            if not audio:
                click.echo(
                    click.style(
                        f"[{iteration}] No audio captured. Check microphone.",
                        fg="red",
                    )
                )
                if _stop_if_once(once, iteration):
                    break
                continue

            # 3. STT
            transcript = transcriber.transcribe(audio)
            click.echo(f"[{iteration}] Transcript: {transcript}")

            if _stt_is_error(transcript):
                click.echo(
                    click.style(
                        f"[{iteration}] STT error — retry or use a different --stt-backend.",
                        fg="yellow",
                    )
                )
                if _stop_if_once(once, iteration):
                    break
                continue

            if not transcript.strip():
                click.echo(
                    f"[{iteration}] No speech detected. Waiting for next wake."
                )
                if _stop_if_once(once, iteration):
                    break
                continue

            # 4. Run pipeline
            _run_pipeline(transcript, input_mode="voice", tts_fake=False)

            if _stop_if_once(once, iteration):
                break

    except KeyboardInterrupt:
        click.echo(f"\nWake loop stopped after {iteration} iteration(s).")


@main.command()
@click.option("--stt-backend", default=None, help=_STT_BACKEND_HELP)
def app(stt_backend: str | None):
    """Launch the macOS menu bar app (rumps-based system tray)."""
    from voice_claude_agent.app import launch_app

    click.echo("Launching Voice Claude Agent menu bar app...")
    stt_backend = _resolve_stt_backend(stt_backend)
    click.echo(f"STT backend: {stt_backend}")
    click.echo("Look for the 🎤 icon in your menu bar.")
    click.echo("Press Ctrl+C in this terminal to quit.")
    launch_app(stt_backend=stt_backend)


if __name__ == "__main__":
    main()
