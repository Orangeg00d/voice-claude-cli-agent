"""Phase 6 — macOS menu bar app.

Wraps the voice agent wake loop in a rumps-based system tray application.
Reuses cli.py's pipeline functions for recording, STT, Claude execution, and TTS.
"""

import threading
import time

import rumps

from voice_claude_agent.config import check_mic_permission, get_agent_state_dir

# Silence rumps debug output during tests
import logging
logging.getLogger("rumps").setLevel(logging.WARNING)


class VoiceClaudeApp(rumps.App):
    """macOS menu bar app for Voice Claude Agent.

    Menu items:
      Start Wake        — begins the continuous wake loop
      Stop Wake         — stops the wake loop
      Trigger Recording — fires a single record → STT → Claude → TTS cycle
      Mic Diagnostic    — shows detailed mic/recording diagnostic info
      Mic Status        — shows current microphone permission
      Quit              — exits the application (stops wake loop first)
    """

    DEFAULT_RECORD_SECONDS = 5

    def __init__(
        self,
        stt_backend: str = "text-input",
        *,
        _wake_target: callable | None = None,
        _alert_patch: callable | None = None,
    ):
        super().__init__(
            name="Voice Agent",
            title="\U0001f3a4",
            icon=None,
            quit_button=None,
        )

        self.stt_backend = stt_backend
        self._wake_target = _wake_target or self._run_wake_loop
        self._alert = _alert_patch or self._rumps_alert

        self._wake_active = False
        self._wake_event = threading.Event()
        self._trigger_event = threading.Event()
        self._wake_thread: threading.Thread | None = None

        self.start_item = rumps.MenuItem("Start Wake", callback=self._start_wake)
        self.stop_item = rumps.MenuItem("Stop Wake", callback=self._stop_wake)
        self.trigger_item = rumps.MenuItem(
            "Trigger Recording", callback=self._trigger_recording
        )
        self.diagnostic_item = rumps.MenuItem(
            "Mic Diagnostic", callback=self._run_mic_diagnostic
        )
        self.mic_status_item = rumps.MenuItem(
            "Mic Status: checking...", callback=self._refresh_mic_status_event
        )
        self.quit_item = rumps.MenuItem("Quit", callback=self._quit)

        self.menu = [
            self.start_item,
            self.stop_item,
            self.trigger_item,
            None,
            self.diagnostic_item,
            self.mic_status_item,
            None,
            self.quit_item,
        ]

        self._update_mic_status()
        self._sync_menu_titles()
        self._validate_stt_backend()

    # ── STT backend validation ───────────────────────────────

    def _validate_stt_backend(self) -> None:
        if self.stt_backend != "whisper-cli":
            return
        from voice_claude_agent.stt import _find_whisper_cpp_binary, _resolve_whisper_model

        binary = _find_whisper_cpp_binary()
        if not binary:
            self.mic_status_item.title = "STT: whisper-cli not installed"
            self._alert(
                title="STT Backend Unavailable",
                message=(
                    "whisper-cli is the default STT backend, "
                    "but whisper.cpp is not installed.\n\n"
                    "Install it: brew install whisper-cpp\n"
                    "Or set VOICE_STT_BACKEND=text-input and restart."
                ),
            )
            return
        model, model_err = _resolve_whisper_model()
        if model is None:
            self.mic_status_item.title = "STT: model not found"
            self._alert(
                title="Whisper Model Not Found",
                message=(
                    f"{model_err}\n\n"
                    "Set WHISPER_CPP_MODEL to a downloaded GGML model file,\n"
                    "or set VOICE_STT_BACKEND=text-input and restart."
                ),
            )

    # ── Mic permission helpers ────────────────────────────────

    def _update_mic_status(self) -> None:
        has_mic, detail = check_mic_permission()
        if has_mic:
            self.mic_status_item.title = "Mic: Accessible"
        else:
            short = detail[:40] + "..." if len(detail) > 40 else detail
            self.mic_status_item.title = f"Mic: Denied ({short})"

    def _refresh_mic_status_event(self, sender: rumps.MenuItem) -> None:
        self._update_mic_status()

    @staticmethod
    def _rumps_alert(title: str, message: str) -> None:
        rumps.alert(title=title, message=message)

    def _check_mic_or_alert(self) -> bool:
        has_mic, detail = check_mic_permission()
        if not has_mic:
            self.mic_status_item.title = "Mic: Denied"
            self._alert(
                title="Microphone Not Available",
                message=(
                    f"Cannot start recording: {detail}\n\n"
                    "Grant microphone access in:\n"
                    "System Settings > Privacy & Security > Microphone,\n"
                    "then restart this app."
                ),
            )
            return False
        return True

    # ── Mic Diagnostic ───────────────────────────────────────

    def _run_mic_diagnostic(self, sender: rumps.MenuItem) -> None:
        """Run a full mic/recording diagnostic and show the results in an alert."""
        import platform
        import sys

        lines = ["=== Mic Diagnostic ===", ""]

        # Bundle / app info
        lines.append("Bundle ID: com.voiceclaude.agent")
        lines.append(f"Python: {platform.python_version()}")
        lines.append(f"Agent state dir: {get_agent_state_dir()}")
        lines.append("Python path containing _sounddevice_data:")
        sounddevice_data_paths = [p for p in sys.path if "_sounddevice_data" in p or "python3.14" in p]
        if sounddevice_data_paths:
            lines.extend(f"- {p}" for p in sounddevice_data_paths[:3])
        else:
            lines.append("- not present in sys.path sample")

        # Input device info
        device_name = "unknown"
        portaudio_load_ok = False
        try:
            import _sounddevice_data
            import sounddevice as sd
            default_input = sd.query_devices(kind="input")
            device_name = default_input.get("name", "unknown")
            portaudio_load_ok = True
            data_path = next(iter(_sounddevice_data.__path__), "unknown")
            lines.append(f"_sounddevice_data path: {data_path}")
            lines.append(f"Default input device: {device_name}")
            lines.append(f"Input channels: {default_input.get('max_input_channels', '?')}")
            lines.append(f"Default sample rate: {default_input.get('default_samplerate', '?')} Hz")
        except Exception as e:
            lines.append(f"sounddevice query error: {e}")

        # PortAudio library path
        lines.append("")
        lines.append(f"PortAudio loaded: {'YES' if portaudio_load_ok else 'NO'}")
        try:
            import sounddevice as sd
            lib_path = sd._libname if hasattr(sd, '_libname') else "unknown"
            lines.append(f"PortAudio library: {lib_path}")
        except Exception:
            lines.append("PortAudio library: unable to determine")

        lines.append("")

        # TCC / permission info
        has_mic, detail = check_mic_permission()
        lines.append(f"Mic permission check: {'ACCESSIBLE' if has_mic else 'DENIED'}")
        if detail:
            lines.append(f"  Detail: {detail}")

        lines.append("")
        lines.append("If the .app does not appear in System Settings > Privacy & Security > Microphone:")
        lines.append("- The bundle must contain NSMicrophoneUsageDescription in Info.plist")
        lines.append("- The .app must be code-signed (ad-hoc is sufficient for TCC)")
        lines.append("- The .app must be launched from /Applications, ~/Applications, or via Finder")
        lines.append("- Running directly from dist/ or Terminal may not register TCC")
        lines.append("- Use: 'tccutil reset Microphone com.voiceclaude.agent' to reset permissions")

        self._alert(title="Mic Diagnostic", message="\n".join(lines))

    # ── Menu title sync ──────────────────────────────────────

    def _sync_menu_titles(self) -> None:
        if self._wake_active:
            self.start_item.title = "Start Wake (running)"
            self.start_item.set_callback(None)
        else:
            self.start_item.title = "Start Wake"
            self.start_item.set_callback(self._start_wake)

    # ── Start / Stop ─────────────────────────────────────────

    def _start_wake(self, sender: rumps.MenuItem) -> None:
        if self._wake_active:
            return
        self._update_mic_status()
        if not self._check_mic_or_alert():
            return
        self._wake_active = True
        self._wake_event.clear()
        self._trigger_event.clear()
        self._sync_menu_titles()
        self._wake_thread = threading.Thread(
            target=self._wake_target, daemon=True, name="wake-loop"
        )
        self._wake_thread.start()

    def _stop_wake(self, sender: rumps.MenuItem) -> None:
        if not self._wake_active:
            return
        self._wake_active = False
        self._wake_event.set()
        self._trigger_event.set()
        if self._wake_thread is not None and self._wake_thread.is_alive():
            self._wake_thread.join(timeout=5.0)
        self._sync_menu_titles()

    def _trigger_recording(self, sender: rumps.MenuItem) -> None:
        self._update_mic_status()
        if not self._check_mic_or_alert():
            return
        if not self._wake_active:
            self._wake_active = True
            self._wake_event.clear()
            self._trigger_event.set()
            self._sync_menu_titles()
            self._wake_thread = threading.Thread(
                target=self._wake_target, daemon=True, name="wake-loop"
            )
            self._wake_thread.start()
        else:
            self._trigger_event.set()

    def _quit(self, sender: rumps.MenuItem) -> None:
        if self._wake_active:
            self._stop_wake(sender)
        rumps.quit_application()

    # ── Wake loop (background thread) ────────────────────────

    def _run_wake_loop(self) -> None:
        while not self._wake_event.is_set():
            self._trigger_event.wait(timeout=0.5)
            if self._wake_event.is_set():
                break
            if not self._trigger_event.is_set():
                continue
            self._trigger_event.clear()
            self._record_and_execute()

    # ── Non-interactive recording (NO input()) ───────────────

    def _record_and_execute(self) -> None:
        from voice_claude_agent.cli import _run_pipeline
        from voice_claude_agent.stt import RecordingTranscriber

        recorder = self._open_mic_or_alert()
        if recorder is None:
            return

        self.trigger_item.title = "Recording..."
        audio, diag = self._record_fixed_duration_with_diag(recorder)
        if not audio:
            self.mic_status_item.title = "Mic: No audio captured"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="No Audio",
                message=(
                    "No audio was captured. Check your microphone connection.\n\n"
                    f"{diag}"
                ),
            )
            return

        self.trigger_item.title = "Transcribing..."
        transcriber = RecordingTranscriber(backend=self.stt_backend)
        transcript = transcriber.transcribe(audio)

        if not transcript.strip():
            self.mic_status_item.title = "Mic: Empty transcript"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="No Speech Detected",
                message="No speech was detected in the recording.",
            )
            return

        if transcript.startswith("[STT error:"):
            self.mic_status_item.title = "Mic: STT Error"
            self.trigger_item.title = "Trigger Recording"
            self._alert(title="STT Error", message=transcript)
            return

        self.trigger_item.title = "Running Claude..."
        _run_pipeline(transcript, input_mode="voice", tts_fake=False)

        self.trigger_item.title = "Done ✓"
        threading.Timer(1.5, lambda: setattr(self.trigger_item, "title", "Trigger Recording")).start()

    def _open_mic_or_alert(self):
        from voice_claude_agent.cli import _safe_real_recorder

        recorder = _safe_real_recorder()
        if recorder is None:
            self.mic_status_item.title = "Mic: Error"
            self.trigger_item.title = "Trigger Recording"
            self._alert(
                title="Recording Failed",
                message=(
                    "Could not access the microphone.\n\n"
                    "Check: System Settings > Privacy & Security > Microphone"
                ),
            )
            return None
        return recorder

    def _record_fixed_duration_with_diag(self, recorder) -> tuple[bytes, str]:
        """Record for DEFAULT_RECORD_SECONDS. Returns (audio_bytes, diagnostic_string)."""
        diag_parts = []
        device_name = "unknown"

        try:
            import sounddevice as sd
            dev = sd.query_devices(kind="input")
            device_name = dev.get("name", "unknown")
        except Exception as e:
            device_name = f"query error: {e}"
        diag_parts.append(f"Input device: {device_name}")

        frames_count = 0
        audio_len = 0

        try:
            recorder.start()
        except Exception as e:
            diag_parts.append(f"recorder.start: FAILED ({e})")
            return b"", "\n".join(diag_parts)

        diag_parts.append("recorder.start: OK")

        time.sleep(self.DEFAULT_RECORD_SECONDS)

        try:
            recorder.stop()
        except Exception as e:
            diag_parts.append(f"recorder.stop: FAILED ({e})")
            return b"", "\n".join(diag_parts)

        diag_parts.append("recorder.stop: OK")

        if hasattr(recorder, "_frames"):
            frames_count = len(recorder._frames)
        diag_parts.append(f"Frames captured: {frames_count}")

        audio = recorder.get_audio()
        audio_len = len(audio)
        diag_parts.append(f"Audio bytes: {audio_len} ({audio_len / 2 / 16000:.1f}s at 16kHz)")

        if not audio:
            diag_parts.append("RESULT: EMPTY AUDIO")
            diag_parts.append("")
            diag_parts.append("Possible causes:")
            diag_parts.append("- Mic permission denied for bundle. Check System Settings > Privacy & Security > Microphone.")
            diag_parts.append("- Bundle ID is com.voiceclaude.agent — verify this appears in the mic permissions list.")
            diag_parts.append("- If built via py2app, ensure NSMicrophoneUsageDescription is in Info.plist.")
            diag_parts.append("- The .app must be code-signed (ad-hoc is fine).")

        return audio, "\n".join(diag_parts)


def launch_app(stt_backend: str = "text-input") -> None:
    VoiceClaudeApp(stt_backend=stt_backend).run()
